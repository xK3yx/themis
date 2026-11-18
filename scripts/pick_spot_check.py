"""Pick golden tables for a human spot-check, by seeded random choice (M1 spec §10.2).

    uv run python scripts/pick_spot_check.py [N]

The seed is derived from a fixed phrase, not chosen by whoever wrote the goldens, so the
picks can't be steered toward easy tables. Only real tables are eligible: they are the
ones transcribed by hand from papers. Anyone can re-run this and get the same picks.
"""

import hashlib
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.extract.golden import load_goldens  # noqa: E402

SEED_PHRASE = b"themis M1 golden spot-check"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    seed = int.from_bytes(hashlib.sha256(SEED_PHRASE).digest()[:8], "big")
    population = sorted(g.id for g in load_goldens() if not g.synthetic)
    picks = random.Random(seed).sample(population, n)
    by_id = {g.id: g for g in load_goldens()}
    print(f"seed phrase : {SEED_PHRASE.decode()!r}")
    print(f"seed        : {seed}  (first 8 bytes of sha256(phrase), big-endian)")
    print(f"population  : {len(population)} real golden tables")
    for i, gid in enumerate(picks, 1):
        g = by_id[gid]
        t = g.meta["tables"][g.name]
        cells = sum(len(x.cells) for x in g.tables)
        print(f"\npick {i}: {gid}")
        print(f"  paper    : arXiv {g.meta['arxiv_id']}{g.meta['version']}  ({g.meta['license']})")
        print(f"  source   : {t['source_path']} bytes {t['slice'][0]}-{t['slice'][1]}")
        print(f"  size     : {g.tables[0].n_rows} rows x {g.tables[0].n_cols} cols, {cells} cells")
        print(f"  cases    : {', '.join(g.cases)}")
        print(f"  LaTeX    : {(g.dir / g.expected['fixture']).relative_to(ROOT)}")
        print(f"  expected : {(g.dir / (g.name + '.expected.json')).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
