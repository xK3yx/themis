"""Find papers whose arXiv licence allows committing table excerpts as golden fixtures.

    uv run python scripts/find_golden_candidates.py WORK_DIR OUT.jsonl [--per-query N]

Searches arXiv for LoRA-family and wider efficient-ML topics, then reads each hit's licence
from arXiv OAI-PMH metadata. Uses Themis's own cached fetcher (pacing, ledger cache) with a
work ledger in WORK_DIR, so an interrupted run resumes without refetching. Writes one JSON
line per paper: id, version, title, licence, redistributable, query. The eval papers are
excluded outright. Selection criteria: docs/m1-ingest-extract.md §10.2.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from themis.config import Env, load_config  # noqa: E402
from themis.ingest.arxiv import fetch_oai, search_arxiv  # noqa: E402
from themis.ingest.http import Fetcher  # noqa: E402
from themis.ledger.dao import Ledger  # noqa: E402

EVAL_PAPERS = {"2106.09685", "2305.14314", "2303.10512", "2402.09353", "2405.09673"}

QUERIES = [
    # LoRA family first
    'abs:"low-rank adaptation"',
    "ti:LoRA",
    'abs:"parameter-efficient fine-tuning"',
    'abs:adapter AND abs:"parameter-efficient"',
    # widening: fine-tuning, quantization, efficient training
    'abs:"quantization" AND abs:"large language models" AND abs:"fine-tuning"',
    'abs:"efficient fine-tuning" AND cat:cs.LG',
    'abs:"prompt tuning" AND abs:"parameter-efficient"',
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--per-query", type=int, default=50)
    args = ap.parse_args()

    cfg = load_config(ROOT / "config" / "providers.toml", Env({}), require_enabled=False)
    ledger = Ledger.open(args.work_dir)
    fetcher = Fetcher(cfg, Env({}), ledger)

    seen: dict[str, dict] = {}
    for q in QUERIES:
        page = search_arxiv(fetcher, q, start=0, max_results=args.per_query)
        for e in page.entries:
            if e.arxiv_id in EVAL_PAPERS or e.arxiv_id in seen:
                continue
            seen[e.arxiv_id] = {
                "id": e.arxiv_id,
                "version": e.version,
                "title": e.title,
                "year": e.year,
                "query": q,
            }
        print(f"{q!r}: {len(page.entries)} hits, {len(seen)} unique so far", flush=True)

    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        for i, (aid, rec) in enumerate(sorted(seen.items())):
            try:
                oai, _ = fetch_oai(fetcher, aid)
                rec["license"] = oai.license
                rec["redistributable"] = oai.redistributable
            except Exception as e:  # recorded per paper, not fatal
                rec["license"], rec["redistributable"], rec["error"] = None, False, str(e)
            out.write(json.dumps(rec, sort_keys=True) + "\n")
            out.flush()
            if i % 25 == 0:
                print(f"licences checked: {i + 1}/{len(seen)}", flush=True)
    ok = sum(
        1
        for line in args.out.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["redistributable"]
    )
    print(f"done: {len(seen)} papers, {ok} redistributable")
    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
