"""arXiv API search (Atom) and OAI-PMH metadata, including the licence (M1 spec §4.1).

Parsed with stdlib ElementTree: Python 3.11's bundled expat resists entity-expansion
attacks and ElementTree never resolves external entities.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from themis.ingest.http import Fetcher
from themis.ingest.records import PaperMeta, SearchPage, clean_title

API = "https://export.arxiv.org/api/query"
OAI = "https://oaipmh.arxiv.org/oai"  # moved from export.arxiv.org/oai2 (which redirects here)

_NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "os": "http://a9.com/-/spec/opensearch/1.1/",
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "ax": "http://arxiv.org/OAI/arXiv/",
}
_NEW = r"(?P<new>\d{4}\.\d{4,5})"
_OLD = r"(?P<old>[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})"
_ID = re.compile(
    rf"(?:^|arxiv\.org/(?:abs|pdf)/|arxiv\.)(?:{_NEW}|{_OLD})(?:v(?P<v>\d+))?(?:\.pdf)?$",
    re.IGNORECASE,
)

# Licences that allow us to commit excerpts of the source (golden fixtures, spec §10.2).
REDISTRIBUTABLE = (
    re.compile(r"^https?://creativecommons\.org/licenses/by/\d\.\d/?$"),
    re.compile(r"^https?://creativecommons\.org/licenses/by-sa/\d\.\d/?$"),
    re.compile(r"^https?://creativecommons\.org/publicdomain/zero/1\.0/?$"),
)


def arxiv_id_from(text: str) -> tuple[str, int | None] | None:
    """(versionless id, version or None) from an arXiv URL, id, or arXiv DOI."""
    t = text.strip()
    t = re.sub(r"^https?://(dx\.)?doi\.org/", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^10\.48550/", "", t, flags=re.IGNORECASE)
    m = _ID.search(t)
    if not m:
        return None
    aid = m.group("new") or m.group("old")
    return aid, int(m.group("v")) if m.group("v") else None


def is_redistributable(license_url: str | None) -> bool:
    return bool(license_url) and any(p.match(license_url.strip()) for p in REDISTRIBUTABLE)


def _parse_xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from None


def parse_search(body: bytes) -> tuple[list[PaperMeta], int | None]:
    root = _parse_xml(body)
    if root.tag != f"{{{_NS['a']}}}feed":
        raise ValueError(f"not an Atom feed (root {root.tag})")
    total_text = root.findtext("os:totalResults", namespaces=_NS)
    entries = []
    for e in root.findall("a:entry", _NS):
        parsed = arxiv_id_from(e.findtext("a:id", default="", namespaces=_NS))
        if parsed is None:
            continue  # the API returns an error entry this way
        aid, version = parsed
        authors = [
            clean_title(a.findtext("a:name", default="", namespaces=_NS))
            for a in e.findall("a:author", _NS)
        ]
        published = e.findtext("a:published", default="", namespaces=_NS)
        doi = e.findtext("arxiv:doi", namespaces=_NS)
        entries.append(
            PaperMeta(
                source_api="arxiv",
                title=clean_title(e.findtext("a:title", namespaces=_NS)),
                arxiv_id=aid,
                version=version,
                doi=doi.strip().lower() if doi else None,
                first_author=authors[0] if authors else None,
                year=int(published[:4]) if published[:4].isdigit() else None,
                raw={"id": aid, "version": version, "authors": authors, "published": published},
            )
        )
    return entries, int(total_text) if total_text and total_text.isdigit() else None


@dataclass(frozen=True)
class OaiRecord:
    arxiv_id: str
    license: str | None
    title: str

    @property
    def redistributable(self) -> bool:
        return is_redistributable(self.license)


def parse_oai(body: bytes) -> OaiRecord:
    root = _parse_xml(body)
    meta = root.find(".//ax:arXiv", _NS)
    if meta is None:
        err = root.findtext(".//oai:error", namespaces=_NS)
        raise ValueError(f"no arXiv metadata in OAI response: {err}")
    return OaiRecord(
        arxiv_id=(meta.findtext("ax:id", default="", namespaces=_NS)).strip(),
        license=(meta.findtext("ax:license", namespaces=_NS) or "").strip() or None,
        title=clean_title(meta.findtext("ax:title", namespaces=_NS)),
    )


def search_arxiv(fetcher: Fetcher, query: str, *, start: int, max_results: int) -> SearchPage:
    r = fetcher.get(
        API,
        {
            "search_query": query,
            "start": start,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        },
        media_type="application/atom+xml",
    )
    entries, total = parse_search(r.body)
    return SearchPage(entries, r.key, total)


def fetch_oai(fetcher: Fetcher, arxiv_id: str) -> tuple[OaiRecord, str]:
    r = fetcher.get(
        OAI,
        {"verb": "GetRecord", "identifier": f"oai:arXiv.org:{arxiv_id}", "metadataPrefix": "arXiv"},
        media_type="application/xml",
    )
    return parse_oai(r.body), r.key
