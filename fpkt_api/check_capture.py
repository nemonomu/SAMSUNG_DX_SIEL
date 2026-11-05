"""Install/check diagnostic Chromium using local HTML only; no collection/DB/email."""
from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import subprocess
import sys


REQUIREMENTS = Path(__file__).with_name("requirements-capture.txt")
PIN = "playwright==1.62.0"


def check_capture(out: Path | None = None) -> bool:
    try:
        if importlib.metadata.version("playwright") != PIN.split("==")[1]:
            print(f"[capture] required package: {PIN}")
            return False
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=20000)
            try:
                context = browser.new_context(viewport={"width": 900, "height": 500})
                # The smoke check cannot contact any external site.
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_content("<html><body style='font:28px sans-serif;padding:40px'>"
                                 "<h1>Flipkart diagnostic capture: OK</h1>"
                                 "<p>Offline test only. No collection or DB access.</p></body></html>")
                if out:
                    out.parent.mkdir(parents=True, exist_ok=True)
                png = page.screenshot(path=str(out) if out else None, timeout=15000)
                if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("invalid PNG")
                print(f"[capture] OK: {PIN}, Chromium {browser.version}, offline PNG={len(png)} bytes")
                if out:
                    print(f"[capture] screenshot={out.resolve()}")
            finally:
                browser.close()
        return True
    except Exception as exc:
        # Browser errors may contain machine-specific launch arguments.
        reason = type(exc).__name__
        if "Executable doesn't exist" in str(exc):
            reason = "Chromium binary missing"
        elif isinstance(exc, PermissionError):
            reason = "browser process permission denied"
        print(f"[capture] unavailable: {reason}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install only when the pinned browser check fails")
    parser.add_argument("--out", type=Path, help="Optional path to keep the offline smoke screenshot")
    args = parser.parse_args()
    if check_capture(args.out):
        return 0
    if not args.install:
        print("[capture] run schedule\\fpkt_api_setup_capture.bat with the scheduler's Python/user")
        return 1
    print("[capture] installing diagnostic package and Chromium; collection will not run", flush=True)
    for command in ([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
                    [sys.executable, "-m", "playwright", "install", "chromium"]):
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0 if check_capture(args.out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
