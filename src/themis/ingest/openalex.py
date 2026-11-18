"""OpenAlex `/works` search (M1 spec §4.1). The `mailto` param is a secret: sent, never stored."""

import json

from themis.ingest.arxiv import arxiv_id_from
from themis.ingest.http import Fetcher
from themis.ingest.records import PaperMeta, SearchPage, clean_title

API = "https://api.openalex.org/works"


def _strip_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix) :]
    return d or None


def _arxiv_from_work(w: dict, doi: str | None) -> tuple[str, int | None] | None:
    if doi and doi.startswith("10.48550/arxiv."):
        return arxiv_id_from(doi)
    locations = [w.get("primary_location"), w.get("best_oa_location"), *(w.get("locations") or [])]
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for key in ("landing_page_url", "pdf_url"):
            url = loc.get(key)
            if url and "arxiv.org/" in url:
                found = arxiv_id_from(url)
                if found:
                    return found
    return None


def parse_works(body: bytes) -> tuple[list[PaperMeta], str | None]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"OpenAlex response is not JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("OpenAlex response has no results list")
    works = []
    for w in data["results"]:
        oid = (w.get("id") or "").rsplit("/", 1)[-1] or None
        doi = _strip_doi(w.get("doi"))
        arx = _arxiv_from_work(w, doi)
        authorships = w.get("authorships") or []
        first = (authorships[0].get("author") or {}).get("display_name") if authorships else None
        works.append(
            PaperMeta(
                source_api="openalex",
                title=clean_title(w.get("title") or w.get("display_name")),
                arxiv_id=arx[0] if arx else None,
                doi=doi,
                openalex_id=oid,
                first_author=first,
                year=w.get("publication_year"),
                raw={"id": oid, "doi": doi, "arxiv": arx[0] if arx else None},
            )
        )
    return works, (data.get("meta") or {}).get("next_cursor")


def search_openalex(
    fetcher: Fetcher,
    query: str,
    *,
    per_page: int,
    filter: str | None = None,
    cursor: str = "*",
) -> SearchPage:
    params = {"search": query, "per-page": per_page, "cursor": cursor}
    if filter:
        params["filter"] = filter
    r = fetcher.get(
        API,
        params,
        secrets={"mailto": fetcher.config.sources.openalex.mailto_env},
        media_type="application/json",
    )
    works, nxt = parse_works(r.body)
    return SearchPage(works, r.key, None, nxt)
