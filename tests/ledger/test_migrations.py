"""Schema migrations (M1 spec §2.1) and write-lock transactions (§2.2)."""

import contextlib
import re
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.ledger.helpers import PRODUCER, prior
from themis.ledger import db as dbmod
from themis.ledger.dao import Ledger
from themis.ledger.db import LATEST_VERSION, MIGRATIONS, connect, tx
from themis.ledger.errors import SchemaIntegrityError

M0_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ledger" / "m0_ledger.sqlite3"
M0_TABLES = ["blobs", "sources", "runs", "nodes", "edges", "observations"]


def _dump(path: Path, tables: list[str]) -> dict[str, list[tuple]]:
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}
    finally:
        c.close()


def test_migrations_are_numbered_and_contiguous():
    numbers = [m.number for m in MIGRATIONS]
    assert numbers == list(range(1, len(MIGRATIONS) + 1))
    assert len(MIGRATIONS) == LATEST_VERSION


def test_fresh_install_is_latest(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


def test_upgrade_real_m0_db_keeps_rows_byte_identical(tmp_path):
    db = tmp_path / "ledger.db"
    shutil.copyfile(M0_FIXTURE, db)
    before = _dump(db, M0_TABLES)
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 1
    assert sum(len(rows) for rows in before.values()) > 0

    c = connect(db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    c.close()

    assert _dump(db, M0_TABLES) == before
    connect(db).close()  # integrity check passes on reopen


def test_upgraded_db_still_enforces_m0_rules(tmp_path):
    db = tmp_path / "ledger.db"
    shutil.copyfile(M0_FIXTURE, db)
    c = connect(db)
    with pytest.raises(sqlite3.IntegrityError, match="V17"):
        c.execute("DELETE FROM nodes")
    c.close()


def test_tampered_m0_db_refuses_upgrade(tmp_path):
    db = tmp_path / "ledger.db"
    shutil.copyfile(M0_FIXTURE, db)
    raw = sqlite3.connect(db)
    raw.execute("DROP TRIGGER nodes_no_delete")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaIntegrityError, match="nodes_no_delete"):
        connect(db)
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda m: m.name)
def test_migrations_not_destructive(migration):
    body = re.sub(r"--[^\n]*", "", migration.sql)
    # Trigger bodies use BEGIN ... END; only transaction control is forbidden.
    body = re.sub(r"\bBEGIN\s+(SELECT|INSERT)", "", body, flags=re.IGNORECASE)
    assert not re.search(
        r"\bDROP\s+(TABLE|INDEX|VIEW)\b|\bALTER\s+TABLE\b[^;]*\bDROP\b|\bDELETE\s+FROM\b"
        r"|\bUPDATE\s+\w+\s+SET\b|\bPRAGMA\s+user_version\b|\bCOMMIT\b|\bROLLBACK\b",
        body,
        re.IGNORECASE,
    ), migration.name


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    shutil.copyfile(M0_FIXTURE, db)
    bad = dbmod.Migration(
        number=LATEST_VERSION + 1,
        name="9999_bad",
        sql="CREATE TABLE half_done(x INTEGER) STRICT;\nTHIS IS NOT SQL;",
    )
    monkeypatch.setattr(dbmod, "MIGRATIONS", (*MIGRATIONS, bad))
    monkeypatch.setattr(dbmod, "LATEST_VERSION", bad.number)
    dbmod._expected_objects.cache_clear()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connect(db)
    finally:
        dbmod._expected_objects.cache_clear()
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in names


def test_db_newer_than_code_refuses(db_path):
    connect(db_path).close()
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    raw.close()
    with pytest.raises(SchemaIntegrityError, match="user_version"):
        connect(db_path)


# ---------------------------------------------------------------- §2.2 write lock


def test_busy_timeout_set(db_path):
    c = connect(db_path, busy_timeout_ms=1234)
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    c.close()


def test_outer_tx_takes_write_lock_before_any_write(db_path):
    a = connect(db_path, busy_timeout_ms=0)
    b = connect(db_path, busy_timeout_ms=0)
    try:
        with tx(a):
            a.execute("SELECT count(*) FROM nodes").fetchone()  # read only, no write yet
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                b.execute("BEGIN IMMEDIATE")
    finally:
        a.close()
        b.close()


def test_concurrent_add_same_node_is_idempotent(tmp_path, monkeypatch):
    """Both writers pass the existence check at the same moment unless the outer
    transaction holds the write lock from the start (BEGIN IMMEDIATE)."""
    data = tmp_path / "data"
    Ledger.open(data).close()
    barrier = threading.Barrier(2)
    original = Ledger._validate

    def slow_validate(self, node):
        # With the write lock held, the other writer never gets here and the wait times out.
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=1.0)
        return original(self, node)

    monkeypatch.setattr(Ledger, "_validate", slow_validate)
    results, errors = [], []

    def worker():
        lg = Ledger.open(data)
        try:
            results.append(lg.add_node(prior("same"), PRODUCER))
        except Exception as e:
            errors.append(e)
        finally:
            lg.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
