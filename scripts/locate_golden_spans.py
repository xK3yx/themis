"""Fill byte spans into a hand-written golden expected file (M1 spec §10.2).

    uv run python scripts/locate_golden_spans.py <golden dir>/<name>.expected.json

The expected file is written by hand: coordinates, spans, and each cell's verbatim `raw`
text, plus `literal` / `pm_literal` for numbers. This tool only turns those hand-chosen
strings into integer byte offsets, by searching the fixture IN ORDER (each cell after the
previous one). It does not use the parser. An optional per-cell `after` string anchors the
search past it (e.g. a `\\multirow{2}{*}{` wrapper whose digits would otherwise match).

Every gap between consecutive cells must contain a separator (`&` or `\\\\`) or an opening
wrapper `{`; otherwise the cell was found in the wrong place and the tool fails.
"""

import json
import re
import sys
from pathlib import Path

GAP_OK = re.compile(r"&|\\\\|\{|\\begin\{|\\tabularnewline")


def locate(exp_path: Path) -> list[str]:
    exp = json.loads(exp_path.read_text(encoding="utf-8"))
    tex = (exp_path.parent / exp["fixture"]).read_bytes()
    problems = []
    for t_i, table in enumerate(exp["tables"]):
        start_hint = table.get("start_after", "")
        cursor = tex.find(start_hint.encode()) + len(start_hint.encode()) if start_hint else 0
        if start_hint and cursor < len(start_hint.encode()):
            problems.append(f"table {t_i}: start_after {start_hint!r} not found")
            continue
        prev_end = None
        for c in table["cells"]:
            where = f"table {t_i} cell ({c['row']},{c['col']})"
            raw = c.get("raw") or ""
            if not raw:
                c["span"] = None
                continue
            if c.get("after"):
                a = tex.find(c["after"].encode(), cursor)
                if a < 0:
                    problems.append(f"{where}: after-hint {c['after']!r} not found")
                    continue
                cursor = a + len(c["after"].encode())
            s = tex.find(raw.encode(), cursor)
            if s < 0:
                problems.append(f"{where}: raw {raw!r} not found after byte {cursor}")
                continue
            e = s + len(raw.encode())
            if prev_end is not None:
                gap = tex[prev_end:s].decode("utf-8", errors="replace")
                if not GAP_OK.search(gap):
                    problems.append(f"{where}: suspicious gap {gap!r} before {raw!r}")
            c["span"] = [s, e]
            n = c.get("number")
            if n:
                ls = tex.find(n["literal"].encode(), s, e)
                if ls < 0:
                    problems.append(f"{where}: literal {n['literal']!r} not in cell")
                else:
                    n["literal_span"] = [ls, ls + len(n["literal"].encode())]
                    if n.get("pm_literal") is not None:
                        ps = tex.find(n["pm_literal"].encode(), n["literal_span"][1], e)
                        if ps < 0:
                            problems.append(f"{where}: pm literal {n['pm_literal']!r} not found")
                        else:
                            n["pm_literal_span"] = [ps, ps + len(n["pm_literal"].encode())]
            cursor, prev_end = e, e
    exp_path.write_bytes((json.dumps(exp, indent=1, ensure_ascii=False) + "\n").encode())
    return problems


def main() -> int:
    bad = 0
    for arg in sys.argv[1:]:
        problems = locate(Path(arg))
        for p in problems:
            print(f"{arg}: {p}")
        bad += len(problems)
        print(f"{arg}: {'OK' if not problems else f'{len(problems)} problems'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
