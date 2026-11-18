"""Mechanical LaTeX table parser (M1 spec §5). No LLM.

Every position is a BYTE offset into the file it came from, so cell spans can be cited as
ledger provenance (TableCell locator) and checked against the blob.

The parser core handles rows, cells, rules and placement (case C13). Each other case in
§5.3 is a handler registered in HANDLERS under its case id; unimplemented or disabled
handlers are no-ops. The mutation check (scripts/mutation_check.py) disables one handler
at a time and requires the golden tests to fail.

Conventions (also in docs/m1-ingest-extract.md §5.5):
  - comment lines and rule-only lines are not rows; rules are recorded as "after row N"
    (-1 = above the first row); \\rowcolor / \\arrayrulecolor at a row start belong to the row;
  - a cell is the trimmed text between separators;
  - a cell has a number only if a literal starts it (after style wrappers) and is followed
    only by an allowed suffix (+-, \\%, footnote marks, closing wrappers, a bracketed note).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import accumulate

PARSER_VERSION = "1"

HANDLERS: dict[str, Callable] = {}


def handler(case: str) -> Callable:
    def register(fn: Callable) -> Callable:
        HANDLERS[case] = fn
        return fn

    return register


def _noop(*args, **kwargs):
    return None


def _h(case: str) -> Callable:
    return HANDLERS.get(case, _noop)


# ---------------------------------------------------------------- data


@dataclass(frozen=True)
class Span:
    start: int  # byte offsets
    end: int
    text: str


@dataclass
class Number:
    value: int | float
    span: Span
    pm: int | float | None = None
    pm_span: Span | None = None
    pm_style: str | None = None
    percent: bool = False


@dataclass
class Cell:
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    span: Span | None = None
    raw: str = ""
    text: str = ""
    style: dict = field(default_factory=dict)
    number: Number | None = None
    missing: str | None = None
    pad: bool = False
    # working fields: char offsets of the content in the document, and the row's colour
    _start: int = 0
    _end: int = 0
    _row_color: str | None = None


@dataclass
class ParsedTable:
    source_path: str  # file holding the float (or the bare tabular)
    tabular_path: str  # file holding the tabular (differs for \input, case C18)
    env: str
    colspec: str
    table_span: Span | None
    tabular_span: Span | None
    caption: Span | None
    label: str | None
    n_rows: int
    n_cols: int
    rules: list[tuple[int, str]]
    cells: list[Cell]
    header_rows: list[int] = field(default_factory=list)
    col_headers: list[list[str]] = field(default_factory=list)
    image_table: bool = False
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- document


class Doc:
    """Decoded text plus a comment-masked copy of equal length and char->byte offsets."""

    def __init__(self, data: bytes, path: str = "main.tex"):
        self.path = path
        self.text = data.decode("utf-8", errors="surrogateescape")
        self.masked = _mask_comments(self.text)
        self._b = [0, *accumulate(len(ch.encode("utf-8", "surrogateescape")) for ch in self.text)]

    def byte(self, i: int) -> int:
        return self._b[i]

    def span(self, i: int, j: int) -> Span:
        return Span(self._b[i], self._b[j], self.text[i:j])


def _mask_comments(text: str) -> str:
    """Replace each unescaped % ... end-of-line with spaces, keeping every offset."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _group(m: str, i: int) -> int:
    """Index just past the brace group starting at m[i] == '{' (or i if not a group)."""
    if i >= len(m) or m[i] != "{":
        return i
    depth = 0
    j = i
    while j < len(m):
        ch = m[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(m)


def _opt(m: str, i: int) -> int:
    """Skip one optional [...] argument (with surrounding spaces) starting at i."""
    j = i
    while j < len(m) and m[j] in " \t\r\n":
        j += 1
    if j < len(m) and m[j] == "[":
        depth = 0
        while j < len(m):
            if m[j] == "[":
                depth += 1
            elif m[j] == "]":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
    return i


def _skip_ws(m: str, i: int) -> int:
    while i < len(m) and m[i] in " \t\r\n":
        i += 1
    return i


def _find_env(m: str, name: str, start: int = 0) -> list[tuple[int, int, int, int]]:
    """(begin_start, content_start, content_end, end_end) for each \\begin{name}..\\end{name}."""
    out = []
    b_tok, e_tok = f"\\begin{{{name}}}", f"\\end{{{name}}}"
    i = start
    while True:
        b = m.find(b_tok, i)
        if b < 0:
            return out
        depth, j = 1, b + len(b_tok)
        while depth:
            nb, ne = m.find(b_tok, j), m.find(e_tok, j)
            if ne < 0:
                return out
            if 0 <= nb < ne:
                depth, j = depth + 1, nb + len(b_tok)
            else:
                depth, j = depth - 1, ne + len(e_tok)
        out.append((b, b + len(b_tok), j - len(e_tok), j))
        i = j


# ---------------------------------------------------------------- macros


def collect_macros(text: str) -> dict[str, str]:
    """Zero-argument macro definitions (\\newcommand{\\x}{body}, \\newcommand\\x{body},
    \\def\\x{body}), as {name: body}."""
    out: dict[str, str] = {}
    m = _mask_comments(text)
    pat = re.compile(
        r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z]+)\}?\s*(\[\d\])?\s*\{|\\def\\([A-Za-z]+)\s*\{"
    )
    for mt in pat.finditer(m):
        if mt.group(2):
            continue  # takes arguments
        name = mt.group(1) or mt.group(3)
        start = mt.end() - 1
        end = _group(m, start)
        out[name] = text[start + 1 : end - 1]
    return out


# ---------------------------------------------------------------- context


@dataclass
class Ctx:
    """Per-parse settings handlers may extend (e.g. C14 adds environments)."""

    tabular_envs: set[str] = field(default_factory=lambda: {"tabular"})
    float_envs: set[str] = field(default_factory=lambda: {"table"})
    macros: dict[str, str] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    consumed_inputs: set[str] = field(default_factory=set)


# ---------------------------------------------------------------- public API


def parse_tables(
    data: bytes,
    *,
    path: str = "main.tex",
    macros: dict[str, str] | None = None,
    files: dict[str, bytes] | None = None,
) -> list[ParsedTable]:
    """Every table in one file, in source order. Never raises on malformed LaTeX."""
    ctx = Ctx(macros=dict(macros or {}), files=dict(files or {}))
    _h("C14")(ctx)
    return _parse_doc(Doc(data, path), ctx)


def parse_document(
    files: dict[str, bytes], *, macros: dict[str, str] | None = None
) -> list[ParsedTable]:
    """Every table in a multi-file source. Files pulled in by a float's \\input are parsed as
    part of that float (C18), not again on their own."""
    ctx = Ctx(macros=dict(macros or {}), files=dict(files))
    _h("C14")(ctx)
    out: list[ParsedTable] = []
    for path in sorted(files):
        out += _parse_doc(Doc(files[path], path), ctx)
    return [t for t in out if t.source_path not in ctx.consumed_inputs]


# ---------------------------------------------------------------- floats and tabulars


def _parse_doc(doc: Doc, ctx: Ctx) -> list[ParsedTable]:
    m = doc.masked
    floats = sorted((f for env in ctx.float_envs for f in _find_env(m, env)), key=lambda f: f[0])
    tabs = sorted(
        ((env, t) for env in ctx.tabular_envs for t in _find_env(m, env)), key=lambda x: x[1][0]
    )
    out: list[ParsedTable] = []
    used: set[int] = set()
    for f in floats:
        inside = [(env, t) for env, t in tabs if f[1] <= t[0] and t[3] <= f[2]]
        caption, label = _caption_label(doc, f[1], f[2])
        if not inside:
            extra = _h("C18")(doc, ctx, f, caption, label) or _h("C19")(doc, ctx, f, caption, label)
            if extra:
                out += extra
            continue
        for env, t in inside:
            used.add(t[0])
            pt = _parse_tabular(doc, ctx, env, t)
            pt.table_span = doc.span(f[0], f[3])
            pt.caption, pt.label = caption, label
            out.append(pt)
    for env, t in tabs:  # bare tabulars outside any float
        if t[0] not in used and not any(f[0] <= t[0] < f[3] for f in floats):
            out.append(_parse_tabular(doc, ctx, env, t))
    return out


def _caption_label(doc: Doc, start: int, end: int) -> tuple[Span | None, str | None]:
    m = doc.masked
    caption = None
    mt = re.compile(r"\\caption(?:of\{[^}]*\})?\s*(\[[^\]]*\])?\s*\{").search(m, start, end)
    if mt:
        g_end = _group(m, mt.end() - 1)
        caption = doc.span(mt.end(), g_end - 1)
    label = None
    ml = re.compile(r"\\label\s*\{([^}]*)\}").search(m, start, end)
    if ml:
        label = ml.group(1)
    return caption, label


_TAB_ARGS = {"tabular": 1, "tabular*": 2, "tabularx": 2, "tabulary": 2, "longtable": 1}


def _parse_tabular(doc: Doc, ctx: Ctx, env: str, t: tuple[int, int, int, int]) -> ParsedTable:
    m = doc.masked
    i = _opt(m, t[1])
    spec_start = spec_end = i
    for _ in range(_TAB_ARGS.get(env, 1)):
        i = _skip_ws(m, i)
        spec_start, spec_end = i, _group(m, i)
        i = spec_end
    colspec = doc.text[spec_start + 1 : spec_end - 1] if spec_end > spec_start else ""
    pt = ParsedTable(
        source_path=doc.path,
        tabular_path=doc.path,
        env=env,
        colspec=colspec,
        table_span=None,
        tabular_span=doc.span(t[0], t[3]),
        caption=None,
        label=None,
        n_rows=0,
        n_cols=0,
        rules=[],
        cells=[],
    )
    rows = _split_rows(doc, ctx, pt, i, t[2])
    if env == "longtable" and _enabled("C14"):
        rows = _drop_longtable_repeats(doc, rows, i, t[2])
        pt.caption, pt.label = _caption_label(doc, t[1], t[2])
    _place(doc, pt, rows)
    _h("C17")(pt)
    for cell in pt.cells:
        _analyse_cell(doc, ctx, pt, cell)
    _h("C6")(pt)
    _h("C4")(pt)
    return pt


# ---------------------------------------------------------------- rows and cells

_RULE = re.compile(
    r"\\(?P<kind>hline|toprule|midrule|bottomrule)(?:\[[^\]]*\])?"
    r"|\\(?P<kind2>cline)\{[^}]*\}"
    r"|\\(?P<kind3>cmidrule)(?:\[[^\]]*\])?(?:\([^)]*\))?\{[^}]*\}"
    r"|\\(?P<kind4>specialrule)\{[^}]*\}\{[^}]*\}\{[^}]*\}"
)
_ROW_CMD = re.compile(
    r"\\rowcolor(?:\[[^\]]*\])?\{(?P<color>[^}]*)\}(?:\[[^\]]*\])?"
    r"|\\arrayrulecolor(?:\[[^\]]*\])?\{[^}]*\}"
    r"|\\addlinespace(?:\[[^\]]*\])?"
    r"|\\hhline\{[^}]*\}"
    r"|\\end(?:first)?head\b|\\end(?:last)?foot\b"
)


def _split_rows(doc: Doc, ctx: Ctx, pt: ParsedTable, start: int, end: int):
    """Split a tabular body into rows of (cell_start, cell_end) char ranges.

    Returns [(row_color, [(s, e), ...]), ...]; records rules on `pt`.
    """
    m = doc.masked
    cells: list[tuple[int, int]] = []
    cell_start = start
    depth = 0
    i = start
    rows_raw: list[list[tuple[int, int]]] = []
    while i < end:
        ch = m[i]
        if ch == "\\":
            if m.startswith("\\\\", i) and depth == 0:
                cells.append((cell_start, i))
                rows_raw.append(cells)
                j = i + 2
                if j < end and m[j] == "*":
                    j += 1
                j = _opt(m, j)
                cells, cell_start, i = [], j, j
                continue
            if m.startswith("\\tabularnewline", i) and depth == 0:
                cells.append((cell_start, i))
                rows_raw.append(cells)
                j = _opt(m, i + len("\\tabularnewline"))
                cells, cell_start, i = [], j, j
                continue
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "&" and depth == 0:
            cells.append((cell_start, i))
            cell_start = i + 1
        i += 1
    cells.append((cell_start, end))
    rows_raw.append(cells)

    out = []
    for raw_cells in rows_raw:
        s0, e0 = raw_cells[0]
        j, color = s0, None
        while True:
            j = _skip_ws(m, j)
            mr = _RULE.match(m, j, e0)
            if mr:
                kind = (
                    mr.group("kind") or mr.group("kind2") or mr.group("kind3") or mr.group("kind4")
                )
                pt.rules.append((len(out) - 1, kind))
                j = mr.end()
                continue
            mc = _ROW_CMD.match(m, j, e0)
            if mc:
                if mc.group("color"):
                    color = mc.group("color").strip()
                j = mc.end()
                continue
            break
        raw_cells = [(j, e0), *raw_cells[1:]]
        if len(raw_cells) == 1 and not m[j:e0].strip():
            continue  # rules / commands only: not a row
        out.append((color, raw_cells))
    return out


@handler("C14")
def _environments(ctx: Ctx) -> None:
    """Tabular variants and floats beyond plain tabular/table. Wrappers such as \\resizebox,
    \\scalebox and adjustbox need nothing: tabulars are found wherever they sit in a float,
    and several tabulars in one float each become a table with the float's caption."""
    ctx.tabular_envs |= {"tabular*", "tabularx", "tabulary", "longtable"}
    ctx.float_envs |= {"table*", "sidewaystable", "sidewaystable*", "wraptable"}


@handler("C17")
def _pad_short_rows(pt: ParsedTable) -> None:
    """A row shorter than the table, where nothing covers the gap, gets empty padding cells
    (no span: they are not in the source) and a warning."""
    covered = getattr(pt, "_covered", set())
    added = []
    for r in range(pt.n_rows):
        for c in range(pt.n_cols):
            if (r, c) not in covered:
                added.append(Cell(row=r, col=c, pad=True, missing="empty"))
    if added:
        pt.warnings.append(f"padded {len(added)} cell(s) in short rows")
        pt.cells = sorted(pt.cells + added, key=lambda x: (x.row, x.col))


def _resolve_input(ctx: Ctx, from_path: str, name: str) -> str | None:
    """Find an \\input target among the source files (relative to the root or the file)."""
    name = name.strip()
    base = from_path.rsplit("/", 1)[0] + "/" if "/" in from_path else ""
    for cand in (name, name + ".tex", base + name, base + name + ".tex"):
        cand = cand.lstrip("./") if cand.startswith("./") else cand
        if cand in ctx.files:
            return cand
    return None


@handler("C18")
def _input_tables(doc: Doc, ctx: Ctx, f, caption, label) -> list[ParsedTable] | None:
    """A float whose tabular comes from \\input{file} / \\include{file}: parse the tabular in
    that file (spans refer to its bytes); table span, caption and label come from the float."""
    m = doc.masked
    out: list[ParsedTable] = []
    for mt in re.finditer(r"\\(?:input|include)\s*\{([^}]*)\}", m[f[1] : f[2]]):
        target = _resolve_input(ctx, doc.path, mt.group(1))
        if target is None:
            continue
        idoc = Doc(ctx.files[target], target)
        tabs = sorted(
            ((env, t) for env in ctx.tabular_envs for t in _find_env(idoc.masked, env)),
            key=lambda x: x[1][0],
        )
        for env, t in tabs:
            pt = _parse_tabular(idoc, ctx, env, t)
            pt.source_path = doc.path
            pt.table_span = doc.span(f[0], f[3])
            pt.caption, pt.label = caption, label
            out.append(pt)
        ctx.consumed_inputs.add(target)
    return out or None


@handler("C19")
def _image_table(doc: Doc, ctx: Ctx, f, caption, label) -> list[ParsedTable] | None:
    """A table float holding only an image: recorded with zero cells and a warning, so the
    pipeline can skip it explicitly instead of losing it."""
    if "\\includegraphics" not in doc.masked[f[1] : f[2]]:
        return None
    return [ParsedTable(
        source_path=doc.path, tabular_path=doc.path, env="image", colspec="",
        table_span=doc.span(f[0], f[3]), tabular_span=None, caption=caption, label=label,
        n_rows=0, n_cols=0, rules=[], cells=[], image_table=True, warnings=["image_table"],
    )]


def _drop_longtable_repeats(doc: Doc, rows, start: int, end: int):
    """In a longtable, rows between \\endfirsthead and \\endhead repeat the header on later
    pages: drop them (and the repeated foot before \\endlastfoot)."""
    m = doc.masked
    fh, h = m.find("\\endfirsthead", start, end), m.find("\\endhead", start, end)
    if fh < 0 or h < 0:
        return rows
    return [r for r in rows if not (fh < r[1][0][0] <= h)]


def _unwrap(doc: Doc, cell: Cell, name: str, n_args_before: int, opts: bool) -> int | None:
    """If the cell is exactly \\name[opt]{a1}..{content}, narrow it to content and return the
    first argument as an int; otherwise None. Optional [..] args are skipped when `opts`."""
    m = doc.masked
    s, e = cell._start, cell._end
    tok = "\\" + name
    if not m.startswith(tok, s) or (s + len(tok) < e and m[s + len(tok)].isalpha()):
        return None
    j = s + len(tok)
    first = None
    for k in range(n_args_before):
        j = _opt(m, j) if opts else j
        j = _skip_ws(m, j)
        g = _group(m, j)
        if g == j:
            return None
        if k == 0:
            first = doc.text[j + 1 : g - 1].strip()
        j = g
    j = _opt(m, j) if opts else j
    j = _skip_ws(m, j)
    g = _group(m, j)
    if g == j or m[g:e].strip():
        return None  # not a whole-cell wrapper
    try:
        n = int(first)
    except (TypeError, ValueError):
        return None
    cell._start, cell._end = _trim(m, j + 1, g - 1)
    return n


@handler("C1")
def _multicolumn(doc: Doc, cell: Cell) -> None:
    """\\multicolumn{n}{spec}{content}: colspan n; the cell's span is the content."""
    n = _unwrap(doc, cell, "multicolumn", 2, opts=False)
    if n is not None and n >= 1:
        cell.colspan = n


@handler("C2")
def _multirow(doc: Doc, cell: Cell) -> None:
    """\\multirow[pos]{n}[bigstruts]{width}[fixup]{content}: rowspan |n|; span = content.
    A negative n spans upward in LaTeX; it is recorded as rowspan |n| from this row."""
    n = _unwrap(doc, cell, "multirow", 2, opts=True)
    if n is not None and n != 0:
        cell.rowspan = abs(n)


@handler("C3")
def _nested_spans(doc: Doc, cell: Cell) -> None:
    """\\multirow inside \\multicolumn and vice versa: keep unwrapping until stable."""
    for _ in range(4):
        before = (cell._start, cell._end, cell.rowspan, cell.colspan)
        _h("C1")(doc, cell)
        _h("C2")(doc, cell)
        if (cell._start, cell._end, cell.rowspan, cell.colspan) == before:
            return


def _trim(m: str, s: int, e: int) -> tuple[int, int]:
    while s < e and m[s] in " \t\r\n":
        s += 1
    while e > s and m[e - 1] in " \t\r\n":
        e -= 1
    return s, e


def _place(doc: Doc, pt: ParsedTable, rows) -> None:
    """Put each source cell on the logical grid. An empty source cell under a row span from
    above is a placeholder: it occupies its columns but is not an anchor."""
    m = doc.masked
    covered: set[tuple[int, int]] = set()
    n_cols = 0
    for r, (color, raw_cells) in enumerate(rows):
        c = 0
        for s, e in raw_cells:
            s, e = _trim(m, s, e)
            cell = Cell(row=r, col=c, _start=s, _end=e, _row_color=color)
            _h("C1")(doc, cell)
            _h("C2")(doc, cell)
            _h("C3")(doc, cell)
            if cell._start >= cell._end and (r, c) in covered:
                c += cell.colspan
                continue
            if (r, c) in covered:
                pt.warnings.append(f"cell ({r},{c}) has content under a span from another cell")
            pt.cells.append(cell)
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    covered.add((r + dr, c + dc))
            c += cell.colspan
        n_cols = max(n_cols, c)
    pt.n_rows, pt.n_cols = len(rows), n_cols
    pt._covered = covered  # for padding (C17)


# ---------------------------------------------------------------- cell content

# Wrappers a number may sit inside at the start of a cell (spec §5.5, number rule).
_LEAD = re.compile(
    r"(?:\s|\$|\{|~"
    r"|\\(?:textbf|textit|emph|underline|uline|mathbf|boldsymbol|mathrm|text|textsc|textnormal"
    r"|num)\s*\{"
    r"|\\(?:bf|it|bfseries|itshape|small|footnotesize|scriptsize|tiny|large|normalsize|rm|sf"
    r"|tt|sffamily|ttfamily)(?![A-Za-z])\s*"
    r"|\\cellcolor(?:\[[^\]]*\])?\{[^}]*\}"
    r"|\\textcolor(?:\[[^\]]*\])?\{[^}]*\}\s*\{"
    r"|\\color\{[^}]*\})*"
)
_BASIC_LITERAL = re.compile(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?![\w.])")
_SUFFIX = re.compile(
    r"(?:\s|\}|\$|\\xspace)*"
    r"(?:(?:\$?\^\{?(?:\\(?:dagger|ddagger|diamond|star|ast)|\*)\{?\}?\}?\$?"
    r"|\\textsuperscript\{[^}]*\}|\\(?:dagger|ddagger)|[†‡])(?:\s|\}|\$)*)*"
    r"(?:\([^()]*\))?(?:\s|\}|\$)*"
)
_MISSING_EMPTY = "empty"


def plain(s: str, macros: dict[str, str] | None = None) -> str:
    """Display text: (C15) macros expanded, markup stripped. Display only, never evidence."""
    if macros:
        s = _h("C15")(s, macros) or s
    s = re.sub(r"\\xspace(?![A-Za-z])", "", s)
    s = s.replace("$", "")
    for _ in range(5):
        s2 = re.sub(
            r"\\(?:text|textbf|textit|emph|underline|uline|mathrm|mathbf|textsc|texttt"
            r"|textnormal)\s*\{([^{}]*)\}",
            r"\1",
            s,
        )
        if s2 == s:
            break
        s = s2
    s = re.sub(
        r"\\(?:tt|bf|it|rm|sf|sc|small|footnotesize|scriptsize|tiny|large|normalsize"
        r"|bfseries|itshape)(?![A-Za-z])\s*",
        "",
        s,
    )
    s = s.replace("{", "").replace("}", "")
    return " ".join(s.split())


def _enabled(case: str) -> bool:
    return HANDLERS.get(case, _noop) is not _noop


def _analyse_cell(doc: Doc, ctx: Ctx, pt: ParsedTable, cell: Cell) -> None:
    if cell._start >= cell._end:
        cell.raw, cell.span, cell.missing, cell.text = "", None, _MISSING_EMPTY, ""
    else:
        cell.span = doc.span(cell._start, cell._end)
        cell.raw = cell.span.text
        cell.text = plain(cell.raw, ctx.macros)
        _number(doc, ctx, cell)
        _h("C10")(cell)
        _h("C5")(cell)
        _h("C11")(cell)
        _h("C12")(cell)
    _h("C7")(cell)


def _lead(raw: str) -> int:
    return _LEAD.match(raw).end()


def _number(doc: Doc, ctx: Ctx, cell: Cell) -> None:
    """Core number rule: a literal at the start of the cell (after wrappers), followed only by
    an allowed suffix. C9 widens the literal grammar, C8 reads +- values, C16 unwraps unknown
    whole-cell macros."""
    raw = cell.raw
    lead = _h("C16")(raw, _lead(raw)) or _lead(raw)
    literal = _h("C9")(raw, lead) or _BASIC_LITERAL.match(raw, lead)
    if not literal:
        return
    from themis.numbers import parse_literal

    value = parse_literal(literal.group(0))
    if value is None:
        return
    j = literal.end()
    pm = _h("C8")(raw, j)
    if pm:
        pm_value, pm_s, pm_e, pm_style, j = pm
    pct = raw.startswith("\\%", j)
    if pct:
        j += 2
    rest = _SUFFIX.match(raw, j)
    if rest.end() != len(raw):
        return
    base = cell._start
    cell.number = Number(value=value, span=doc.span(base + literal.start(), base + literal.end()))
    if pm:
        cell.number.pm = pm_value
        cell.number.pm_span = doc.span(base + pm_s, base + pm_e)
        cell.number.pm_style = pm_style
    if pct:
        cell.number.percent = _enabled("C9")  # percent is part of the C9 number formats
