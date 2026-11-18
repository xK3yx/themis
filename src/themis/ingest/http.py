"""Cached HTTP fetcher for search APIs and e-prints (M1 spec §4.1).

Every GET is cached in the ledger (`http_fetches` + a blob), so re-running a search or a
download replays from the ledger, and every paper's metadata traces to the exact response
it came from. Secret parameters (e.g. OpenAlex `mailto`) are sent on the wire but left out
of the cache key, the stored URL and the stored body.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from themis.config import Config, Env
from themis.ledger.blobstore import utc_now
from themis.ledger.canonical import content_hash
from themis.ledger.dao import Ledger
from themis.ledger.db import tx
from themis.router.client import retry_wait

CACHEABLE = (200, 404, 410)  # definitive answers; everything else is retried or raised
PACED_HOSTS = ("export.arxiv.org", "arxiv.org")
MAX_REDIRECTS = 3


class FetchError(Exception):
    """A fetch failed and nothing was cached."""


@dataclass(frozen=True)
class FetchResult:
    key: str
    url: str  # as stored: public params only
    status: int
    body: bytes
    content_type: str | None
    cached: bool


class Fetcher:
    def __init__(
        self,
        config: Config,
        env: Env,
        ledger: Ledger,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.config = config
        self.env = env
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.http = httpx.Client(
            transport=transport, timeout=config.defaults.timeout_s, follow_redirects=False
        )
        self._last: dict[str, float] = {}
        self._last_wait: float | None = None  # Retry-After from the latest 429

    # ------------------------------------------------------------ public

    def get(
        self,
        url: str,
        params: dict | None = None,
        *,
        secrets: dict[str, str] | None = None,
        media_type: str | None = None,
    ) -> FetchResult:
        """GET `url`. `secrets` maps a query param name to the env var holding its value."""
        params = {k: str(v) for k, v in (params or {}).items()}
        self._guard(url)
        key = content_hash({"method": "GET", "url": url, "params": params})
        stored_url = url + ("?" + urlencode(sorted(params.items())) if params else "")

        hit = self.ledger.conn.execute(
            "SELECT status, response_blob, content_type FROM http_fetches WHERE key = ?", (key,)
        ).fetchone()
        if hit is not None:
            status, blob, ctype = hit
            return FetchResult(key, stored_url, status, self.ledger.blobs.get(blob), ctype, True)

        sent = dict(params)
        secret_values = []
        for name, env_name in (secrets or {}).items():
            value = self.env.get(env_name)
            if value:
                sent[name] = value
                secret_values.append(value)

        status, body, ctype = self._fetch(url, sent)
        for value in secret_values:  # never store a secret, even if the service echoes it
            body = body.replace(value.encode(), f"<{_env_name(secrets, value, self.env)}>".encode())
        if status not in CACHEABLE:
            raise FetchError(f"GET {stored_url}: HTTP {status}")

        blob = self.ledger.blobs.put(body, media_type or ctype or "application/octet-stream")
        with tx(self.ledger.conn):
            exists = self.ledger.conn.execute(
                "SELECT 1 FROM http_fetches WHERE key = ?", (key,)
            ).fetchone()
            if not exists:
                self.ledger.conn.execute(
                    "INSERT INTO http_fetches(key, method, url, status, response_blob,"
                    " content_type, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    (key, "GET", stored_url, status, blob, ctype, utc_now()),
                )
        return FetchResult(key, stored_url, status, body, ctype, False)

    # ------------------------------------------------------------ internals

    def _guard(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise FetchError(f"refusing non-https URL {url}")
        if parts.hostname not in self.config.network.allowed_hosts:
            raise FetchError(f"{parts.hostname!r} is not in network.allowed_hosts")

    def _pace(self, host: str) -> None:
        if host not in PACED_HOSTS:
            return
        gap = self.config.sources.arxiv.min_interval_s
        last = self._last.get(host)
        if last is not None:
            wait = gap - (self.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.monotonic()

    def _fetch(self, url: str, params: dict) -> tuple[int, bytes, str | None]:
        d = self.config.defaults
        tries = 0
        while True:
            try:
                status, body, ctype = self._fetch_once(url, params)
            except httpx.TransportError as err:  # includes timeouts
                status, body, ctype, transient = 0, b"", None, f"{type(err).__name__}: {err}"
            else:
                transient = None
                if status == 429 or status >= 500:
                    transient = f"HTTP {status}"
            if transient is None:
                return status, body, ctype
            if tries >= d.max_retries_per_entry:
                raise FetchError(f"GET {url}: {transient} (retries exhausted)")
            tries += 1
            wait = (
                self._last_wait
                if status == 429 and self._last_wait is not None
                else (2.0 ** (tries - 1))
            )
            self.sleep(min(wait, d.max_backoff_s))

    def _fetch_once(self, url: str, params: dict) -> tuple[int, bytes, str | None]:
        cap = self.config.ingest.max_download_bytes
        target, query = url, params
        for _ in range(MAX_REDIRECTS + 1):
            self._guard(target)
            self._pace(urlsplit(target).hostname or "")
            with self.http.stream("GET", target, params=query) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError(f"redirect without location from {target}")
                    target, query = urljoin(str(resp.url), location), None
                    continue
                self._last_wait = (
                    retry_wait(resp, self.clock()) if resp.status_code == 429 else None
                )
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise FetchError(f"GET {url}: response larger than {cap} bytes")
                    chunks.append(chunk)
                return resp.status_code, b"".join(chunks), resp.headers.get("content-type")
        raise FetchError(f"GET {url}: more than {MAX_REDIRECTS} redirects")


def _env_name(secrets: dict[str, str] | None, value: str, env: Env) -> str:
    for env_name in (secrets or {}).values():
        if env.get(env_name) == value:
            return env_name
    return "REDACTED"
