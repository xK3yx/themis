"""The router: one OpenAI-compatible client for every role (M1 spec §3.3-3.5).

Code passes a role and a prompt template, never a model name. Each call:
  1. looks up the cache for EVERY entry of the role, in order (a hit never touches the network);
  2. tries enabled, non-exhausted entries in config order, handling each failure kind as in
     the spec's table (backoff, repair retry, fallback, exhaustion);
  3. raises QuotaExhausted (a normal state) when quota is the reason nothing worked, or
     AllEntriesFailed otherwise.
"""

import email.utils
import json
import math
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pydantic
from pydantic import BaseModel

from themis.config import ROLES, Config, Env, RoleEntry
from themis.ledger.dao import Ledger
from themis.ops import OpsDB
from themis.router.cache import LlmCache, cache_key
from themis.router.errors import AllEntriesFailed, QuotaExhausted, RouterError

_DAILY = re.compile(r"per[\s_-]?day|daily|requests per day|\bRPD\b", re.IGNORECASE)
_CONTEXT = re.compile(r"context.{0,20}(length|window|too long|exceed)|too many tokens", re.I)
_DURATION = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?$"
)


@dataclass(frozen=True)
class TemplateRef:
    """A prompt template identity: its id and a hash of its text."""

    id: str
    sha: str

    def __str__(self) -> str:
        return f"{self.id}@{self.sha}"


@dataclass(frozen=True)
class ChatResult:
    output: Any  # validated schema instance, or str when no schema
    key: str  # cache key == llm_calls.key (put this in node producers)
    provider: str
    model: str
    cached: bool


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    key: str
    provider: str
    model: str
    dims: int
    cached: bool


class _Bad(Exception):
    """Unusable 200 response (malformed or schema-invalid). Carries the raw text."""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


def _parse_duration(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = _DURATION.match(text)
    if not m or not any(m.groupdict().values()):
        return None
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return g["h"] * 3600 + g["m"] * 60 + g["s"] + g["ms"] / 1000


def _find_retry_delay(obj: Any) -> str | None:
    if isinstance(obj, dict):
        if isinstance(obj.get("retryDelay"), str):
            return obj["retryDelay"]
        for v in obj.values():
            if (found := _find_retry_delay(v)) is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := _find_retry_delay(v)) is not None:
                return found
    return None


def retry_wait(resp: httpx.Response, now: datetime) -> float | None:
    """Seconds the provider asks us to wait, from headers or a Gemini RetryInfo body."""
    ra = resp.headers.get("retry-after")
    if ra:
        secs = _parse_duration(ra)
        if secs is not None:
            return max(0.0, secs)
        try:
            when = email.utils.parsedate_to_datetime(ra)
            return max(0.0, (when - now).total_seconds())
        except (TypeError, ValueError):
            pass
    waits = []
    for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "x-ratelimit-reset"):
        v = resp.headers.get(name)
        if not v:
            continue
        secs = _parse_duration(v)
        if secs is not None and secs > 1e12:  # epoch milliseconds (OpenRouter)
            secs = secs / 1000 - now.timestamp()
        elif secs is not None and secs > 1e9:  # epoch seconds
            secs = secs - now.timestamp()
        if secs is not None:
            waits.append(max(0.0, secs))
    if waits:
        return max(waits)
    try:
        delay = _find_retry_delay(resp.json())
    except ValueError:
        delay = None
    return _parse_duration(delay) if delay else None


def _next_utc_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _strip_fences(text: str) -> str:
    m = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, flags=re.S)
    return m.group(1) if m else text


class Router:
    def __init__(
        self,
        config: Config,
        env: Env,
        ledger: Ledger,
        ops: OpsDB,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
    ):
        self.config = config
        self.env = env
        self.cache = LlmCache(ledger)
        self.ops = ops
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or time.sleep
        self.jitter = jitter or (lambda: random.random() * 0.5)
        self.http = httpx.Client(transport=transport, timeout=config.defaults.timeout_s)
        self._disabled: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ helpers

    def guard_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.config.network.allowed_hosts:
            raise RouterError(f"{parts.hostname!r} is not in network.allowed_hosts (or not https)")

    def request_body(
        self, provider: str, model: str, messages: list[dict], schema: type[BaseModel] | None
    ) -> dict:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.config.defaults.temperature,
        }
        if schema is not None:
            body["response_format"] = {"type": "json_object"}
        return body

    def _entries(self, role: str) -> list[RoleEntry]:
        if role not in ROLES:
            raise RouterError(f"unknown role {role!r}")
        return self.config.roles[role]

    def _post(self, entry: RoleEntry, path: str, body: dict) -> httpx.Response:
        prov = self.config.providers[entry.provider]
        url = prov.base_url.rstrip("/") + "/" + path
        self.guard_url(url)
        key = self.env.get(prov.key_env)
        return self.http.post(url, json=body, headers={"Authorization": f"Bearer {key}"})

    def _sleep(self, seconds: float) -> None:
        self.sleep(min(seconds + self.jitter(), self.config.defaults.max_backoff_s))

    # ------------------------------------------------------------ chat

    @staticmethod
    def _parse_chat(raw: dict, schema: type[BaseModel] | None) -> Any:
        try:
            content = raw["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise _Bad("response has no choices[0].message.content", json.dumps(raw)) from None
        if not isinstance(content, str):
            raise _Bad("message content is not text", json.dumps(raw))
        if schema is None:
            return content
        try:
            data = json.loads(_strip_fences(content))
        except json.JSONDecodeError as e:
            raise _Bad(f"reply is not valid JSON: {e}", content) from None
        try:
            return schema.model_validate(data)
        except pydantic.ValidationError as e:
            raise _Bad(f"reply does not match the schema: {e}", content) from None

    def chat(
        self,
        role: str,
        messages: list[dict],
        *,
        schema: type[BaseModel] | None,
        template: Any,
    ) -> ChatResult:
        entries = self._entries(role)
        osv = int(getattr(schema, "SCHEMA_VERSION", 0)) if schema is not None else 0

        # 1. Cache-first across every entry of the role (enabled or not).
        for e in entries:
            key = cache_key(
                e.provider, e.model, self.request_body(e.provider, e.model, messages, schema), osv
            )
            raw = self.cache.get(key)
            if raw is not None:
                try:
                    out = self._parse_chat(raw, schema)
                except _Bad:
                    continue
                self.ops.count(role, hit=True)
                return ChatResult(out, key, e.provider, e.model, cached=True)
        self.ops.count(role, hit=False)

        def attempt(e: RoleEntry):
            body = self.request_body(e.provider, e.model, messages, schema)
            key = cache_key(e.provider, e.model, body, osv)
            sent, repaired = body, False
            while True:
                started = time.monotonic()
                resp = yield ("post", "chat/completions", sent)
                try:
                    out = self._parse_chat(resp.json(), schema)
                except (_Bad, ValueError) as err:
                    raw = err.raw if isinstance(err, _Bad) else resp.text
                    if repaired:
                        raise _Bad(f"still invalid after one repair: {err}", raw) from None
                    repaired = True
                    sent = {
                        **body,
                        "messages": [
                            *messages,
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": f"Your previous reply was invalid: {err}. Reply again "
                                "with only a JSON object matching the required schema.",
                            },
                        ],
                    }
                    continue
                self.cache.put(
                    key=key,
                    role=role,
                    provider=e.provider,
                    model=e.model,
                    request_record={"request": body, "sent": sent if repaired else None},
                    response=resp.json(),
                    template=str(template),
                    output_schema_version=osv,
                    usage=resp.json().get("usage"),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    now=self.clock(),
                )
                return ChatResult(out, key, e.provider, e.model, cached=False)

        return self._run(role, entries, attempt)

    # ------------------------------------------------------------ embeddings

    def embed(self, role: str, texts: list[str]) -> EmbedResult:
        entries = self._entries(role)
        for e in entries:
            body = {"model": e.model, "input": texts}
            key = cache_key(e.provider, e.model, body, 0)
            raw = self.cache.get(key)
            if raw is not None:
                try:
                    vecs = self._parse_embed(raw, texts, e.dims)
                except _Bad:
                    continue
                self.ops.count(role, hit=True)
                return EmbedResult(vecs, key, e.provider, e.model, e.dims or 0, cached=True)
        self.ops.count(role, hit=False)

        def attempt(e: RoleEntry):
            body = {"model": e.model, "input": texts}
            key = cache_key(e.provider, e.model, body, 0)
            resp = yield ("post", "embeddings", body)
            try:
                vecs = self._parse_embed(resp.json(), texts, e.dims)
            except ValueError as err:
                raise _Bad(f"embedding response is not JSON: {err}", resp.text) from None
            self.cache.put(
                key=key,
                role=role,
                provider=e.provider,
                model=e.model,
                request_record={"request": body, "sent": None},
                response=resp.json(),
                template="embed",
                output_schema_version=0,
                usage=resp.json().get("usage"),
                latency_ms=None,
                now=self.clock(),
            )
            return EmbedResult(vecs, key, e.provider, e.model, e.dims or 0, cached=False)

        return self._run(role, entries, attempt)

    @staticmethod
    def _parse_embed(raw: dict, texts: list[str], dims: int | None) -> list[list[float]]:
        try:
            data = sorted(raw["data"], key=lambda d: d["index"])
            vecs = [[float(x) for x in d["embedding"]] for d in data]
        except (KeyError, TypeError, ValueError):
            raise _Bad("malformed embedding response", json.dumps(raw)) from None
        if len(vecs) != len(texts):
            raise _Bad(f"expected {len(texts)} vectors, got {len(vecs)}", "")
        for v in vecs:
            if dims is not None and len(v) != dims:
                raise _Bad(f"expected {dims} dims, got {len(v)}", "")
            if not all(math.isfinite(x) for x in v):
                raise _Bad("non-finite value in embedding", "")
        return vecs

    # ------------------------------------------------------------ the fallback loop

    def _run(self, role: str, entries: list[RoleEntry], attempt: Callable) -> Any:
        """Drive `attempt` (a generator yielding requests) over entries, per spec §3.4."""
        enabled = {(e.provider, e.model) for e in self.config.enabled_entries(role)}
        d = self.config.defaults
        attempts: list[str] = []
        resets: list[datetime] = []
        quota_hit = False

        for e in entries:
            ident = (e.provider, e.model)
            label = f"{e.provider}/{e.model}"
            if ident not in enabled or ident in self._disabled:
                continue
            until = self.ops.exhausted_until(e.provider, e.model, self.clock())
            if until is not None:
                quota_hit = True
                resets.append(until)
                attempts.append(f"{label}: exhausted until {until.isoformat()}")
                continue

            gen = attempt(e)
            request = next(gen)
            tries = 0  # retries used on this entry (429 / 5xx / timeouts)
            while True:
                _, path, body = request
                try:
                    resp = self._post(e, path, body)
                except httpx.TimeoutException as err:
                    outcome, info = "retry", f"timeout: {err}"
                except httpx.TransportError as err:
                    outcome, info = "retry", f"connection error: {err}"
                else:
                    outcome, info = self._classify(e, resp)
                now = self.clock()

                if outcome == "ok":
                    self.ops.record(
                        e.provider, e.model, "headers", at=now, detail=self._limit_headers(resp)
                    )
                    try:
                        request = gen.send(resp)
                    except StopIteration as stop:
                        return stop.value
                    except _Bad as bad:
                        attempts.append(f"{label}: {bad}")
                        break
                    continue  # the attempt asked for a repair request

                if outcome == "wait":
                    wait = info
                    if tries < d.max_retries_per_entry:
                        tries += 1
                        self.ops.record(
                            e.provider, e.model, "rate_limited", at=now, detail={"wait_s": wait}
                        )
                        self._sleep(wait)
                        continue
                    outcome, info = "exhausted", now + timedelta(seconds=wait)

                if outcome == "backoff":  # 429 with no usable wait hint
                    if tries < d.max_retries_per_entry:
                        tries += 1
                        self._sleep(min(2.0 ** (tries - 1), d.max_backoff_s))
                        continue
                    outcome, info = "exhausted", _next_utc_midnight(now)

                if outcome == "exhausted":
                    quota_hit = True
                    resets.append(info)
                    self.ops.record(e.provider, e.model, "exhausted", reset_at=info, at=now)
                    attempts.append(f"{label}: quota exhausted until {info.isoformat()}")
                    break

                if outcome == "retry":  # 5xx, timeout, connection error
                    if tries < d.max_retries_per_entry:
                        tries += 1
                        self._sleep(min(2.0 ** (tries - 1), d.max_backoff_s))
                        continue
                    self.ops.record(e.provider, e.model, "error", at=now, detail={"error": info})
                    attempts.append(f"{label}: {info} (retries exhausted)")
                    break

                if outcome == "next":  # e.g. context length: try a bigger model
                    attempts.append(f"{label}: {info}")
                    break

                if outcome == "disable":
                    self._disabled.add(ident)
                    self.ops.record(e.provider, e.model, "disabled", at=now, detail={"error": info})
                    attempts.append(f"{label}: disabled for this process ({info})")
                    break

                raise AssertionError(f"unhandled outcome {outcome}")

        if quota_hit:
            raise QuotaExhausted(role, min(resets) if resets else None, attempts)
        raise AllEntriesFailed(role, attempts or ["no enabled entries"])

    def _classify(self, e: RoleEntry, resp: httpx.Response) -> tuple[str, Any]:
        code = resp.status_code
        now = self.clock()
        if code == 200:
            return "ok", None
        text = resp.text[:2000]
        if code == 429:
            wait = retry_wait(resp, now)
            if _DAILY.search(text):
                reset = now + timedelta(seconds=wait) if wait else _next_utc_midnight(now)
                return "exhausted", reset
            if wait is None:
                return "backoff", None
            if wait <= self.config.defaults.max_backoff_s:
                return "wait", wait
            return "exhausted", now + timedelta(seconds=wait)
        if code >= 500:
            return "retry", f"HTTP {code}"
        if code == 400 and _CONTEXT.search(text):
            return "next", "context length exceeded"
        return "disable", f"HTTP {code}"

    @staticmethod
    def _limit_headers(resp: httpx.Response) -> dict:
        return {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
