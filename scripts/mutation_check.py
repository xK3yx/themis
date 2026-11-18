"""Mutation check (docs/m1-ingest-extract.md §10.3).

Disables one safeguard at a time and runs the test suite; every mutant must make the
suite fail. A surviving mutant is a safeguard with no test that proves it.

    uv run python scripts/mutation_check.py            # all SQL mutants
    uv run python scripts/mutation_check.py --list     # list mutants without running
    uv run python scripts/mutation_check.py -k V24     # only mutants whose label matches

SQL mutants (all migrations): each RAISE neutralised, each trigger dropped, each listed
constraint removed. Files are restored from their in-memory originals after every mutant,
never via git, so uncommitted work is safe.

Python mutants (provenance checks, parser cases) are added as those modules land.
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "themis" / "ledger" / "migrations"

# Constraints that sit behind triggers or are otherwise easy to lose. (file, label, old, new)
CONSTRAINTS = [
    ("0001_m0.sql", "kind CHECK", "CHECK (kind IN ('cited', 'measured', 'derived', 'prior'))", ""),
    (
        "0001_m0.sql",
        "one_successor index",
        "CREATE UNIQUE INDEX one_successor ON edges(dst) WHERE kind = 'supersedes';",
        "",
    ),
    (
        "0001_m0.sql",
        "cited source_id CHECK",
        "CHECK ((kind = 'cited') = (source_id IS NOT NULL)),",
        "",
    ),
    (
        "0001_m0.sql",
        "measured run_id CHECK",
        "CHECK ((kind = 'measured') = (run_id IS NOT NULL))",
        "CHECK (1)",
    ),
    ("0001_m0.sql", "role IS 'text'", "json_type(producer_json, '$.role') IS 'text'", "1"),
    (
        "0001_m0.sql",
        "generated n_seeds",
        "n_seeds       INTEGER GENERATED ALWAYS AS (json_array_length(seeds_json)) STORED",
        "n_seeds       INTEGER",
    ),
    (
        "0001_m0.sql",
        "node id hex CHECK",
        "CHECK (length(id) = 64 AND id NOT GLOB '*[^0-9a-f]*'),\n  kind",
        ",\n  kind",
    ),
    (
        "0002_m1.sql",
        "llm_calls role CHECK",
        "CHECK (role IN ('extractor', 'reasoner', 'critic', 'embedder'))",
        "",
    ),
    ("0002_m1.sql", "http_fetches no-email CHECK", "CHECK (instr(url, '@') = 0)", ""),
    ("0002_m1.sql", "paper_records doi lowercase", "CHECK (doi IS NULL OR doi = lower(doi))", ""),
    (
        "0002_m1.sql",
        "paper_records needs an id",
        "CHECK (arxiv_id IS NOT NULL OR doi IS NOT NULL OR openalex_id IS NOT NULL)",
        "CHECK (1)",
    ),
    ("0002_m1.sql", "paper_links a < b", "CHECK (a < b)", "CHECK (1)"),
]

# Mutants proven to change nothing, with the reason (spec §10.3 requires listing them).
EQUIVALENT: dict[str, str] = {}


@dataclass
class Mutant:
    label: str
    path: Path
    text: str


def sql_mutants() -> list[Mutant]:
    out = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        original = path.read_text(encoding="utf-8")
        for m in re.finditer(r"RAISE\(ABORT, '((?:[^']|'')*)'\)", original):
            label = f"{path.name}: raise {m.group(1)[:60]}"
            out.append(
                Mutant(
                    label,
                    path,
                    original[: m.start()] + f"NULLIF(1, '{m.group(1)}')" + original[m.end() :],
                )
            )
        for m in re.finditer(r"CREATE TRIGGER (\w+).*?\bEND;\n", original, flags=re.S):
            out.append(
                Mutant(
                    f"{path.name}: drop trigger {m.group(1)}",
                    path,
                    original[: m.start()] + original[m.end() :],
                )
            )
    for fname, label, old, new in CONSTRAINTS:
        path = MIGRATIONS / fname
        original = path.read_text(encoding="utf-8")
        if old not in original:
            raise SystemExit(f"constraint mutant {label!r} no longer matches {fname}")
        out.append(Mutant(f"{fname}: {label}", path, original.replace(old, new, 1)))
    return out


def run_suite() -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return r.returncode != 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only run mutants whose label contains this")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    mutants = [m for m in sql_mutants() if args.k in m.label and m.label not in EQUIVALENT]
    if args.list:
        for m in mutants:
            print(m.label)
        print(f"{len(mutants)} mutants")
        return 0

    survivors = []
    for m in mutants:
        original = m.path.read_text(encoding="utf-8")
        try:
            m.path.write_text(m.text, encoding="utf-8")
            killed = run_suite()
        finally:
            m.path.write_text(original, encoding="utf-8")
        print(("killed  " if killed else "SURVIVED") + "  " + m.label, flush=True)
        if not killed:
            survivors.append(m.label)

    print(f"\n{len(mutants) - len(survivors)}/{len(mutants)} mutants killed")
    for label in survivors:
        print("  survivor:", label)
    for label, why in EQUIVALENT.items():
        print(f"  equivalent (not run): {label}: {why}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
