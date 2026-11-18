"""Ledger DAO.

Every write is validated here first (readable `ValidationError`s naming the rule), and
again by the database triggers (the authority). There are no update or delete methods:
corrections are new nodes that supersede old ones.
"""

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from themis.ledger.blobstore import BlobStore, utc_now
from themis.ledger.canonical import canonical_json, content_hash
from themis.ledger.db import connect, tx
from themis.ledger.errors import (
    CanonicalizationError,
    LedgerCorruptError,
    NotFoundError,
    ValidationError,
)
from themis.ledger.models import (
    DerivedNode,
    Observation,
    StoredNode,
    TableCell,
    hashable,
    node_id,
    parse_node,
    provenance_dict,
)


def _canonical_text(value: Any, what: str) -> str:
    try:
        return canonical_json(value).decode("utf-8")
    except CanonicalizationError as e:
        raise ValidationError(f"{what} is not canonical JSON: {e}") from None


def _check_producer(producer: Any) -> dict:
    if not isinstance(producer, dict):
        raise ValidationError("producer must be an object with a 'role'")
    role = producer.get("role")
    if not isinstance(role, str) or not role:
        raise ValidationError("producer must have a non-empty string 'role'")
    _canonical_text(producer, "producer")
    return producer


class Ledger:
    def __init__(self, conn: sqlite3.Connection, blobs: BlobStore):
        self.conn = conn
        self.blobs = blobs

    @classmethod
    def open(cls, data_dir: Path) -> "Ledger":
        """Open `<data_dir>/ledger.db` with blobs under `<data_dir>/store/`."""
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = connect(data_dir / "ledger.db")
        return cls(conn, BlobStore(data_dir / "store", conn))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ sources and runs

    def add_source(self, blob_sha256: str, origin: dict) -> str:
        if not isinstance(origin, dict):
            raise ValidationError("source origin must be an object")
        if not self.blobs.is_registered(blob_sha256):
            raise NotFoundError(f"blob {blob_sha256} is not registered")
        origin_text = _canonical_text(origin, "source origin")
        sid = content_hash({"blob": blob_sha256, "origin": origin})
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT blob_sha256, origin_json FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            if row is not None:
                if row != (blob_sha256, origin_text):
                    raise LedgerCorruptError(f"source {sid} exists with different content")
                return sid
            self._execute(
                "INSERT INTO sources(id, blob_sha256, origin_json, created_at) VALUES (?,?,?,?)",
                (sid, blob_sha256, origin_text, utc_now()),
            )
        return sid

    def add_run(self, manifest: bytes, results: bytes, seeds: list[int]) -> str:
        if not isinstance(seeds, list) or not seeds or any(type(s) is not int for s in seeds):
            raise ValidationError("V10: run seeds must be a non-empty list of integers")
        if len(set(seeds)) != len(seeds):
            raise ValidationError(f"V10: run seeds must be distinct, got {seeds}")
        self._check_results(results, seeds)
        m = self.blobs.put(manifest, "application/octet-stream")
        r = self.blobs.put(results, "application/json")
        seeds = sorted(seeds)
        rid = content_hash({"manifest": m, "results": r, "seeds": seeds})
        seeds_text = json.dumps(seeds, separators=(",", ":"))
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT manifest_blob, results_blob, seeds_json FROM runs WHERE id = ?", (rid,)
            ).fetchone()
            if row is not None:
                if row != (m, r, seeds_text):
                    raise LedgerCorruptError(f"run {rid} exists with different content")
                return rid
            self._execute(
                "INSERT INTO runs(id, manifest_blob, results_blob, seeds_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (rid, m, r, seeds_text, utc_now()),
            )
        return rid

    @staticmethod
    def _load_results(data: bytes) -> dict:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValidationError(f"run results are not UTF-8 JSON: {e}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("metrics"), dict):
            raise ValidationError('run results must be {"metrics": {name: {seed: number}}}')
        return doc

    def _check_results(self, data: bytes, seeds: list[int]) -> None:
        metrics = self._load_results(data)["metrics"]
        allowed = {str(s) for s in seeds}
        for name, per_seed in metrics.items():
            if not isinstance(per_seed, dict):
                raise ValidationError(f"run results: metric {name!r} must map seed -> number")
            for seed, value in per_seed.items():
                if seed not in allowed:
                    raise ValidationError(f"run results: metric {name!r} has unknown seed {seed}")
                if type(value) not in (int, float) or (
                    isinstance(value, float) and not math.isfinite(value)
                ):
                    raise ValidationError(
                        f"run results: metric {name!r} seed {seed} is not a finite number"
                    )

    # ------------------------------------------------------------ nodes

    def add_node(self, node: Any, producer: dict) -> str:
        """Insert a node (idempotent) and record who asserted it. Returns the node id."""
        node = parse_node(node)
        producer = _check_producer(producer)
        content_text = _canonical_text(node.content, "node content")
        prov_text = _canonical_text(provenance_dict(node), "node provenance")
        nid = node_id(node)
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT content_json, provenance_json FROM nodes WHERE id = ?", (nid,)
            ).fetchone()
            if row is None:
                self._validate(node)
                p = node.provenance
                self._execute(
                    "INSERT INTO nodes(id, kind, schema_version, content_json, provenance_json,"
                    " source_id, run_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        nid,
                        node.kind,
                        hashable(node)["schema_version"],
                        content_text,
                        prov_text,
                        getattr(p, "source", None),
                        getattr(p, "run", None),
                        utc_now(),
                    ),
                )
            elif row != (content_text, prov_text):
                raise LedgerCorruptError(f"node {nid} exists with different content")
            self._observe(nid, producer)
        return nid

    def supersede(self, old_id: str, new: Any, producer: dict) -> str:
        """Insert `new` as the correction of `old_id`."""
        new = parse_node(new)
        if new.provenance.supersedes not in (None, old_id):
            raise ValidationError(
                f"node already supersedes {new.provenance.supersedes}, not {old_id}"
            )
        self.get(old_id)  # NotFoundError if unknown
        prov = new.provenance.model_copy(update={"supersedes": old_id})
        new = parse_node(
            {"kind": new.kind, "content": new.content, "provenance": prov.model_dump()}
        )
        return self.add_node(new, producer)

    def _validate(self, node: Any) -> None:
        """DAO-side checks, with readable messages. The DB re-checks all but V7 and V11."""
        p = node.provenance
        if node.kind == "cited":
            row = self.conn.execute(
                "SELECT b.sha256, b.size_bytes FROM sources s JOIN blobs b"
                " ON b.sha256 = s.blob_sha256 WHERE s.id = ?",
                (p.source,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"V4: cited node references unknown source {p.source}")
            blob_sha, size = row
            span = p.span
            if span.end > size:
                raise ValidationError(f"V5: span end {span.end} exceeds source size {size} bytes")
            data = self.blobs.get(blob_sha)
            if data[span.start : span.end] != span.text.encode("utf-8"):
                raise ValidationError(
                    f"V7: source bytes [{span.start}:{span.end}] do not match the span text"
                )
            if isinstance(p.locator, TableCell):
                cell = p.locator.cell
                if data[cell.start : cell.end] != cell.text.encode("utf-8"):
                    raise ValidationError(
                        f"V7b: source bytes [{cell.start}:{cell.end}] do not match the cell text"
                    )
        elif node.kind == "measured":
            row = self.conn.execute(
                "SELECT n_seeds, seeds_json, results_blob FROM runs WHERE id = ?", (p.run,)
            ).fetchone()
            if row is None:
                raise ValidationError(f"V8: measured node references unknown run {p.run}")
            n_seeds, seeds_json, results_blob = row
            if n_seeds < 3:
                raise ValidationError(f"V9: run {p.run} has {n_seeds} seeds; need >= 3")
            metrics = self._load_results(self.blobs.get(results_blob))["metrics"]
            per_seed = metrics.get(p.metric)
            if per_seed is None:
                raise ValidationError(f"V11: run {p.run} has no metric {p.metric!r}")
            missing = [s for s in json.loads(seeds_json) if str(s) not in per_seed]
            if missing:
                raise ValidationError(
                    f"V11: metric {p.metric!r} is missing for seeds {missing} of run {p.run}"
                )
        elif node.kind == "derived":
            for parent in p.parents:
                if not self._exists(parent):
                    raise ValidationError(f"V13: derived parent {parent} does not exist")
                succ = self.successor(parent)
                if succ is not None:
                    raise ValidationError(
                        f"V14: derived parent {parent} is superseded by {succ};"
                        " derive from the current node instead"
                    )
        if p.supersedes is not None:
            if not self._exists(p.supersedes):
                raise ValidationError(f"V15: supersedes target {p.supersedes} does not exist")
            succ = self.successor(p.supersedes)
            if succ is not None:
                raise ValidationError(
                    f"V15: {p.supersedes} is already superseded by {succ}; supersede {succ} instead"
                )

    def _observe(self, nid: str, producer: dict) -> None:
        psha = content_hash(producer)
        exists = self.conn.execute(
            "SELECT 1 FROM observations WHERE node_id = ? AND producer_sha256 = ?", (nid, psha)
        ).fetchone()
        if exists is None:
            self._execute(
                "INSERT INTO observations(node_id, producer_sha256, producer_json, observed_at)"
                " VALUES (?,?,?,?)",
                (nid, psha, _canonical_text(producer, "producer"), utc_now()),
            )

    def _execute(self, sql: str, params: tuple) -> None:
        try:
            self.conn.execute(sql, params)
        except sqlite3.IntegrityError as e:
            raise ValidationError(f"database rejected write: {e}") from e

    # ------------------------------------------------------------ reads

    def _exists(self, nid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM nodes WHERE id = ?", (nid,)).fetchone() is not None

    def _row_to_stored(self, row: tuple, verify: bool = True) -> StoredNode:
        nid, kind, version, content_json, prov_json, created_at = row
        obj = {
            "schema_version": version,
            "kind": kind,
            "content": json.loads(content_json),
            "provenance": json.loads(prov_json),
        }
        if verify and content_hash(obj) != nid:
            raise LedgerCorruptError(f"node {nid} does not match its content hash")
        node = parse_node(
            {"kind": kind, "content": obj["content"], "provenance": obj["provenance"]}
        )
        return StoredNode(id=nid, created_at=created_at, schema_version=version, node=node)

    _NODE_COLS = "id, kind, schema_version, content_json, provenance_json, created_at"

    def get(self, node_id: str) -> StoredNode:
        """Fetch a node, verifying that its id is the hash of its stored content."""
        row = self.conn.execute(
            f"SELECT {self._NODE_COLS} FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"node {node_id} not found")
        return self._row_to_stored(row)

    def verify_all(self) -> list[str]:
        """Ids of nodes whose stored content does not hash to their id."""
        bad = []
        for row in self.conn.execute(f"SELECT {self._NODE_COLS} FROM nodes ORDER BY id"):
            obj = {
                "schema_version": row[2],
                "kind": row[1],
                "content": json.loads(row[3]),
                "provenance": json.loads(row[4]),
            }
            try:
                ok = content_hash(obj) == row[0]
            except CanonicalizationError:
                ok = False
            if not ok:
                bad.append(row[0])
        return bad

    def parents(self, node_id: str) -> list[StoredNode]:
        node = self.get(node_id).node
        if not isinstance(node, DerivedNode):
            return []
        return [self.get(p) for p in node.provenance.parents]

    def successor(self, node_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND kind = 'supersedes'", (node_id,)
        ).fetchone()
        return row[0] if row else None

    def current(self, node_id: str) -> str:
        """Follow the (linear) correction chain to its head."""
        self.get(node_id)
        while (nxt := self.successor(node_id)) is not None:
            node_id = nxt
        return node_id

    def observations(self, node_id: str) -> list[Observation]:
        rows = self.conn.execute(
            "SELECT node_id, producer_sha256, producer_json, observed_at FROM observations"
            " WHERE node_id = ? ORDER BY observed_at, producer_sha256",
            (node_id,),
        ).fetchall()
        return [Observation(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]

    # ------------------------------------------------------------ admissibility

    def is_admissible(self, node_id: str) -> bool:
        return not self.why_inadmissible(node_id)

    def why_inadmissible(self, node_id: str) -> list[str]:
        """Reasons (naming node ids) why a node may not support a conclusion; empty if none."""
        memo: dict[str, list[str]] = {}

        def reasons(nid: str) -> list[str]:
            if nid in memo:
                return memo[nid]
            out: list[str] = []
            node = self.get(nid).node
            if node.kind == "prior":
                out.append(f"{nid} is a prior (unsourced model assertion)")
            succ = self.successor(nid)
            if succ is not None:
                out.append(f"{nid} is superseded by {succ}")
            if isinstance(node, DerivedNode):
                for parent in node.provenance.parents:
                    sub = reasons(parent)
                    if sub:
                        out.append(f"{nid} depends on inadmissible parent {parent}")
                        out.extend(sub)
            memo[nid] = list(dict.fromkeys(out))
            return memo[nid]

        return reasons(node_id)
