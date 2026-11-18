"""Cached HTTP fetcher (M1 spec §4.1): ledger cache, secret params, pacing, safety."""

import httpx
import pytest

from tests.router.fake import Clock
from themis.config import Env, load_config
from themis.ingest.http import Fetcher, FetchError
from themis.ledger.dao import Ledger

TOML = """
[defaults]
timeout_s = 30
max_backoff_s = 10
max_retries_per_entry = 2
busy_timeout_ms = 5000
max_concurrency = 2
[network]
allowed_hosts = ["export.arxiv.org", "arxiv.org", "api.openalex.org", "x.example"]
[providers.p]
base_url = "https://x.example/v1/"
key_env = "K"
[[roles.extractor]]
provider = "p"
model = "m"
family = "f1"
[[roles.reasoner]]
provider = "p"
model = "m"
family = "f1"
[[roles.critic]]
provider = "p"
model = "m"
family = "f2"
[[roles.embedder]]
provider = "p"
model = "m"
family = "f3"
dims = 2
[sources.arxiv]
min_interval_s = 3
[sources.openalex]
mailto_env = "OPENALEX_MAILTO"
[sources.semantic_scholar]
enabled = false
key_env = "S2"
[ingest]
max_download_bytes = 1000
"""

EMAIL = "someone.private@example.org"


class Server:
    def __init__(self):
        self.routes: dict[str, list] = {}
        self.requests: list[httpx.Request] = []

    def on(self, path: str, *responses):
        self.routes.setdefault(path, []).extend(responses)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        q = self.routes.get(request.url.path)
        if not q:
            raise AssertionError(f"unexpected request {request.url}")
        r = q.pop(0)
        return r(request) if callable(r) else r


@pytest.fixture
def setup(tmp_path):
    p = tmp_path / "providers.toml"
    p.write_text(TOML, encoding="utf-8")
    cfg = load_config(p, Env({}), require_enabled=False)
    lg = Ledger.open(tmp_path / "data")
    server, clock = Server(), Clock()
    mono = {"t": 0.0}

    def sleep(s):
        clock.sleep(s)
        mono["t"] += s

    def make():
        return Fetcher(
            cfg,
            Env({"OPENALEX_MAILTO": EMAIL}),
            lg,
            transport=httpx.MockTransport(server.handler),
            clock=clock.now,
            sleep=sleep,
            monotonic=lambda: mono["t"],
        )

    yield make, server, clock, lg
    lg.close()


def test_fetch_and_cache(setup):
    make, server, _clock, lg = setup
    server.on(
        "/api/query",
        httpx.Response(200, content=b"<feed/>", headers={"content-type": "application/atom+xml"}),
    )
    f = make()
    r = f.get("https://export.arxiv.org/api/query", {"search_query": "all:lora", "start": 0})
    assert (r.status, r.body, r.cached) == (200, b"<feed/>", False)
    again = make().get(
        "https://export.arxiv.org/api/query", {"start": 0, "search_query": "all:lora"}
    )
    assert again.cached and again.body == b"<feed/>" and again.key == r.key
    assert len(server.requests) == 1
    url, status = lg.conn.execute("SELECT url, status FROM http_fetches").fetchone()
    assert status == 200 and "search_query=all%3Alora" in url


def test_secret_param_sent_but_never_stored(setup):
    make, server, _clock, lg = setup
    body = b'{"results": [], "meta": {"note": "polite pool for ' + EMAIL.encode() + b'"}}'
    server.on("/works", httpx.Response(200, content=body))
    r = make().get(
        "https://api.openalex.org/works", {"search": "lora"}, secrets={"mailto": "OPENALEX_MAILTO"}
    )
    assert server.requests[0].url.params["mailto"] == EMAIL  # sent on the wire
    stored = r.url + r.body.decode()
    for (sha,) in lg.conn.execute("SELECT sha256 FROM blobs"):
        stored += lg.blobs.get(sha).decode()
    for (u,) in lg.conn.execute("SELECT url FROM http_fetches"):
        stored += u
    assert EMAIL not in stored and "mailto" not in r.url


def test_secret_absent_gives_same_cache_key(setup, tmp_path):
    make, server, _clock, _lg = setup
    server.on("/works", httpx.Response(200, content=b"{}"))
    with_mail = make().get(
        "https://api.openalex.org/works", {"search": "lora"}, secrets={"mailto": "OPENALEX_MAILTO"}
    )
    without = make().get("https://api.openalex.org/works", {"search": "lora"})
    assert with_mail.key == without.key and without.cached


def test_arxiv_pacing(setup):
    make, server, clock, _lg = setup
    server.on("/api/query", *[httpx.Response(200, content=b"x")] * 3)
    f = make()
    for i in range(3):
        f.get("https://export.arxiv.org/api/query", {"start": i})
    assert clock.sleeps == [3.0, 3.0]


def test_cache_hits_do_not_pace(setup):
    make, server, clock, _lg = setup
    server.on("/api/query", httpx.Response(200, content=b"x"))
    f = make()
    for _ in range(3):
        f.get("https://export.arxiv.org/api/query", {"start": 0})
    assert clock.sleeps == []


def test_404_is_cached(setup):
    make, server, _clock, _lg = setup
    server.on("/e-print/9999.99999v1", httpx.Response(404, content=b"not found"))
    r = make().get("https://arxiv.org/e-print/9999.99999v1")
    assert r.status == 404
    assert make().get("https://arxiv.org/e-print/9999.99999v1").cached


def test_5xx_retries_then_fails_uncached(setup):
    make, server, _clock, lg = setup
    server.on("/api/query", *[httpx.Response(503)] * 3)
    with pytest.raises(FetchError, match="503"):
        make().get("https://export.arxiv.org/api/query", {"q": 1})
    assert len(server.requests) == 3
    assert lg.conn.execute("SELECT count(*) FROM http_fetches").fetchone()[0] == 0


def test_429_waits_for_retry_after(setup):
    make, server, clock, _lg = setup
    server.on(
        "/works",
        httpx.Response(429, headers={"retry-after": "4"}),
        httpx.Response(200, content=b"{}"),
    )
    make().get("https://api.openalex.org/works", {"search": "x"})
    assert 4.0 in clock.sleeps


def test_other_4xx_fails_uncached(setup):
    make, server, _clock, _lg = setup
    server.on("/works", httpx.Response(403))
    with pytest.raises(FetchError, match="403"):
        make().get("https://api.openalex.org/works")


def test_host_allowlist_and_https(setup):
    make, _server, _clock, _lg = setup
    with pytest.raises(FetchError, match="allowed"):
        make().get("https://evil.example/x")
    with pytest.raises(FetchError, match="https"):
        make().get("http://arxiv.org/e-print/1")


def test_redirect_followed_only_to_allowed_hosts(setup):
    make, server, _clock, _lg = setup
    server.on("/e-print/1", httpx.Response(302, headers={"location": "https://arxiv.org/src/1"}))
    server.on("/src/1", httpx.Response(200, content=b"tex"))
    assert make().get("https://arxiv.org/e-print/1").body == b"tex"
    server.on("/e-print/2", httpx.Response(302, headers={"location": "https://evil.example/2"}))
    with pytest.raises(FetchError, match="allowed"):
        make().get("https://arxiv.org/e-print/2")


def test_download_size_cap(setup):
    make, server, _clock, _lg = setup
    server.on("/e-print/big", httpx.Response(200, content=b"x" * 1001))
    with pytest.raises(FetchError, match="larger"):
        make().get("https://arxiv.org/e-print/big")
