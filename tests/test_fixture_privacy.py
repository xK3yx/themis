"""No committed fixture may carry a secret or an email address (M1 spec §10.1)."""

import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FILES = sorted(p for p in FIXTURES.rglob("*") if p.is_file() and p.suffix != ".sqlite3")

PATTERNS = {
    "authorization header": re.compile(rb"(?i)\bauthorization\s*[:=]"),
    "bearer token": re.compile(rb"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    "key-like param": re.compile(rb"(?i)\b(?:api_?key|key|token|mailto)=[A-Za-z0-9_\-.%@]{16,}"),
    "provider key": re.compile(rb"\b(?:sk|gsk|hf|jina)_[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9\-]{16,}"),
    "email address": re.compile(
        rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
    ),
}

# Public addresses that legitimately appear in recorded API responses.
ALLOWED_EMAILS = {b"help@arxiv.org", b"support@openalex.org", b"team@ourresearch.org"}


def test_fixtures_exist():
    assert FILES, "no fixtures found"


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_no_secrets_in_fixture(path):
    data = path.read_bytes()
    for label, pattern in PATTERNS.items():
        hits = {m.group(0) for m in pattern.finditer(data)}
        if label == "email address":
            hits -= ALLOWED_EMAILS
        assert not hits, f"{label} in {path.name}: {sorted(hits)[:3]}"


def test_scanner_catches_planted_secrets():
    planted = [
        b"Authorization: Bearer abcdefghijklmnopqrstuv",
        b"?mailto=someone.private@example.org",
        b'"key": "sk-abcdefghijklmnopqrstuvwxyz"',
        b"https://x/?api_key=abcdefghijklmnop1234",
    ]
    for blob in planted:
        assert any(p.search(blob) for p in PATTERNS.values()), blob
