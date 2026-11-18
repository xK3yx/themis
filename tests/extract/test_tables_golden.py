"""The table parser against the hand-written goldens, case by case (M1 spec §5.3, §10.2).

IMPLEMENTED lists the cases whose parser behaviour exists. The parser is built test-first:
adding a case here turns its tests red until its handler is written. A case's test compares
only that case's fields, matching cells by coordinates. A golden's full comparison runs once
every case it needs is implemented. At the end all 20 are implemented and nothing is skipped.
"""

import pytest

from tests.extract.golden import CASES, Golden, load_goldens
from themis.extract import tables as T

IMPLEMENTED = ["C13", "C1", "C2", "C3", "C14", "C17"]

GOLDENS = load_goldens()
STRUCTURAL = {"C1", "C2", "C3", "C14", "C17", "C18", "C19"}


def implicit_cases(g: Golden) -> set[str]:
    """Structural cases a golden needs even when not tagged with them."""
    need = set()
    for t in g.tables:
        for c in t.cells:
            if c.colspan > 1:
                need.add("C1")
            if c.rowspan > 1:
                need.add("C2")
            if c.colspan > 1 and c.rowspan > 1:
                need.add("C3")
    return need | (set(g.cases) & STRUCTURAL)


def run_parser(g: Golden) -> list:
    macros = T.collect_macros("\n".join(g.macros))
    if "float" in g.expected:  # C18: the tabular lives in an \input file
        meta = g.meta["tables"][g.name]
        files = {
            meta["source_path"]: (g.dir / g.expected["float"]).read_bytes(),
            meta["input"]["source_path"]: g.tex,
        }
        return [
            t
            for t in T.parse_document(files, macros=macros)
            if t.source_path == meta["source_path"]
        ]
    return T.parse_tables(g.tex, macros=macros)


def _style(cell, key):
    v = cell.style.get(key)
    return v if v else None


def compare(g: Golden, aspects: set[str]) -> list[str]:
    got = run_parser(g)
    out = []
    if len(got) != len(g.tables):
        return [f"{g.id}: parsed {len(got)} tables, expected {len(g.tables)}"]
    for i, (e, p) in enumerate(zip(g.tables, got, strict=True)):
        w = f"{g.id} t{i}"
        grid = bool(aspects & ({"C13", "C20"} | STRUCTURAL))
        if grid:
            if (p.n_rows, p.n_cols) != (e.n_rows, e.n_cols):
                out.append(f"{w}: grid {p.n_rows}x{p.n_cols}, expected {e.n_rows}x{e.n_cols}")
            ek = {(c.row, c.col) for c in e.cells}
            pk = {(c.row, c.col) for c in p.cells}
            if ek != pk:
                out.append(
                    f"{w}: anchors missing {sorted(ek - pk)[:5]} extra {sorted(pk - ek)[:5]}"
                )
        if "C13" in aspects and [tuple(r[:2]) for r in p.rules] != e.rules:
            out.append(f"{w}: rules {[tuple(r[:2]) for r in p.rules]} != {e.rules}")
        if aspects & {"C14", "C18", "C19"}:
            cap = p.caption.text if p.caption else ""
            if e.caption_prefix and not cap.startswith(e.caption_prefix):
                out.append(f"{w}: caption {cap[:60]!r} lacks prefix {e.caption_prefix!r}")
            if p.label != e.label:
                out.append(f"{w}: label {p.label!r} != {e.label!r}")
        if "C19" in aspects and p.image_table != e.image_table:
            out.append(f"{w}: image_table {p.image_table} != {e.image_table}")
        pc = {(c.row, c.col): c for c in p.cells}
        for c in e.cells:
            q = pc.get((c.row, c.col))
            if q is None:
                continue
            cw = f"{w} ({c.row},{c.col})"
            if grid:
                qs = (q.span.start, q.span.end) if q.span else None
                if (q.raw, qs, q.rowspan, q.colspan) != (c.raw, c.span, c.rowspan, c.colspan):
                    out.append(
                        f"{cw}: {(q.raw, qs, q.rowspan, q.colspan)!r} != "
                        f"{(c.raw, c.span, c.rowspan, c.colspan)!r}"
                    )
            if "C17" in aspects and (q.pad, q.missing) != (c.pad, c.missing):
                out.append(f"{cw}: pad/missing {(q.pad, q.missing)!r} != {(c.pad, c.missing)!r}")
            if "C10" in aspects and q.missing != c.missing:
                out.append(f"{cw}: missing {q.missing!r} != {c.missing!r}")
            if "C9" in aspects:
                en = c.number
                qn = q.number
                ev = (en["value"], en.get("literal_span")) if en else None
                qv = (qn.value, (qn.span.start, qn.span.end)) if qn else None
                if ev != qv or (qn and en and type(qn.value) is not type(en["value"])):
                    out.append(f"{cw}: number {qv!r} != {ev!r}")
                if en and qn and bool(qn.percent) != bool(en.get("percent")):
                    out.append(f"{cw}: percent {qn.percent} != {en.get('percent')}")
            if "C8" in aspects and c.number and c.number.get("pm") is not None:
                qn = q.number
                got_pm = (
                    (qn.pm, (qn.pm_span.start, qn.pm_span.end) if qn.pm_span else None, qn.pm_style)
                    if qn
                    else None
                )
                exp_pm = (c.number["pm"], c.number.get("pm_literal_span"), c.number["pm_style"])
                if got_pm != exp_pm:
                    out.append(f"{cw}: pm {got_pm!r} != {exp_pm!r}")
            for case, key in (
                ("C5", "bold"),
                ("C5", "italic"),
                ("C6", "bold"),
                ("C6", "italic"),
                ("C7", "color"),
                ("C11", "arrow"),
                ("C12", "marks"),
            ):
                if case in aspects and _style(q, key) != (c.style.get(key) or None):
                    out.append(f"{cw}: {key} {_style(q, key)!r} != {c.style.get(key)!r}")
            if "C15" in aspects and c.text is not None and q.text != c.text:
                out.append(f"{cw}: text {q.text!r} != {c.text!r}")
        if "C4" in aspects and "col_headers" in g.expected["tables"][i]:
            et = g.expected["tables"][i]
            if p.header_rows != et["header_rows"]:
                out.append(f"{w}: header_rows {p.header_rows} != {et['header_rows']}")
            if p.col_headers != et["col_headers"]:
                out.append(f"{w}: col_headers {p.col_headers} != {et['col_headers']}")
    return out


def _goldens_for(case: str) -> list[Golden]:
    return [g for g in GOLDENS if case in g.cases]


@pytest.mark.parametrize("case", CASES)
def test_case(case):
    if case not in IMPLEMENTED:
        pytest.skip(f"{case} not implemented yet")
    if case == "C20":
        pytest.skip(
            "C20 (transposed tables) has no parser behaviour; its goldens are checked "
            "by the full comparison"
        )
    ready = [g for g in _goldens_for(case) if implicit_cases(g) <= set(IMPLEMENTED)]
    if not ready:
        pytest.skip(f"no golden for {case} has all its structural cases implemented yet")
    problems = [p for g in ready for p in compare(g, {case})]
    assert problems == []


@pytest.mark.parametrize("g", GOLDENS, ids=[g.id for g in GOLDENS])
def test_full(g):
    needed = (set(g.cases) | implicit_cases(g)) - {"C20"}
    if not needed <= set(IMPLEMENTED):
        pytest.skip(f"needs {sorted(needed - set(IMPLEMENTED))}")
    aspects = set(CASES)
    assert compare(g, aspects) == []


def test_handlers_registered_for_implemented_cases():
    for case in IMPLEMENTED:
        if case not in ("C13", "C20"):
            assert case in T.HANDLERS, f"{case} is listed as implemented but has no handler"


def test_parser_version():
    assert T.PARSER_VERSION
