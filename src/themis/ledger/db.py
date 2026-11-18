"""SQLite connection factory for the ledger.

`connect()` gates on the SQLite version, sets pragmas, installs the schema on an empty
database, and refuses to open a database whose schema differs from `schema.sql`.
"""

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from importlib import resources
from pathlib import Path

from themis.ledger.errors import SchemaIntegrityError, SQLiteVersionError

MIN_SQLITE_VERSION = (3, 38, 0)
USER_VERSION = 1


@cache
def schema_sql() -> str:
    return resources.files("themis.ledger").joinpath("schema.sql").read_text(encoding="utf-8")


def _check_sqlite_version() -> None:
    found = tuple(sqlite3.sqlite_version_info)
    if found < MIN_SQLITE_VERSION:
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        raise SQLiteVersionError(
            f"SQLite {sqlite3.sqlite_version} is too old: the ledger needs >= {need} "
            "(built-in JSON functions, generated columns, STRICT tables). "
            "Use a Python build that bundles a newer SQLite."
        )


def _schema_objects(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: (typ, " ".join((sql or "").split())) for name, typ, sql in rows}


@cache
def _expected_objects() -> dict[str, tuple[str, str]]:
    ref = sqlite3.connect(":memory:", isolation_level=None)
    try:
        ref.executescript(schema_sql())
        return _schema_objects(ref)
    finally:
        ref.close()


def _check_integrity(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != USER_VERSION:
        raise SchemaIntegrityError(f"user_version is {version}, expected {USER_VERSION}")
    expected, actual = _expected_objects(), _schema_objects(conn)
    problems = []
    for name in sorted(expected.keys() - actual.keys()):
        problems.append(f"missing {expected[name][0]} {name}")
    for name in sorted(actual.keys() - expected.keys()):
        problems.append(f"unexpected {actual[name][0]} {name}")
    for name in sorted(expected.keys() & actual.keys()):
        if expected[name] != actual[name]:
            problems.append(f"altered {expected[name][0]} {name}")
    if problems:
        raise SchemaIntegrityError("ledger schema mismatch: " + "; ".join(problems))


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and if empty, initialise) a ledger database."""
    _check_sqlite_version()
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        is_empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if is_empty:
            conn.executescript(schema_sql())
        else:
            _check_integrity(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block via a savepoint; nests safely inside an outer transaction."""
    name = f"sp_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
