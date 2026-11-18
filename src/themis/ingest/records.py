"""Paper metadata as parsed from one source API response."""

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PaperMeta:
    source_api: str  # 'arxiv' | 'openalex'
    title: str
    arxiv_id: str | None = None  # versionless
    version: int | None = None
    doi: str | None = None  # lowercased, no https://doi.org/ prefix
    openalex_id: str | None = None  # 'W...'
    first_author: str | None = None
    year: int | None = None
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class SearchPage:
    entries: list[PaperMeta]
    fetch_key: str
    total: int | None = None
    next_cursor: str | None = None


def clean_title(text: str | None) -> str:
    return " ".join((text or "").split())


def normalise_title(text: str) -> str:
    """For duplicate detection only: NFKC, casefold, keep letters and digits."""
    t = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", t)


def surname(name: str | None) -> str | None:
    """Last whitespace-separated token, normalised: 'Edward J. Hu' and 'J. Edward Hu' -> 'hu'."""
    if not name:
        return None
    parts = unicodedata.normalize("NFKC", name).casefold().replace(",", " ").split()
    return re.sub(r"[\W_]+", "", parts[-1]) if parts else None
