"""Shared fixtures. `Raw` inserts rows with plain SQL, bypassing the DAO, so tests can
prove that the database itself enforces each rule."""

import json
import sqlite3

import pytest

from themis.ledger.canonical import sha256_hex
from themis.ledger.db import connect

NOW = "2026-01-01T00:00:00+00:00"
_UNSET = object()


def hx(label: str) -> str:
    """A well-formed 64-hex id derived from a label (the DB cannot check real hashes)."""
    return sha256_hex(label.encode())


class Raw:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._n = 0

    def _label(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def blob(self, size: int = 100) -> str:
        sha = hx(self._label("blob"))
        self.conn.execute(
            "INSERT INTO blobs(sha256, size_bytes, media_type, created_at) VALUES (?,?,?,?)",
            (sha, size, "text/plain", NOW),
        )
        return sha

    def source(self, size: int = 100) -> str:
        blob = self.blob(size)
        sid = hx(self._label("source"))
        self.conn.execute(
            "INSERT INTO sources(id, blob_sha256, origin_json, created_at) VALUES (?,?,?,?)",
            (sid, blob, json.dumps({"arxiv": "0000.00000"}), NOW),
        )
        return sid

    def run(self, seeds=(1, 2, 3)) -> str:
        rid = hx(self._label("run"))
        m, r = self.blob(), self.blob()
        self.conn.execute(
            "INSERT INTO runs(id, manifest_blob, results_blob, seeds_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (rid, m, r, json.dumps(list(seeds)), NOW),
        )
        return rid

    def node(
        self,
        kind: str,
        provenance: dict,
        content: dict | None = None,
        node_id: str | None = None,
        source_id=_UNSET,
        run_id=_UNSET,
    ) -> str:
        nid = node_id or hx(self._label("node"))
        if source_id is _UNSET:
            source_id = provenance.get("source") if kind == "cited" else None
        if run_id is _UNSET:
            run_id = provenance.get("run") if kind == "measured" else None
        self.conn.execute(
            "INSERT INTO nodes(id, kind, schema_version, content_json, provenance_json,"
            " source_id, run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                nid,
                kind,
                1,
                json.dumps(content or {"claim": "x"}),
                json.dumps(provenance),
                source_id,
                run_id,
                NOW,
            ),
        )
        return nid

    # M1 tables
    def llm_call(self, role: str = "extractor") -> str:
        key = hx(self._label("call"))
        req, resp = self.blob(), self.blob()
        self.conn.execute(
            "INSERT INTO llm_calls(key, role, provider, model, request_blob, response_blob,"
            " prompt_template, output_schema_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, role, "fake", "fake-model", req, resp, "label_table@abc", 1, NOW),
        )
        return key

    def http_fetch(self, url: str = "https://export.arxiv.org/api/query?q=lora") -> str:
        key = hx(self._label("fetch"))
        self.conn.execute(
            "INSERT INTO http_fetches(key, method, url, status, response_blob, content_type,"
            " fetched_at) VALUES (?,?,?,?,?,?,?)",
            (key, "GET", url, 200, self.blob(), "application/atom+xml", NOW),
        )
        return key

    def paper_record(
        self, arxiv_id=_UNSET, doi=None, openalex_id=None, title: str = "A paper"
    ) -> str:
        if arxiv_id is _UNSET:
            arxiv_id = "2106.09685"
        rid = hx(self._label("record"))
        self.conn.execute(
            "INSERT INTO paper_records(id, fetch_key, source_api, arxiv_id, doi, openalex_id,"
            " title, first_author, year, record_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                self.http_fetch(),
                "arxiv",
                arxiv_id,
                doi,
                openalex_id,
                title,
                "Hu",
                2021,
                "{}",
                NOW,
            ),
        )
        return rid

    def paper_link(self, a: str, b: str, kind: str = "possible_duplicate", ordered=True):
        if ordered:
            a, b = sorted([a, b])
        self.conn.execute(
            "INSERT INTO paper_links(a, b, kind, reason, created_at) VALUES (?,?,?,?,?)",
            (a, b, kind, "same normalised title", NOW),
        )

    def stage_record(self, key=None, status="done", attempt=1, version="1", stage="parse"):
        key = key or self._label("item")
        self.conn.execute(
            "INSERT INTO stage_records(stage, input_key, version, attempt, status, output_ref,"
            " detail_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (stage, key, version, attempt, status, None, "{}", NOW),
        )

    # Convenience constructors for valid nodes.
    def span(self, start=0, end=5, text="hello") -> dict:
        return {"type": "span", "start": start, "end": end, "text": text}

    def cited(self, **kw) -> str:
        src = self.source()
        return self.node("cited", {"source": src, "locator": self.span()}, **kw)

    def prior(self, **kw) -> str:
        return self.node("prior", {"asserted_by": "reasoner"}, **kw)

    def derived(self, parents, **kw) -> str:
        return self.node("derived", {"parents": list(parents), "rule": "pool_mean"}, **kw)

    def supersede(self, old: str) -> str:
        return self.node("prior", {"asserted_by": "reasoner", "supersedes": old})


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "ledger.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def raw(conn):
    return Raw(conn)
