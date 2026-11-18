"""Repository settings that evidence depends on."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_fixtures_are_never_line_ending_converted():
    """Fixture byte offsets and hashes would break if git turned LF into CRLF on Windows."""
    out = subprocess.run(
        [
            "git",
            "check-attr",
            "text",
            "--",
            "tests/fixtures/http/x.body",
            "tests/fixtures/latex/golden/x/main.tex",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.count("text: unset") == 2, out


def test_committed_fixture_text_has_no_crlf():
    for p in (ROOT / "tests" / "fixtures").rglob("*.json"):
        assert b"\r\n" not in p.read_bytes(), p
