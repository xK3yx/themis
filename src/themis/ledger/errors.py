"""Ledger exception hierarchy."""


class LedgerError(Exception):
    """Base class for all ledger errors."""


class CanonicalizationError(LedgerError):
    """A value cannot be represented in canonical JSON."""


class ValidationError(LedgerError):
    """A node, run, source or observation violates a validity rule."""


class NotFoundError(LedgerError):
    """A referenced row does not exist."""


class LedgerCorruptError(LedgerError):
    """Stored data does not match its content address."""


class SchemaIntegrityError(LedgerError):
    """The database schema differs from the expected schema (e.g. a trigger was dropped)."""


class SQLiteVersionError(LedgerError):
    """The SQLite library is too old for the ledger schema."""


class BlobCorruptError(LedgerCorruptError):
    """A blob's bytes do not hash to its address."""


class BlobMissingError(NotFoundError):
    """A blob is registered or referenced but its file is absent."""
