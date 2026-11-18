"""Paper records and dedupe (M1 spec §4.2)."""

from pathlib import Path

import pytest

from themis.ingest.arxiv import parse_search
from themis.ingest.openalex import parse_works
from themis.ingest.papers import Papers
from themis.ingest.records import PaperMeta
from themis.ledger.canonical import content_hash

FIX = Path(__file__).parents[1] / "fixtures" / "http"


@pytest.fixture
def papers(ledger):
    return Papers(ledger)


def fetch_key(ledger, label: str) -> str:
    """A real http_fetches row for records to point at."""
    from tests.ledger.conftest import NOW

    key = content_hash({"label": label})
    blob = ledger.blobs.put(label.encode(), "text/plain")
    ledger.conn.execute(
        "INSERT INTO http_fetches(key, method, url, status, response_blob, content_type,"
        " fetched_at) VALUES (?,?,?,?,?,?,?)",
        (key, "GET", f"https://export.arxiv.org/{label}", 200, blob, "text/plain", NOW),
    )
    return key


def meta(api="arxiv", title="A Study of Things", **kw) -> PaperMeta:
    return PaperMeta(source_api=api, title=title, **kw)


def links(ledger):
    return ledger.conn.execute(
        "SELECT a, b, kind, reason FROM paper_links ORDER BY a, b"
    ).fetchall()


# ---------------------------------------------------------------- records


def test_add_record_idempotent(ledger, papers):
    fk = fetch_key(ledger, "q1")
    m = meta(arxiv_id="2106.09685", version=2, first_author="Edward J. Hu", year=2021)
    assert papers.add_record(m, fk) == papers.add_record(m, fk)
    assert ledger.conn.execute("SELECT count(*) FROM paper_records").fetchone()[0] == 1


def test_record_traces_to_its_fetch(ledger, papers):
    fk = fetch_key(ledger, "q1")
    rid = papers.add_record(meta(arxiv_id="2106.09685"), fk)
    assert ledger.conn.execute(
        "SELECT fetch_key FROM paper_records WHERE id = ?", (rid,)
    ).fetchone() == (fk,)


# ---------------------------------------------------------------- resolve: recorded data


def test_recorded_arxiv_and_openalex_merge_on_arxiv_id(ledger, papers):
    arx, _ = parse_search((FIX / "arxiv_search_lora.body").read_bytes())
    oa, _ = parse_works((FIX / "openalex_search_lora.body").read_bytes())
    fa, fo = fetch_key(ledger, "arxiv"), fetch_key(ledger, "openalex")
    for m in arx:
        papers.add_record(m, fa)
    for m in oa:
        papers.add_record(m, fo)
    resolved = {p.key: p for p in papers.resolve()}
    lora = resolved["arxiv:2106.09685"]
    assert len(lora.record_ids) == 2  # one from each API
    assert lora.openalex_id == "W3168867926" and lora.doi == "10.48550/arxiv.2106.09685"
    # FLoRA has a NeurIPS DOI in OpenAlex, but its arXiv id still keys it.
    assert "arxiv:2409.05976" in resolved
    assert all(p.key.startswith(("arxiv:", "doi:", "openalex:")) for p in resolved.values())


# ---------------------------------------------------------------- resolve: rules


def test_merge_on_shared_doi_and_link_reason(ledger, papers):
    fk = fetch_key(ledger, "q")
    a = papers.add_record(meta("openalex", openalex_id="W1", doi="10.1/x"), fk)
    b = papers.add_record(meta("openalex", openalex_id="W2", doi="10.1/x"), fk)
    [p] = papers.resolve()
    assert p.key == "doi:10.1/x" and set(p.record_ids) == {a, b}
    [(la, lb, kind, reason)] = links(ledger)
    assert (la, lb) == tuple(sorted([a, b])) and kind == "same_paper" and "10.1/x" in reason


def test_conflicting_arxiv_ids_are_not_merged(ledger, papers):
    fk = fetch_key(ledger, "q")
    a = papers.add_record(meta(arxiv_id="2401.00001", doi="10.1/shared"), fk)
    b = papers.add_record(meta(arxiv_id="2401.00002", doi="10.1/shared"), fk)
    resolved = papers.resolve()
    assert {p.key for p in resolved} == {"arxiv:2401.00001", "arxiv:2401.00002"}
    [(la, lb, kind, reason)] = links(ledger)
    assert (la, lb) == tuple(sorted([a, b])) and kind == "possible_duplicate"
    assert "different arXiv" in reason


def test_title_only_match_is_flagged_not_merged(ledger, papers):
    fk = fetch_key(ledger, "q")
    title = "LoRA: Low-Rank Adaptation of Large Language Models"
    papers.add_record(
        meta(title=title, arxiv_id="2106.09685", first_author="Edward J. Hu", year=2021), fk
    )
    papers.add_record(
        meta(
            "openalex",
            title=title.upper() + "!",
            openalex_id="W9",
            first_author="J. Edward Hu",
            year=2022,
        ),
        fk,
    )
    resolved = papers.resolve()
    assert len(resolved) == 2
    [(_, _, kind, reason)] = links(ledger)
    assert kind == "possible_duplicate" and "title" in reason


@pytest.mark.parametrize(
    ("author", "year"),
    [("Someone Else", 2021), ("Edward J. Hu", 2024)],
)
def test_title_match_needs_same_surname_and_close_year(ledger, papers, author, year):
    fk = fetch_key(ledger, "q")
    papers.add_record(
        meta(title="Same Title", arxiv_id="2106.09685", first_author="Edward J. Hu", year=2021), fk
    )
    papers.add_record(
        meta("openalex", title="Same Title", openalex_id="W9", first_author=author, year=year), fk
    )
    papers.resolve()
    assert links(ledger) == []


def test_key_priority(ledger, papers):
    fk = fetch_key(ledger, "q")
    papers.add_record(meta("openalex", openalex_id="W5", doi="10.5/y"), fk)
    papers.add_record(meta("openalex", openalex_id="W6"), fk)
    assert {p.key for p in papers.resolve()} == {"doi:10.5/y", "openalex:W6"}


def test_resolve_is_idempotent(ledger, papers):
    fk = fetch_key(ledger, "q")
    papers.add_record(meta(arxiv_id="2401.00001", doi="10.1/s"), fk)
    papers.add_record(meta("openalex", openalex_id="W1", doi="10.1/s"), fk)
    papers.add_record(meta("openalex", openalex_id="W2", arxiv_id="2401.00001"), fk)
    first = papers.resolve()
    n = len(links(ledger))
    assert papers.resolve() == first and len(links(ledger)) == n


def test_transitive_merge(ledger, papers):
    fk = fetch_key(ledger, "q")
    papers.add_record(meta(arxiv_id="2401.00001"), fk)
    papers.add_record(meta("openalex", openalex_id="W1", arxiv_id="2401.00001", doi="10.1/t"), fk)
    papers.add_record(meta("openalex", openalex_id="W1"), fk)
    [p] = papers.resolve()
    assert p.key == "arxiv:2401.00001" and len(p.record_ids) == 3
