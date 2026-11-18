"""Strict numeric-literal grammar used by the parser and by provenance check V24."""

import pytest

from themis.numbers import parse_literal


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("71.2", 71.2),
        ("8", 8),
        ("0", 0),
        (".5", 0.5),
        ("-3.1", -3.1),
        (chr(0x2212) + "3.1", -3.1),  # U+2212 MINUS SIGN
        ("+2", 2),
        ("1,024", 1024),
        ("1{,}024", 1024),
        ("12,345,678", 12345678),
        ("1e-4", 1e-4),
        ("2E+3", 2e3),
        ("3.5e10", 3.5e10),
        ("1\\times10^{-4}", 1e-4),
        ("2.5 \\times 10^{3}", 2.5e3),
        ("1\\times 10^-4", 1e-4),
        ("1{,}234.5", 1234.5),
    ],
)
def test_parses(text, value):
    assert parse_literal(text) == value


def test_int_stays_int():
    assert isinstance(parse_literal("8"), int)
    assert isinstance(parse_literal("8.0"), float)
    assert isinstance(parse_literal("1,024"), int)


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "71.2\\%",  # the percent sign is not part of the literal
        "71.2 \\pm 0.3",
        "71.2 0.3",
        "abc",
        "1,02",  # bad grouping
        "1,0245",
        "1.2.3",
        "--3",
        "e5",
        "1e",
        "nan",
        "inf",
        "∞",
        "$71.2$",
        " 71.2",  # surrounding whitespace is not part of a literal span
        "71.2 ",
        "٣",  # non-ASCII digit
    ],
)
def test_rejects(text):
    assert parse_literal(text) is None
