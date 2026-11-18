"""Record one real HTTP response as a test fixture (M1 spec §10.1).

    uv run python scripts/record_fixture.py NAME URL [key=value ...]

Writes tests/fixtures/http/NAME.body and NAME.meta.json. Sends no auth headers and no
secrets. Refuses to write if any non-blank .env value appears in the request URL or the
response body, so recorded fixtures can't leak keys or the OpenAlex mailto address.
Tests replay fixtures through httpx.MockTransport; they never touch the network.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from themis.config import Env  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "http"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    name, url, *pairs = sys.argv[1:]
    params = dict(p.split("=", 1) for p in pairs)
    if urlsplit(url).scheme != "https":
        raise SystemExit("https only")
    resp = httpx.get(
        url,
        params=params,
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": "themis-fixture-recorder"},
    )
    secrets = Env.from_file(ROOT / ".env").values()
    haystack = str(resp.request.url).encode() + resp.content
    if any(s.encode() in haystack for s in secrets):
        raise SystemExit("refusing to record: a .env value appears in the request or response")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.body").write_bytes(resp.content)
    meta = {
        "url": url,
        "params": params,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Bytes, not text: Windows text mode would write CRLF into a byte-exact fixture.
    (OUT / f"{name}.meta.json").write_bytes((json.dumps(meta, indent=2) + "\n").encode())
    print(f"recorded {name}: HTTP {resp.status_code}, {len(resp.content)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
