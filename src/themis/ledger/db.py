"""SQLite connection factory for the ledger.

`connect()` gates on the SQLite version, sets pragmas, applies pending migrations (each
inside one transaction), and refuses to open a database whose schema differs from a fresh
install of the migrations. See docs/m0-ledger.md §5.3 and docs/m1-ingest-extract.md §2.
"""

import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from themis.ledger.errors import SchemaIntegrityError, SQLiteVersionError

MIN_SQLITE_VERSION = (3, 38, 0)
DEFAULT_BUSY_TIMEOUT_MS = 5000  # callers pass the configured value; this is only a fallback


@dataclass(frozen=True)
class Migration:
    number: int
    name: str
    sql: str


def _load_migrations() -> tuple[Migration, ...]:
    root = resources.files("themis.ledger").joinpath("migrations")
    found = []
    for entry in root.iterdir():
        m = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.number)
    return tuple(found)


MIGRATIONS: tuple[Migration, ...] = _load_migrations()
LATEST_VERSION: int = len(MIGRATIONS)


def _check_sqlite_version() -> None:
    found = tuple(sqlite3.sqlite_version_info)
    if found < MIN_SQLITE_VERSION:
        need = ".".join(map(str, MIN_SQLITE_VERSION))
        raise SQLiteVersionError(
            f"SQLite {sqlite3.sqlite_version} is too old: the ledger needs >= {need} "
            "(built-in JSON functions, generated columns, STRICT tables). "
            "Use a Python build that bundles a newer SQLite."
        )


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration and bump user_version, atomically."""
    script = (
        "BEGIN IMMEDIATE;\n"
        + migration.sql
        + f"\nPRAGMA user_version = {migration.number};\nCOMMIT;\n"
    )
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _schema_objects(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: (typ, " ".join((sql or "").split())) for name, typ, sql in rows}


@cache
def _expected_objects(upto: int | None = None) -> dict[str, tuple[str, str]]:
    """Schema produced by a fresh install of migrations 1..upto (default: all)."""
    upto = LATEST_VERSION if upto is None else upto
    ref = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for mig in MIGRATIONS[:upto]:
            _apply(ref, mig)
        return _schema_objects(ref)
    finally:
        ref.close()


def _check_integrity(conn: sqlite3.Connection, version: int) -> None:
    expected, actual = _expected_objects(version), _schema_objects(conn)
    problems = []
    for name in sorted(expected.keys() - actual.keys()):
        problems.append(f"missing {expected[name][0]} {name}")
    for name in sorted(actual.keys() - expected.keys()):
        problems.append(f"unexpected {actual[name][0]} {name}")
    for name in sorted(expected.keys() & actual.keys()):
        if expected[name] != actual[name]:
            problems.append(f"altered {expected[name][0]} {name}")
    if problems:
        raise SchemaIntegrityError(
            f"ledger schema mismatch at user_version {version}: " + "; ".join(problems)
        )


def connect(path: Path | str, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a ledger database, installing or upgrading its schema as needed."""
    _check_sqlite_version()
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=True)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA recursive_triggers = ON")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        is_empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        if version == 0:
            if not is_empty:
                raise SchemaIntegrityError(
                    "user_version is 0 but the database has objects: not a Themis ledger"
                )
        elif not 1 <= version <= LATEST_VERSION:
            raise SchemaIntegrityError(
                f"user_version is {version}; this code supports 1..{LATEST_VERSION}"
            )
        else:
            _check_integrity(conn, version)  # never upgrade a tampered database
        for mig in MIGRATIONS[version:]:
            _apply(conn, mig)
        _check_integrity(conn, LATEST_VERSION)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block.

    The outermost block opens with BEGIN IMMEDIATE, taking the write lock before its first
    read, so a check-then-insert inside it can't race another writer. Nested blocks are
    savepoints.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return
    name = f"sp_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
