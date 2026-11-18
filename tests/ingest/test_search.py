"""arXiv and OpenAlex parsing, against responses recorded once from the real services."""

import json
from pathlib import Path

import httpx
import pytest

from themis.ingest.arxiv import arxiv_id_from, parse_oai, parse_search, search_arxiv
from themis.ingest.openalex import parse_works, search_openalex

FIX = Path(__file__).parents[1] / "fixtures" / "http"


def body(name: str) -> bytes:
    return (FIX / f"{name}.body").read_bytes()


# ---------------------------------------------------------------- arXiv ids


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://arxiv.org/abs/2106.09685v2", ("2106.09685", 2)),
        ("https://arxiv.org/pdf/2106.09685", ("2106.09685", None)),
        ("2305.14314v1", ("2305.14314", 1)),
        ("1501.00001", ("1501.00001", None)),
        ("http://arxiv.org/abs/hep-th/9901001v3", ("hep-th/9901001", 3)),
        ("https://doi.org/10.48550/arxiv.2402.09353", ("2402.09353", None)),
        ("10.48550/ARXIV.2402.09353", ("2402.09353", None)),
        ("https://openreview.net/forum?id=abc", None),
        ("2106.096851", None),
    ],
)
def test_arxiv_id_from(text, expected):
    assert arxiv_id_from(text) == expected


# ---------------------------------------------------------------- arXiv search (recorded)


def test_parse_arxiv_search_recorded():
    entries, total = parse_search(body("arxiv_search_lora"))
    assert total == 150 and len(entries) == 5
    lora = entries[0]
    assert (lora.arxiv_id, lora.version) == ("2106.09685", 2)
    assert lora.title == "LoRA: Low-Rank Adaptation of Large Language Models"
    assert lora.first_author == "Edward J. Hu" and lora.year == 2021
    assert lora.source_api == "arxiv" and lora.doi is None
    assert all(e.arxiv_id and "\n" not in e.title for e in entries)


def test_parse_arxiv_rejects_non_atom():
    with pytest.raises(ValueError):
        parse_search(b"<html>not a feed</html>")


def test_parse_oai_licence_recorded():
    rec = parse_oai(body("arxiv_oai_2106.09685"))
    assert rec.arxiv_id == "2106.09685"
    assert rec.license == "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"
    assert not rec.redistributable  # arXiv's default licence does not let us commit the source


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://creativecommons.org/licenses/by/4.0/", True),
        ("http://creativecommons.org/licenses/by-sa/4.0/", True),
        ("http://creativecommons.org/publicdomain/zero/1.0/", True),
        ("http://creativecommons.org/licenses/by-nc-sa/4.0/", False),
        ("http://creativecommons.org/licenses/by-nc-nd/4.0/", False),
        ("http://arxiv.org/licenses/nonexclusive-distrib/1.0/", False),
        (None, False),
    ],
)
def test_redistributable_licences(url, ok):
    from themis.ingest.arxiv import is_redistributable

    assert is_redistributable(url) is ok


# ---------------------------------------------------------------- OpenAlex (recorded)


def test_parse_openalex_recorded():
    works, _cursor = parse_works(body("openalex_search_lora"))
    assert len(works) == 5
    lora = works[0]
    assert lora.openalex_id == "W3168867926"
    assert lora.doi == "10.48550/arxiv.2106.09685"
    assert lora.arxiv_id == "2106.09685" and lora.year == 2021
    flora = next(w for w in works if w.openalex_id == "W4415798642")
    assert flora.doi == "10.52202/079017-0708"  # a NeurIPS DOI...
    assert flora.arxiv_id == "2409.05976"  # ...but the arXiv id comes from its locations
    assert all(w.doi is None or w.doi == w.doi.lower() for w in works)


# ---------------------------------------------------------------- through the fetcher


def _replay(name):
    meta = json.loads((FIX / f"{name}.meta.json").read_text(encoding="utf-8"))

    def handler(request):
        return httpx.Response(
            meta["status"], content=body(name), headers={"content-type": meta["content_type"] or ""}
        )

    return httpx.MockTransport(handler)


def test_search_arxiv_through_fetcher(fetcher_for):
    f = fetcher_for(_replay("arxiv_search_lora"))
    page = search_arxiv(f, 'ti:"low-rank adaptation"', start=0, max_results=5)
    assert len(page.entries) == 5 and page.fetch_key and page.total == 150


def test_search_openalex_through_fetcher(fetcher_for):
    f = fetcher_for(_replay("openalex_search_lora"))
    page = search_openalex(f, "low-rank adaptation", per_page=5)
    assert len(page.entries) == 5 and page.fetch_key


def test_oai_uses_the_current_endpoint_directly(fetcher_for):
    """arXiv moved OAI-PMH to oaipmh.arxiv.org; the old URL only redirects there."""
    from themis.ingest.arxiv import OAI, fetch_oai

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=body("arxiv_oai_2106.09685"))

    rec, _ = fetch_oai(fetcher_for(httpx.MockTransport(handler)), "2106.09685")
    assert OAI.startswith("https://oaipmh.arxiv.org/") and seen[0].startswith(OAI)
    assert rec.license
