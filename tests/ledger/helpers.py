"""Builders for DAO-level tests."""

import json

from themis.ledger.dao import Ledger

# 'é' is 2 bytes and '≥' is 3 bytes in UTF-8, so byte and character offsets differ.
PAPER = (
    "\\section{Results} LoRA with rank 8 reaches 71.2 on MNLI. "
    "Café-style ablation: r ≥ 16 gives no gain.\n"
    "\\begin{tabular}{lc} Method & Acc \\\\ LoRA & 71.2 \\end{tabular}\n"
).encode()
ORIGIN = {"arxiv": "2106.09685", "version": "v2", "path": "main.tex"}
PRODUCER = {"role": "test"}


def span_of(text: str, data: bytes = PAPER) -> dict:
    raw = text.encode()
    start = data.index(raw)
    return {"type": "span", "start": start, "end": start + len(raw), "text": text}


def add_paper(ledger: Ledger) -> str:
    blob = ledger.blobs.put(PAPER, "application/x-tex")
    return ledger.add_source(blob, ORIGIN)


def cited(source: str, text: str = "rank 8 reaches 71.2", claim: str = "c") -> dict:
    return {
        "kind": "cited",
        "content": {"claim": claim},
        "provenance": {"source": source, "locator": span_of(text)},
    }


def prior(claim: str = "p") -> dict:
    return {"kind": "prior", "content": {"claim": claim}, "provenance": {"asserted_by": "reasoner"}}


def derived(parents: list[str], claim: str = "d", rule: str = "pool_mean") -> dict:
    return {
        "kind": "derived",
        "content": {"claim": claim},
        "provenance": {"parents": parents, "rule": rule},
    }


def measured(run: str, metric: str = "acc", claim: str = "m") -> dict:
    return {
        "kind": "measured",
        "content": {"claim": claim},
        "provenance": {"run": run, "metric": metric},
    }


def results(metrics: dict[str, dict[int, float]]) -> bytes:
    return json.dumps(
        {"metrics": {m: {str(s): v for s, v in per.items()} for m, per in metrics.items()}}
    ).encode()


def add_run(ledger: Ledger, seeds=(1, 2, 3), metrics=None) -> str:
    metrics = metrics if metrics is not None else {"acc": dict.fromkeys(seeds, 0.7)}
    return ledger.add_run(b'{"config": "lora-r8"}', results(metrics), list(seeds))
