"""Golden table fixtures: loading, expansion and parser-independent checks (M1 spec §10.2).

Per paper (or synthetic set), under tests/fixtures/latex/golden/<dir>/:
    meta.json              provenance: arxiv id, version, licence, per-table source slices,
                           macro-definition lines (C15); or {"synthetic": true, ...}
    oai.body               recorded arXiv OAI-PMH response (licence evidence), real only
    <name>.tex             exact byte slice of the original file (the table float)
    <name>.input.tex       for C18: the \\input file holding the tabular, copied whole
    <name>.expected.json   HAND-WRITTEN expected parse, in a compact row format:

{"fixture": "<file holding the tabular>", "cases": ["C1", ...],
 "tables": [{"start_after": "<anchor text>", "n_rows": R, "n_cols": C,
             "rules": [[after_row, kind], ...], "caption_prefix": "...", "label": "...",
             "rows": [[cell, ...], ...]}]}

cell := "raw"                      no number
      | ["raw", value]             number whose literal is the whole raw text
      | {"raw", "num", "lit", "pm", "pm_lit", "pm_style", "pct", "missing", "style",
         "cs", "rs", "after", "text", "pad"}
"" is an empty cell: covered (not an anchor) if a row span from above covers it, otherwise
an empty anchor. Coordinates follow source order; byte spans are located by in-order search
of the hand-written raw strings, never by the parser.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from themis.ingest.arxiv import is_redistributable, parse_oai
from themis.numbers import parse_literal

ROOT = Path(__file__).parents[1] / "fixtures" / "latex" / "golden"
CASES = [f"C{i}" for i in range(1, 21)]
EVAL_PAPERS = {"2106.09685", "2305.14314", "2303.10512", "2402.09353", "2405.09673"}
SEPARATOR_HINTS = ("&", "\\\\", "{", "\\tabularnewline")


@dataclass
class Cell:
    row: int
    col: int
    rowspan: int
    colspan: int
    raw: str
    span: tuple[int, int] | None
    number: dict | None
    missing: str | None
    style: dict
    text: str | None = None
    pad: bool = False


@dataclass
class Table:
    n_rows: int
    n_cols: int
    cells: list[Cell]
    rules: list[tuple[int, str]]
    caption_prefix: str | None
    label: str | None
    problems: list[str] = field(default_factory=list)
    image_table: bool = False


@dataclass
class Golden:
    dir: Path
    name: str
    meta: dict
    expected: dict
    tex: bytes  # the file holding the tabular(s)
    tables: list[Table]

    @property
    def id(self) -> str:
        return f"{self.dir.name}/{self.name}"

    @property
    def synthetic(self) -> bool:
        return bool(self.meta.get("synthetic"))

    @property
    def cases(self) -> list[str]:
        return self.expected.get("cases", [])

    @property
    def macros(self) -> list[str]:
        return [m["text"] for m in self.meta.get("macros", [])]


def _cell_spec(c) -> dict:
    if isinstance(c, str):
        return {"raw": c}
    if isinstance(c, list):
        return {"raw": c[0], "num": c[1]}
    return dict(c)


def expand(tex: bytes, t: dict) -> Table:
    """Hand-written compact rows -> cells with coordinates and located byte spans."""
    problems: list[str] = []
    cells: list[Cell] = []
    covered: set[tuple[int, int]] = set()
    anchor = t.get("start_after", "")
    cursor = tex.find(anchor.encode()) + len(anchor.encode()) if anchor else 0
    if anchor and tex.find(anchor.encode()) < 0:
        problems.append(f"start_after {anchor!r} not found")
    prev_end = None
    for r, row in enumerate(t["rows"]):
        c = 0
        for spec in map(_cell_spec, row):
            raw = spec.get("raw", "")
            rs, cs = spec.get("rs", 1), spec.get("cs", 1)
            if raw == "" and (r, c) in covered and not spec.get("pad"):
                c += cs  # placeholder under a row span from above: occupies columns, no anchor
                continue
            span = None
            if raw:
                if spec.get("after"):
                    a = tex.find(spec["after"].encode(), cursor)
                    if a < 0:
                        problems.append(f"({r},{c}) after-hint {spec['after']!r} not found")
                    else:
                        cursor = a + len(spec["after"].encode())
                s = tex.find(raw.encode(), cursor)
                if s < 0:
                    problems.append(f"({r},{c}) raw {raw!r} not found after byte {cursor}")
                else:
                    e = s + len(raw.encode())
                    if prev_end is not None:
                        gap = tex[prev_end:s].decode("utf-8", errors="replace")
                        if not any(h in gap for h in SEPARATOR_HINTS):
                            problems.append(f"({r},{c}) suspicious gap {gap!r} before {raw!r}")
                    span, cursor, prev_end = (s, e), e, e
            number = None
            if "num" in spec:
                number = {
                    "value": spec["num"],
                    "pm": spec.get("pm"),
                    "percent": spec.get("pct", False),
                    "literal": spec.get("lit", raw),
                    "pm_literal": spec.get("pm_lit"),
                    "pm_style": spec.get("pm_style"),
                }
                if span:
                    _locate_literals(tex, span, number, (r, c), problems)
            missing = spec.get("missing", "empty" if raw == "" else None)
            cells.append(
                Cell(
                    r,
                    c,
                    rs,
                    cs,
                    raw,
                    span,
                    number,
                    missing,
                    spec.get("style", {}),
                    spec.get("text"),
                    bool(spec.get("pad")),
                )
            )
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        covered.add((r + dr, c + dc))
            c += cs
    rules = [(a, k) for a, k in t.get("rules", [])]
    return Table(
        t["n_rows"],
        t["n_cols"],
        cells,
        rules,
        t.get("caption_prefix"),
        t.get("label"),
        problems,
        bool(t.get("image_table")),
    )


def _locate_literals(tex: bytes, span, number: dict, where, problems: list[str]) -> None:
    s, e = span
    ls = tex.find(number["literal"].encode(), s, e)
    if ls < 0:
        problems.append(f"{where} literal {number['literal']!r} not inside the cell")
        return
    number["literal_span"] = (ls, ls + len(number["literal"].encode()))
    if number.get("pm_literal") is not None:
        ps = tex.find(number["pm_literal"].encode(), number["literal_span"][1], e)
        if ps < 0:
            problems.append(f"{where} pm literal {number['pm_literal']!r} not after the value")
        else:
            number["pm_literal_span"] = (ps, ps + len(number["pm_literal"].encode()))


def load_goldens() -> list[Golden]:
    out = []
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        for exp in sorted(d.glob("*.expected.json")):
            name = exp.name[: -len(".expected.json")]
            expected = json.loads(exp.read_text(encoding="utf-8"))
            tex = (d / expected["fixture"]).read_bytes()
            tables = [expand(tex, t) for t in expected["tables"]]
            out.append(Golden(d, name, meta, expected, tex, tables))
    return out


def licence_problems(meta: dict, d: Path) -> list[str]:
    """Why a fixture directory is not committable, or [] if it is."""
    if meta.get("synthetic"):
        return [] if meta.get("license") == "synthetic" else ["synthetic set must say so"]
    problems = []
    if meta.get("arxiv_id") in EVAL_PAPERS:
        problems.append(f"{meta.get('arxiv_id')} is an eval paper and must never be committed")
    if not is_redistributable(meta.get("license")):
        problems.append(f"licence {meta.get('license')!r} is not CC BY, CC BY-SA or CC0")
    oai = d / "oai.body"
    if not oai.exists():
        problems.append("missing recorded oai.body (licence evidence)")
    else:
        rec = parse_oai(oai.read_bytes())
        if rec.arxiv_id != meta.get("arxiv_id"):
            problems.append(f"oai.body is for {rec.arxiv_id}, not {meta.get('arxiv_id')}")
        if rec.license != meta.get("license"):
            problems.append(f"meta licence {meta.get('license')!r} != recorded {rec.license!r}")
    return problems


def byte_problems(g: Golden) -> list[str]:
    """The mechanical check: spans located, separators between cells, every number literal
    inside its cell and re-parsing to the hand-written value."""
    out = []
    for t_i, table in enumerate(g.tables):
        out += [f"{g.id} table {t_i}: {p}" for p in table.problems]
        for c in table.cells:
            where = f"{g.id} table {t_i} cell ({c.row},{c.col})"
            if c.span:
                s, e = c.span
                if g.tex[s:e].decode("utf-8") != c.raw:
                    out.append(f"{where}: span bytes != raw")
            n = c.number
            if not n:
                continue
            for key, _lit, lspan in (
                ("value", "literal", "literal_span"),
                ("pm", "pm_literal", "pm_literal_span"),
            ):
                if n.get(key) is None:
                    continue
                ls = n.get(lspan)
                if not ls:
                    out.append(f"{where}: no {lspan}")
                    continue
                text = g.tex[ls[0] : ls[1]].decode("utf-8")
                got = parse_literal(text)
                if got != n[key] or type(got) is not type(n[key]):
                    out.append(f"{where}: {text!r} parses to {got!r}, expected {n[key]!r}")
        if not table.image_table and (table.n_rows < 1 or table.n_cols < 1):
            out.append(f"{g.id} table {t_i}: empty grid")
        for c in table.cells:
            if c.row + c.rowspan > table.n_rows or c.col + c.colspan > table.n_cols:
                out.append(
                    f"{g.id} table {t_i}: cell ({c.row},{c.col}) outside "
                    f"{table.n_rows}x{table.n_cols}"
                )
    return out
