"""LLM call cache, stored in the ledger because it is provenance (M1 spec §3.5).

key = sha256(canonical {provider, model, request body, output_schema_version}). The request
body holds everything that affects output (messages, temperature, response_format) and
never any auth header.
"""

import json
from datetime import datetime
from typing import Any

from themis.ledger.canonical import canonical_json, content_hash
from themis.ledger.dao import Ledger
from themis.ledger.db import tx


def cache_key(provider: str, model: str, request_body: dict, output_schema_version: int) -> str:
    return content_hash(
        {
            "provider": provider,
            "model": model,
            "request": request_body,
            "output_schema_version": output_schema_version,
        }
    )


class LlmCache:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def get(self, key: str) -> dict | None:
        """The stored raw provider response for `key`, or None."""
        row = self.ledger.conn.execute(
            "SELECT response_blob FROM llm_calls WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(self.ledger.blobs.get(row[0]))

    def put(
        self,
        *,
        key: str,
        role: str,
        provider: str,
        model: str,
        request_record: dict,
        response: Any,
        template: str,
        output_schema_version: int,
        usage: dict | None,
        latency_ms: int | None,
        now: datetime,
    ) -> None:
        req_blob = self.ledger.blobs.put(canonical_json(request_record), "application/json")
        resp_blob = self.ledger.blobs.put(canonical_json(response), "application/json")
        usage = usage or {}
        with tx(self.ledger.conn):
            exists = self.ledger.conn.execute(
                "SELECT 1 FROM llm_calls WHERE key = ?", (key,)
            ).fetchone()
            if exists:
                return
            self.ledger.conn.execute(
                "INSERT INTO llm_calls(key, role, provider, model, request_blob, response_blob,"
                " prompt_template, output_schema_version, prompt_tokens, completion_tokens,"
                " latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    role,
                    provider,
                    model,
                    req_blob,
                    resp_blob,
                    template,
                    output_schema_version,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    latency_ms,
                    now.isoformat(),
                ),
            )
