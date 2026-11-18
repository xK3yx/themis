"""A scriptable fake OpenAI-compatible provider for router tests (M1 spec §10.4).

Responses are queued per (host, model). An unexpected call fails loudly, so a test can
assert exactly which requests were made, in which order.
"""

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx


def ok(content: str | dict, headers: dict | None = None):
    text = content if isinstance(content, str) else json.dumps(content)

    def make(req):
        return httpx.Response(
            200,
            json={
                "id": "fake",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
            headers=headers or {},
        )

    return make


def embedding(vectors: list[list[float]]):
    def make(req):
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        )

    return make


def status(code: int, body: dict | list | str | None = None, headers: dict | None = None):
    def make(req):
        if body is None or isinstance(body, str):
            return httpx.Response(code, text=body or "", headers=headers or {})
        return httpx.Response(code, json=body, headers=headers or {})

    return make


def rate_limited(retry_after: float | None = None, body: dict | str | None = None):
    headers = {} if retry_after is None else {"retry-after": str(retry_after)}
    return status(429, body or {"error": {"message": "Rate limit reached"}}, headers)


def daily_quota():
    return status(
        429, {"error": {"message": "You exceeded your current quota: requests per day (free tier)"}}
    )


def gemini_retry(delay: str):
    return status(
        429,
        [
            {
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Resource has been exhausted",
                    "details": [
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}
                    ],
                }
            }
        ],
    )


def context_too_long():
    return status(400, {"error": {"message": "This model's maximum context length is 8192 tokens"}})


def timeout():
    def make(req):
        raise httpx.ReadTimeout("timed out", request=req)

    return make


@dataclass
class Call:
    host: str
    path: str
    model: str
    body: dict
    headers: dict


@dataclass
class Fake:
    queues: dict = field(default_factory=lambda: defaultdict(deque))
    calls: list[Call] = field(default_factory=list)

    def queue(self, host: str, model: str, *responses) -> "Fake":
        self.queues[(host, model)].extend(responses)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        model = body.get("model", "")
        self.calls.append(
            Call(request.url.host, request.url.path, model, body, dict(request.headers))
        )
        q = self.queues[(request.url.host, model)]
        if not q:
            raise AssertionError(f"unexpected call to {request.url.host} model={model}")
        return q.popleft()(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sequence(self) -> list[tuple[str, str]]:
        return [(c.host, c.model) for c in self.calls]

    def assert_drained(self) -> None:
        left = {k: len(v) for k, v in self.queues.items() if v}
        assert not left, f"scripted responses never requested: {left}"


class Clock:
    """Deterministic time: `sleep` advances `now` and is recorded."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)
