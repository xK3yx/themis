"""Connection factory: version gate, pragmas, schema install and integrity check."""

import sqlite3

import pytest

from themis.ledger.db import LATEST_VERSION, MIN_SQLITE_VERSION, connect, tx
from themis.ledger.errors import SchemaIntegrityError, SQLiteVersionError


def test_sqlite_version_too_old(monkeypatch, db_path):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.37.2")
    with pytest.raises(SQLiteVersionError) as exc:
        connect(db_path)
    msg = str(exc.value)
    assert "3.37.2" in msg and "3.38" in msg
    assert not db_path.exists()


def test_min_version_is_3_38():
    assert MIN_SQLITE_VERSION == (3, 38, 0)


def test_connect_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION


def test_fresh_install_creates_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"blobs", "sources", "runs", "nodes", "edges", "observations"} <= names


def test_reopen_existing_db(db_path):
    connect(db_path).close()
    c = connect(db_path)
    c.close()


def test_dropped_trigger_refuses_open(db_path):
    c = connect(db_path)
    c.execute("DROP TRIGGER nodes_no_update")
    c.close()
    with pytest.raises(SchemaIntegrityError, match="nodes_no_update"):
        connect(db_path)


def test_altered_trigger_refuses_open(db_path):
    c = connect(db_path)
    c.execute("DROP TRIGGER nodes_no_delete")
    c.execute("CREATE TRIGGER nodes_no_delete BEFORE DELETE ON nodes BEGIN SELECT 1; END")
    c.close()
    with pytest.raises(SchemaIntegrityError, match="nodes_no_delete"):
        connect(db_path)


def test_extra_trigger_refuses_open(db_path):
    c = connect(db_path)
    c.execute("CREATE TRIGGER sneaky AFTER INSERT ON nodes BEGIN SELECT 1; END")
    c.close()
    with pytest.raises(SchemaIntegrityError, match="sneaky"):
        connect(db_path)


def test_wrong_user_version_refuses_open(db_path):
    c = connect(db_path)
    c.execute("PRAGMA user_version = 99")
    c.close()
    with pytest.raises(SchemaIntegrityError, match="user_version"):
        connect(db_path)


def test_tx_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), tx(conn):
        conn.execute("INSERT INTO blobs VALUES (?,?,?,?)", ("a" * 64, 1, "text/plain", "t"))
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


def test_tx_nests(conn):
    with tx(conn), tx(conn):
        conn.execute("INSERT INTO blobs VALUES (?,?,?,?)", ("b" * 64, 1, "text/plain", "t"))
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1


def test_foreign_db_refused(db_path):
    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE something_else(x)")
    raw.close()
    with pytest.raises(SchemaIntegrityError, match="not a Themis ledger"):
        connect(db_path)
