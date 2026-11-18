"""Router behaviour (M1 spec §3.3-3.5, §10.4) against the fake provider."""

from datetime import timedelta

import httpx
import pytest

from tests.conftest import NetworkBlocked
from tests.router import fake as F
from tests.router.conftest import SECRETS, TEMPLATE, Answer
from themis.config import Env
from themis.router.cache import cache_key
from themis.router.client import Router
from themis.router.errors import AllEntriesFailed, QuotaExhausted, RouterError

MSG = [{"role": "user", "content": "Label this table."}]
A, B, C = "a.example", "b.example", "c.example"


def chat(router, role="extractor", schema=Answer, messages=MSG):
    return router.chat(role, messages, schema=schema, template=TEMPLATE)


def llm_rows(ledger):
    return ledger.conn.execute(
        "SELECT provider, model FROM llm_calls ORDER BY created_at"
    ).fetchall()


# ---------------------------------------------------------------- happy path and cache


def test_success_first_entry(router, fake, ledger):
    fake.queue(A, "ext-1", F.ok({"answer": "yes"}))
    r = chat(router)
    assert r.output == Answer(answer="yes")
    assert (r.provider, r.model, r.cached) == ("pa", "ext-1", False)
    assert llm_rows(ledger) == [("pa", "ext-1")]
    assert fake.calls[0].path == "/v1/chat/completions"
    assert fake.calls[0].headers["authorization"] == "Bearer " + SECRETS["KEY_A"]


def test_cache_hit_never_touches_network(router, fake, make_router):
    fake.queue(A, "ext-1", F.ok({"answer": "yes"}))
    first = chat(router)

    def explode(request):
        raise AssertionError("cache hit must not reach the transport")

    for r in (router, make_router(transport=httpx.MockTransport(explode))):
        again = chat(r)
        assert again.cached and again.output == first.output and again.key == first.key
    assert len(fake.calls) == 1


def test_cache_first_across_fallback_list(make_router, fake, ops, clock):
    """Answered by fallback entry 2 yesterday; entry 1 is fine today; still no request."""
    fake.queue(A, "ext-1", F.daily_quota())
    fake.queue(B, "ext-2", F.ok({"answer": "from-b"}))
    first = chat(make_router())
    assert (first.provider, first.model) == ("pb", "ext-2")

    clock.advance(days=2)  # entry 1's quota has reset

    def explode(request):
        raise AssertionError("must reuse the cached fallback answer")

    r = chat(make_router(transport=httpx.MockTransport(explode)))
    assert r.cached and r.output.answer == "from-b" and r.model == "ext-2"


def test_failed_responses_are_not_cached(router, fake, ledger):
    fake.queue(A, "ext-1", F.status(500), F.status(500), F.status(500))
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    chat(router)
    assert llm_rows(ledger) == [("pb", "ext-2")]


def test_request_and_response_blobs_stored(router, fake, ledger):
    fake.queue(A, "ext-1", F.ok({"answer": "yes"}))
    r = chat(router)
    req_blob, resp_blob, tmpl = ledger.conn.execute(
        "SELECT request_blob, response_blob, prompt_template FROM llm_calls WHERE key = ?",
        (r.key,),
    ).fetchone()
    assert b"Label this table." in ledger.blobs.get(req_blob)
    assert b'\\"answer\\"' in ledger.blobs.get(resp_blob) or b"answer" in ledger.blobs.get(
        resp_blob
    )
    assert tmpl == "label_table@abc123"


# ---------------------------------------------------------------- fallback order


@pytest.mark.parametrize(
    ("label", "entry1"),
    [
        ("daily quota", [F.daily_quota()]),
        ("long retry-after", [F.rate_limited(retry_after=3600)]),
        ("5xx until retries run out", [F.status(503)] * 3),
        ("timeouts", [F.timeout()] * 3),
        ("context length", [F.context_too_long()]),
        ("bad key", [F.status(401, {"error": {"message": "invalid api key"}})]),
        ("forbidden", [F.status(403)]),
        ("not json twice", [F.ok("this is not json"), F.ok("still not json")]),
        ("schema-invalid twice", [F.ok({"wrong": 1}), F.ok({"wrong": 2})]),
    ],
)
def test_falls_back_in_order(router, fake, label, entry1):
    fake.queue(A, "ext-1", *entry1)
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    r = chat(router)
    assert r.model == "ext-2"
    assert fake.sequence() == [(A, "ext-1")] * len(entry1) + [(B, "ext-2")]
    fake.assert_drained()


def test_falls_through_two_entries_in_order(router, fake):
    fake.queue(A, "ext-1", F.daily_quota())
    fake.queue(B, "ext-2", F.status(401))
    fake.queue(C, "ext-3", F.ok({"answer": "c"}))
    assert chat(router).model == "ext-3"
    assert fake.sequence() == [(A, "ext-1"), (B, "ext-2"), (C, "ext-3")]


# ---------------------------------------------------------------- 429 handling


def test_short_retry_after_waits_and_retries_same_entry(router, fake, clock):
    fake.queue(A, "ext-1", F.rate_limited(retry_after=2), F.ok({"answer": "a"}))
    assert chat(router).model == "ext-1"
    assert clock.sleeps == [2.0]


def test_gemini_retry_info_is_honoured(router, fake, clock):
    fake.queue(A, "ext-1", F.gemini_retry("7s"), F.ok({"answer": "a"}))
    assert chat(router).model == "ext-1"
    assert clock.sleeps == [7.0]


def test_groq_style_reset_header(router, fake, clock):
    fake.queue(
        A,
        "ext-1",
        F.status(429, {}, {"x-ratelimit-reset-requests": "1m2.5s"}),
        F.ok({"answer": "a"}),
    )
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    # 62.5s > max_backoff_s (10): don't wait, move on.
    assert chat(router).model == "ext-2"
    assert clock.sleeps == []


def test_never_sleeps_past_max_backoff(router, fake, clock):
    fake.queue(A, "ext-1", F.rate_limited(), F.rate_limited(), F.rate_limited())
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    chat(router)
    assert clock.sleeps and all(s <= 10 for s in clock.sleeps)


def test_long_wait_marks_entry_exhausted_until_reset(router, fake, ops, clock):
    fake.queue(A, "ext-1", F.rate_limited(retry_after=600))
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    chat(router)
    assert ops.exhausted_until("pa", "ext-1", clock.now()) == clock.now() + timedelta(seconds=600)


def test_daily_quota_without_reset_waits_until_utc_midnight(router, fake, ops, clock):
    fake.queue(A, "ext-1", F.daily_quota())
    fake.queue(B, "ext-2", F.ok({"answer": "b"}))
    chat(router)
    until = ops.exhausted_until("pa", "ext-1", clock.now())
    assert until is not None and until.hour == 0 and until.minute == 0 and until > clock.now()


# ---------------------------------------------------------------- quota exhaustion


def _exhaust_all(fake):
    fake.queue(A, "ext-1", F.daily_quota())
    fake.queue(B, "ext-2", F.rate_limited(retry_after=900))
    fake.queue(C, "ext-3", F.rate_limited(retry_after=300))


def test_all_exhausted_raises_quota_exhausted_with_earliest_reset(router, fake, clock):
    _exhaust_all(fake)
    with pytest.raises(QuotaExhausted) as exc:
        chat(router)
    assert exc.value.role == "extractor"
    assert exc.value.earliest_reset == clock.now() + timedelta(seconds=300)


def test_exhausted_entries_are_skipped_without_a_request(make_router, fake, clock):
    _exhaust_all(fake)
    with pytest.raises(QuotaExhausted):
        chat(make_router())
    n = len(fake.calls)
    with pytest.raises(QuotaExhausted):  # a fresh router (next process) reads ops.db
        chat(make_router(), messages=[{"role": "user", "content": "another prompt"}])
    assert len(fake.calls) == n


def test_entry_is_retried_after_its_reset(make_router, fake, clock):
    _exhaust_all(fake)
    with pytest.raises(QuotaExhausted):
        chat(make_router())
    clock.advance(seconds=301)  # ext-3's reset passes; the others are still exhausted
    fake.queue(C, "ext-3", F.ok({"answer": "c"}))
    assert chat(make_router()).model == "ext-3"


def test_errors_without_quota_raise_all_entries_failed(router, fake):
    fake.queue(A, "ext-1", F.status(401))
    fake.queue(B, "ext-2", F.status(400, {"error": {"message": "bad request"}}))
    fake.queue(C, "ext-3", F.ok("nope"), F.ok("nope again"))
    with pytest.raises(AllEntriesFailed) as exc:
        chat(router)
    assert not isinstance(exc.value, QuotaExhausted)
    assert len(exc.value.attempts) == 3


def test_bad_key_disables_entry_for_the_process(router, fake):
    fake.queue(A, "ext-1", F.status(401))
    fake.queue(B, "ext-2", F.ok({"answer": "1"}), F.ok({"answer": "2"}))
    chat(router)
    chat(router, messages=[{"role": "user", "content": "second"}])
    assert fake.sequence() == [(A, "ext-1"), (B, "ext-2"), (B, "ext-2")]


# ---------------------------------------------------------------- malformed output


def test_one_repair_retry_on_same_entry(router, fake, ledger):
    fake.queue(A, "ext-1", F.ok({"wrong": 1}), F.ok({"answer": "fixed"}))
    r = chat(router)
    assert r.model == "ext-1" and r.output.answer == "fixed"
    repair = fake.calls[1].body["messages"]
    assert len(repair) == len(MSG) + 2  # + the bad answer + the validation error
    assert "answer" in repair[-1]["content"]  # names the missing field
    # Cached under the original request, so a re-run hits it.
    assert r.key == cache_key("pa", "ext-1", router.request_body("pa", "ext-1", MSG, Answer), 1)


def test_free_text_without_schema(router, fake):
    fake.queue(A, "ext-1", F.ok("plain words"))
    assert chat(router, schema=None).output == "plain words"


# ---------------------------------------------------------------- cache key


def test_cache_key_properties(router):
    body = router.request_body("pa", "ext-1", MSG, Answer)
    k = cache_key("pa", "ext-1", body, 1)
    reordered = {key: body[key] for key in reversed(list(body))}
    assert cache_key("pa", "ext-1", reordered, 1) == k
    assert cache_key("pa", "ext-2", body, 1) != k
    assert cache_key("pb", "ext-1", body, 1) != k
    assert cache_key("pa", "ext-1", body, 2) != k
    assert cache_key("pa", "ext-1", {**body, "temperature": 0.7}, 1) != k
    assert "authorization" not in str(body).lower()


def test_cache_key_independent_of_api_key(cfg, fake, clock, ledger, ops):
    keys = []
    for secret in ("sk-one", "sk-two"):
        r = Router(
            cfg,
            Env({**SECRETS, "KEY_A": secret}),
            ledger,
            ops,
            transport=fake.transport,
            clock=clock.now,
            sleep=clock.sleep,
        )
        keys.append(cache_key("pa", "ext-1", r.request_body("pa", "ext-1", MSG, Answer), 1))
    assert keys[0] == keys[1]


# ---------------------------------------------------------------- secrets and hosts


def test_keys_never_stored(router, fake, ledger, ops, tmp_path):
    fake.queue(A, "ext-1", F.rate_limited(retry_after=2), F.ok({"answer": "a"}))
    chat(router)
    stored = b""
    for (sha,) in ledger.conn.execute("SELECT sha256 FROM blobs"):
        stored += ledger.blobs.get(sha)
    stored += (tmp_path / "data" / "ops.db").read_bytes()
    for (row,) in ledger.conn.execute("SELECT url FROM http_fetches"):
        stored += row.encode()
    for secret in SECRETS.values():
        assert secret.encode() not in stored
    for call in fake.calls:
        assert all(s not in str(call.body) for s in SECRETS.values())


def test_host_allowlist_enforced(router):
    with pytest.raises(RouterError, match="allowed"):
        router.guard_url("https://evil.example/v1/chat/completions")
    router.guard_url("https://a.example/v1/chat/completions")


def test_real_transport_is_blocked_in_tests(cfg, ledger, ops):
    r = Router(cfg, Env(SECRETS), ledger, ops)  # default (real) transport
    with pytest.raises((NetworkBlocked, AllEntriesFailed)) as exc:
        chat(r)
    if isinstance(exc.value, AllEntriesFailed):
        assert "network" in str(exc.value)


# ---------------------------------------------------------------- embeddings


def test_embed_and_cache(router, fake):
    fake.queue(C, "emb-1", F.embedding([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]))
    r = router.embed("embedder", ["a", "b"])
    assert r.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]] and r.model == "emb-1"
    again = router.embed("embedder", ["a", "b"])
    assert again.cached and len(fake.calls) == 1


def test_embed_wrong_dims_falls_back(router, fake):
    fake.queue(C, "emb-1", F.embedding([[0.1, 0.2]]))
    fake.queue(A, "emb-2", F.embedding([[0.1, 0.2, 0.3]]))
    assert router.embed("embedder", ["a"]).model == "emb-2"


def test_unknown_role(router):
    with pytest.raises(RouterError):
        router.chat("oracle", MSG, schema=None, template=TEMPLATE)
