"""Remove only old, high-volume crawler artifacts.

Manual execution is dry-run by default. Scheduled runners pass ``--apply`` or
call the functions with ``apply=True`` after limiting the scope and products.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


SECONDS_PER_DAY = 24 * 60 * 60
DEFAULT_RETENTION_DAYS = 3.0
VALID_PRODUCTS = {"hhp", "tv", "ref", "ldy"}
FPKT_RUN_DIR_RE = re.compile(
    r"^ops_(?P<product>all|tv|hhp|ref|ldy)_\d{8}_\d{6}(?:_\d{3})?$",
    re.I,
)
AMAZON_LARGE_FILE_RE = re.compile(
    r"^siel_amazon_(?P<product>hhp|tv|ref|ldy)_.+\.(?:html|jsonl)$",
    re.I,
)


@dataclass
class CleanupStats:
    scope: str
    root: Path
    mode: str
    scanned: int = 0
    eligible: int = 0
    removed: int = 0
    removed_bytes: int = 0
    skipped: int = 0
    missing_root: bool = False
    failures: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def add_sample(self, name: str) -> None:
        if len(self.samples) < 10:
            self.samples.append(name)

    def summary(self) -> str:
        sample_text = ",".join(self.samples) if self.samples else "-"
        return (
            f"[artifact_cleanup] scope={self.scope} mode={self.mode} root={self.root} "
            f"missing_root={str(self.missing_root).lower()} scanned={self.scanned} "
            f"eligible={self.eligible} removed={self.removed} "
            f"removed_mb={self.removed_bytes / (1024 * 1024):.1f} "
            f"skipped={self.skipped} failures={len(self.failures)} samples={sample_text}"
        )


def retention_days_from_env(default: float = DEFAULT_RETENTION_DAYS) -> float:
    raw = os.environ.get("SIEL_ARTIFACT_RETENTION_DAYS")
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def cleanup_disabled() -> bool:
    return os.environ.get("SIEL_SKIP_ARTIFACT_CLEANUP", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def default_amazon_sync_dir() -> Path:
    return (
        Path.home()
        / "Documents"
        / "퀵오일"
        / "삼성전자"
        / "samsung_dx_retail_com"
        / "siel"
        / "logs"
    )


def _cutoff_timestamp(days: float, now: float | None) -> float:
    if days < 0:
        raise ValueError("retention days must be zero or greater")
    return (time.time() if now is None else now) - days * SECONDS_PER_DAY


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _is_direct_child(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve(strict=False).parent == root.resolve(strict=False)
    except OSError:
        return False


def _contains_reparse_point(root: Path) -> bool:
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in dirs:
            child = current_path / name
            if _is_reparse_point(child):
                return True
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            if _is_reparse_point(current_path / name):
                return True
    return False


def _tree_size(root: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not _is_reparse_point(current_path / name)]
        for name in files:
            path = current_path / name
            if _is_reparse_point(path):
                continue
            try:
                total += path.lstat().st_size
            except OSError:
                continue
    return total


def cleanup_flipkart_artifacts(
    root: Path,
    *,
    products: Iterable[str],
    days: float = DEFAULT_RETENTION_DAYS,
    apply: bool = False,
    now: float | None = None,
) -> CleanupStats:
    """Remove old direct-child ``ops_*`` run directories only."""
    root = Path(root)
    wanted = _normalized_products(products)
    stats = CleanupStats("fpkt", root, "apply" if apply else "dry-run")
    if not wanted:
        return stats
    if not root.is_dir():
        stats.missing_root = True
        return stats

    cutoff = _cutoff_timestamp(days, now)
    for candidate in root.iterdir():
        match = FPKT_RUN_DIR_RE.fullmatch(candidate.name)
        if not match or match.group("product").lower() not in wanted | {"all"}:
            continue
        stats.scanned += 1
        try:
            if (
                not candidate.is_dir()
                or not _is_direct_child(root, candidate)
                or _is_reparse_point(candidate)
            ):
                stats.skipped += 1
                continue
            if candidate.lstat().st_mtime > cutoff:
                stats.skipped += 1
                continue
            if _contains_reparse_point(candidate):
                stats.skipped += 1
                continue
            size = _tree_size(candidate)
            stats.eligible += 1
            stats.add_sample(candidate.name)
            if apply:
                shutil.rmtree(candidate)
                stats.removed += 1
                stats.removed_bytes += size
        except OSError as exc:
            stats.failures.append(f"{candidate.name}:{type(exc).__name__}")
    return stats


def _normalized_products(products: Iterable[str]) -> set[str]:
    normalized = {str(product).strip().lower() for product in products if str(product).strip()}
    invalid = normalized - VALID_PRODUCTS
    if invalid:
        raise ValueError(f"unsupported products: {','.join(sorted(invalid))}")
    return normalized


def cleanup_amazon_log_root(
    root: Path,
    products: Iterable[str],
    *,
    days: float = DEFAULT_RETENTION_DAYS,
    apply: bool = False,
    now: float | None = None,
    scope: str = "amzn",
) -> CleanupStats:
    """Remove old Amazon HTML/JSONL files for selected products only."""
    root = Path(root)
    wanted = _normalized_products(products)
    stats = CleanupStats(scope, root, "apply" if apply else "dry-run")
    if not wanted:
        return stats
    if not root.is_dir():
        stats.missing_root = True
        return stats

    cutoff = _cutoff_timestamp(days, now)
    for candidate in root.iterdir():
        match = AMAZON_LARGE_FILE_RE.fullmatch(candidate.name)
        if not match or match.group("product").lower() not in wanted:
            continue
        stats.scanned += 1
        try:
            if (
                not candidate.is_file()
                or not _is_direct_child(root, candidate)
                or _is_reparse_point(candidate)
            ):
                stats.skipped += 1
                continue
            info = candidate.lstat()
            if info.st_mtime > cutoff:
                stats.skipped += 1
                continue
            stats.eligible += 1
            stats.add_sample(candidate.name)
            if apply:
                candidate.unlink()
                stats.removed += 1
                stats.removed_bytes += info.st_size
        except OSError as exc:
            stats.failures.append(f"{candidate.name}:{type(exc).__name__}")
    return stats


def cleanup_amazon_artifacts(
    repo_root: Path,
    products: Iterable[str],
    *,
    days: float = DEFAULT_RETENTION_DAYS,
    apply: bool = False,
    now: float | None = None,
    sync_dir: Path | None = None,
) -> list[CleanupStats]:
    """Clean the Amazon source logs and the optional synced copy."""
    repo_root = Path(repo_root)
    wanted = _normalized_products(products)
    source = repo_root / "amzn" / "logs"
    sync = default_amazon_sync_dir() if sync_dir is None else Path(sync_dir)
    roots: list[tuple[str, Path]] = [("amzn", source)]
    try:
        if sync.resolve(strict=False) != source.resolve(strict=False):
            roots.append(("amzn_sync", sync))
    except OSError:
        roots.append(("amzn_sync", sync))

    return [
        cleanup_amazon_log_root(
            root,
            wanted,
            days=days,
            apply=apply,
            now=now,
            scope=scope,
        )
        for scope, root in roots
    ]


def _free_gb(path: Path) -> float | None:
    candidate = Path(path)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free / (1024 ** 3)
    except OSError:
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean old high-volume SIEL crawler artifacts")
    parser.add_argument("--scope", choices=("fpkt", "amzn"), required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--products", nargs="+", choices=sorted(VALID_PRODUCTS))
    parser.add_argument("--days", type=float, default=retention_days_from_env())
    parser.add_argument("--apply", action="store_true", help="delete eligible artifacts")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if cleanup_disabled():
        print("[artifact_cleanup] skipped by SIEL_SKIP_ARTIFACT_CLEANUP")
        return 0

    if not args.products:
        print("[artifact_cleanup] --products is required", file=sys.stderr)
        return 2

    before = _free_gb(args.repo_root)
    if args.scope == "fpkt":
        results = [
            cleanup_flipkart_artifacts(
                args.repo_root / "fpkt_api" / "test_output",
                products=args.products,
                days=args.days,
                apply=args.apply,
            )
        ]
    else:
        results = cleanup_amazon_artifacts(
            args.repo_root,
            args.products,
            days=args.days,
            apply=args.apply,
        )

    for result in results:
        print(result.summary())
        for failure in result.failures[:10]:
            print(f"[artifact_cleanup][warning] {result.scope} {failure}", file=sys.stderr)
    after = _free_gb(args.repo_root)
    if before is not None and after is not None:
        print(f"[artifact_cleanup] free_gb_before={before:.1f} free_gb_after={after:.1f}")
    return 1 if any(result.failures for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
