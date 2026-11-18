"""Invariant 3: canonical JSON is deterministic, strict and lossless."""

import datetime
import hashlib
from decimal import Decimal

import pytest

from themis.ledger.canonical import canonical_json, sha256_hex
from themis.ledger.errors import CanonicalizationError


def test_canonical_key_order_irrelevant():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_nested_key_order_irrelevant():
    a = {"x": {"b": [1, {"d": 1, "c": 2}], "a": None}}
    b = {"x": {"a": None, "b": [1, {"c": 2, "d": 1}]}}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_is_compact_utf8():
    assert canonical_json({"k": "é", "n": [1, 2]}) == '{"k":"é","n":[1,2]}'.encode()


def test_canonical_preserves_unicode_form():
    nfc = "é"  # é, composed
    nfd = "é"  # é, decomposed
    assert canonical_json({"t": nfc}) != canonical_json({"t": nfd})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_rejects_nan_inf(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json({"v": bad})


def test_canonical_negative_zero():
    assert canonical_json({"v": -0.0}) == canonical_json({"v": 0.0})


@pytest.mark.parametrize(
    "bad",
    [(1, 2), {1, 2}, datetime.datetime(2026, 1, 1), Decimal("1.5"), b"x", object()],
)
def test_canonical_rejects_non_json_types(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json({"v": bad})


def test_canonical_rejects_non_str_keys():
    with pytest.raises(CanonicalizationError):
        canonical_json({1: "a"})


def test_canonical_int_float_distinct():
    assert canonical_json({"v": 1}) != canonical_json({"v": 1.0})


def test_canonical_bool_not_int():
    assert canonical_json({"v": True}) != canonical_json({"v": 1})


def test_sha256_hex_matches_hashlib():
    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
