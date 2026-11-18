"""Canonical JSON and hashing.

Canonical form: sorted keys, no whitespace, UTF-8, no NaN/inf, -0.0 -> 0.0.
Strings are kept byte-exact (no Unicode normalisation) because span text must equal
the source bytes it was taken from.
"""

import hashlib
import json
import math
from typing import Any

from themis.ledger.errors import CanonicalizationError


def _check(value: Any, path: str) -> Any:
    # bool is a subclass of int; check it first so it is never coerced.
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"non-finite float at {path}")
        return 0.0 if value == 0.0 else value
    if isinstance(value, list):
        return [_check(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string key {k!r} at {path}")
            out[k] = _check(v, f"{path}.{k}")
        return out
    raise CanonicalizationError(f"unsupported type {type(value).__name__} at {path}")


def canonical_json(value: Any) -> bytes:
    """Serialise ``value`` to canonical JSON bytes, or raise CanonicalizationError."""
    checked = _check(value, "$")
    return json.dumps(
        checked, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """sha256 of the canonical JSON of ``value``."""
    return sha256_hex(canonical_json(value))
