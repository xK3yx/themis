"""Strict grammar for a single numeric literal, as it appears in LaTeX source.

Used by the table parser to read cell values and by the ledger's provenance check (V24),
which re-parses the exact bytes a claim's number came from. A literal span covers only the
number: never `\\%`, `\\pm`, `$`, or surrounding whitespace.

    literal  := sign? mantissa exponent?
    sign     := "-" | "+" | MINUS SIGN (U+2212)
    mantissa := int ("." digits)? | "." digits
    int      := digits | d{1,3} ("," | "{,}") ddd ...    (grouping must be exact)
    exponent := ("e" | "E") sign? digits
              | "\\times" " "* "10^" ( "{" sign? digits "}" | sign? digits )

Returns an int when there is no fraction and no exponent, else a float. Anything else is None.
"""

import re

MINUS = chr(0x2212)  # MINUS SIGN, common in typeset numbers
_SIGN = rf"[-+{MINUS}]"
_GROUPED = r"\d{1,3}(?:(?:,|\{,\})\d{3})+"
_MANTISSA = rf"(?:(?:{_GROUPED}|\d+)(?:\.\d+)?|\.\d+)"
_EXP_E = rf"[eE](?P<e>{_SIGN}?\d+)"
_EXP_TIMES = rf"\s*\\times\s*10\^(?:\{{(?P<t1>{_SIGN}?\d+)\}}|(?P<t2>{_SIGN}?\d+))"
_LITERAL = re.compile(
    rf"(?P<sign>{_SIGN})?(?P<m>{_MANTISSA})(?:{_EXP_E}|{_EXP_TIMES})?",
    re.ASCII,
)


LITERAL_PATTERN = _LITERAL.pattern  # for scanners (e.g. the table parser) that search text


def _norm_sign(s: str) -> str:
    return s.replace(MINUS, "-")


def parse_literal(text: str) -> int | float | None:
    """Parse exactly one numeric literal (the whole string), or return None."""
    m = _LITERAL.fullmatch(text)
    if m is None:
        return None
    mantissa = m.group("m").replace("{,}", "").replace(",", "")
    sign = -1 if m.group("sign") in ("-", MINUS) else 1
    exp = m.group("e") or m.group("t1") or m.group("t2")
    if exp is None and "." not in mantissa:
        return sign * int(mantissa)
    value = float(mantissa)
    if exp is not None:
        value = float(f"{mantissa}e{_norm_sign(exp)}")
    return sign * value
