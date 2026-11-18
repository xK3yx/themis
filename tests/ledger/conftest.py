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
