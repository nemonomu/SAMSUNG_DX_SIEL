from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fpkt_api"))

from phase_probe import (  # noqa: E402
    curl_data_raw,
    curl_headers,
    curl_url,
    split_curl_commands,
)


ENDPOINT = "https://2.rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false"
BODY = '{"pageUri":"/sample/p/itm?pid=TEST123","pageContext":{}}'


def cmd_curl(*, modern_url: bool) -> str:
    url_option = "--url " if modern_url else ""
    escaped_body = BODY.replace('"', '\\"')
    return (
        f'curl {url_option}^"{ENDPOINT}^" ^\n'
        '  -H ^"Accept: application/json^" ^\n'
        '  -H ^"Sec-CH-UA: ^\\^"Chromium^\\^";v=^\\^"151^\\^"^" ^\n'
        '  -H ^"Content-Length: 999^" ^\n'
        '  -b ^"anonymous-session=1^" ^\n'
        f'  --data-raw ^"{escaped_body}^"'
    )


def bash_curl() -> str:
    return (
        f"curl '{ENDPOINT}' \\\n"
        "  -H 'Accept: application/json' \\\n"
        "  -H 'Content-Length: 999' \\\n"
        "  -b 'anonymous-session=1' \\\n"
        f"  --data-raw '{BODY}'"
    )


class CurlParsingTests(unittest.TestCase):
    def assert_valid_capture(self, command: str) -> None:
        self.assertEqual(curl_url(command), ENDPOINT)
        self.assertEqual(json.loads(curl_data_raw(command) or ""), json.loads(BODY))
        headers = curl_headers(command)
        self.assertEqual(headers["Accept"], "application/json")
        if "Sec-CH-UA" in headers:
            self.assertEqual(headers["Sec-CH-UA"], '"Chromium";v="151"')
        self.assertEqual(headers["Cookie"], "anonymous-session=1")
        self.assertNotIn("Content-Length", headers)

    def test_legacy_cmd_capture(self) -> None:
        self.assert_valid_capture(cmd_curl(modern_url=False))

    def test_modern_cmd_url_capture(self) -> None:
        self.assert_valid_capture(cmd_curl(modern_url=True))

    def test_bash_capture(self) -> None:
        self.assert_valid_capture(bash_curl())

    def test_multiple_modern_cmd_captures_are_split(self) -> None:
        text = "\ufeff" + cmd_curl(modern_url=True) + " & " + cmd_curl(modern_url=True)
        self.assertEqual(len(split_curl_commands(text)), 2)


if __name__ == "__main__":
    unittest.main()
