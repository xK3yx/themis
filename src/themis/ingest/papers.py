"""Paper records and dedupe (M1 spec §4.2).

Records are appended per (source response, paper). `resolve()` groups them into papers:
  - records sharing a strong identifier (arXiv id, DOI, OpenAlex id) are the same paper,
    except that a merge which would put two DIFFERENT arXiv ids in one paper is refused
    and recorded as a possible duplicate (one wrong DOI must not mix two papers' evidence);
  - records with the same normalised title, same first-author surname and years within
    one of each other are flagged `possible_duplicate`, never merged.
Every merge and flag is written to `paper_links` with its reason.
"""

from dataclasses import dataclass

from themis.ingest.records import PaperMeta, normalise_title, surname
from themis.ledger.blobstore import utc_now
from themis.ledger.canonical import canonical_json, content_hash
from themis.ledger.dao import Ledger
from themis.ledger.db import tx


@dataclass(frozen=True)
class Paper:
    key: str  # arxiv:<id> | doi:<doi> | openalex:<id>
    record_ids: tuple[str, ...]
    arxiv_id: str | None
    doi: str | None
    openalex_id: str | None
    title: str
    year: int | None


class Papers:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.conn = ledger.conn

    def add_record(self, meta: PaperMeta, fetch_key: str) -> str:
        fields = {
            "fetch": fetch_key,
            "source_api": meta.source_api,
            "arxiv_id": meta.arxiv_id,
            "version": meta.version,
            "doi": meta.doi,
            "openalex_id": meta.openalex_id,
            "title": meta.title,
            "first_author": meta.first_author,
            "year": meta.year,
        }
        rid = content_hash(fields)
        record_json = canonical_json({"version": meta.version, **meta.raw}).decode()
        with tx(self.conn):
            if self.conn.execute("SELECT 1 FROM paper_records WHERE id = ?", (rid,)).fetchone():
                return rid
            self.conn.execute(
                "INSERT INTO paper_records(id, fetch_key, source_api, arxiv_id, doi, openalex_id,"
                " title, first_author, year, record_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    fetch_key,
                    meta.source_api,
                    meta.arxiv_id,
                    meta.doi,
                    meta.openalex_id,
                    meta.title,
                    meta.first_author,
                    meta.year,
                    record_json,
                    utc_now(),
                ),
            )
        return rid

    # ------------------------------------------------------------ resolve

    def resolve(self) -> list[Paper]:
        rows = self.conn.execute(
            "SELECT id, arxiv_id, doi, openalex_id, title, first_author, year, source_api"
            " FROM paper_records ORDER BY id"
        ).fetchall()
        recs = {r[0]: r for r in rows}
        parent = {rid: rid for rid in recs}
        arxivs = {rid: ({r[1]} if r[1] else set()) for rid, r in recs.items()}
        new_links: list[tuple[str, str, str, str]] = []

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for col_index, name in ((1, "arxiv_id"), (2, "doi"), (3, "openalex_id")):
            groups: dict[str, list[str]] = {}
            for rid, r in recs.items():
                if r[col_index]:
                    groups.setdefault(r[col_index], []).append(rid)
            for value in sorted(groups):
                ids = sorted(groups[value])
                first = ids[0]
                for other in ids[1:]:
                    ra, rb = find(first), find(other)
                    if ra == rb:
                        continue
                    if arxivs[ra] and arxivs[rb] and arxivs[ra] != arxivs[rb]:
                        new_links.append(
                            (
                                first,
                                other,
                                "possible_duplicate",
                                f"shared {name} {value} but different arXiv ids "
                                f"{sorted(arxivs[ra] | arxivs[rb])}",
                            )
                        )
                        continue
                    parent[rb] = ra
                    arxivs[ra] |= arxivs[rb]
                    new_links.append((first, other, "same_paper", f"shared {name} {value}"))

        clusters: dict[str, list[str]] = {}
        for rid in recs:
            clusters.setdefault(find(rid), []).append(rid)

        # Title-only matches: flag for review, never merge.
        by_title: dict[tuple[str, str], list[tuple[str, int | None, str]]] = {}
        for root, members in clusters.items():
            seen = set()
            for rid in members:
                r = recs[rid]
                key = (normalise_title(r[4]), surname(r[5]) or "")
                if key[0] and key[1] and key not in seen:
                    seen.add(key)
                    by_title.setdefault(key, []).append((root, r[6], min(members)))
        for key in sorted(by_title):
            entries = sorted(by_title[key], key=lambda e: e[2])
            for i, (ra, ya, ida) in enumerate(entries):
                for rb, yb, idb in entries[i + 1 :]:
                    if ra == rb:
                        continue
                    if ya is not None and yb is not None and abs(ya - yb) > 1:
                        continue
                    new_links.append(
                        (
                            ida,
                            idb,
                            "possible_duplicate",
                            "same normalised title and first-author surname",
                        )
                    )

        self._write_links(new_links)
        return sorted(
            (self._paper(members, recs) for members in clusters.values()), key=lambda p: p.key
        )

    def _write_links(self, links: list[tuple[str, str, str, str]]) -> None:
        with tx(self.conn):
            for a, b, kind, reason in links:
                a, b = sorted((a, b))
                exists = self.conn.execute(
                    "SELECT 1 FROM paper_links WHERE a = ? AND b = ? AND kind = ?", (a, b, kind)
                ).fetchone()
                if not exists:
                    self.conn.execute(
                        "INSERT INTO paper_links(a, b, kind, reason, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (a, b, kind, reason, utc_now()),
                    )

    @staticmethod
    def _paper(members: list[str], recs: dict) -> Paper:
        rows = [recs[m] for m in sorted(members)]

        def pick(i: int):
            values = sorted({r[i] for r in rows if r[i]})
            return values[0] if values else None

        arxiv_id, doi, oa = pick(1), pick(2), pick(3)
        key = f"arxiv:{arxiv_id}" if arxiv_id else f"doi:{doi}" if doi else f"openalex:{oa}"
        arxiv_rows = [r for r in rows if r[7] == "arxiv"]
        title = (arxiv_rows or rows)[0][4]
        years = [r[6] for r in rows if r[6] is not None]
        return Paper(
            key, tuple(sorted(members)), arxiv_id, doi, oa, title, min(years) if years else None
        )
