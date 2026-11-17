"""Content-addressed blob store: `<root>/<sha[:2]>/<sha>`, registered in the `blobs` table."""

import contextlib
import os
import re
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from themis.ledger.canonical import sha256_hex
from themis.ledger.db import tx
from themis.ledger.errors import BlobCorruptError, BlobMissingError, ValidationError

_HEX64 = re.compile(r"[0-9a-f]{64}")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class BlobStore:
    def __init__(self, root: Path, conn: sqlite3.Connection):
        self.root = Path(root)
        self.conn = conn

    def path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not _HEX64.fullmatch(sha256):
            raise ValidationError(f"not a sha256 hex address: {sha256!r}")
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes, media_type: str) -> str:
        sha = sha256_hex(data)
        final = self.path(sha)
        if not final.exists():
            self._write_atomic(final, data)
        self._register(sha, len(data), media_type)
        return sha

    def get(self, sha256: str, verify: bool = True) -> bytes:
        p = self.path(sha256)
        try:
            data = p.read_bytes()
        except FileNotFoundError:
            raise BlobMissingError(f"blob {sha256} not found at {p}") from None
        if verify and sha256_hex(data) != sha256:
            raise BlobCorruptError(f"blob {sha256} content does not match its address")
        return data

    def is_registered(self, sha256: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
        return row is not None

    def _write_atomic(self, final: Path, data: bytes) -> None:
        tmp_dir = self.root / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / uuid.uuid4().hex
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
        os.chmod(final, stat.S_IREAD)

    def _register(self, sha: str, size: int, media_type: str) -> None:
        with tx(self.conn):
            if not self.is_registered(sha):
                self.conn.execute(
                    "INSERT INTO blobs(sha256, size_bytes, media_type, created_at)"
                    " VALUES (?,?,?,?)",
                    (sha, size, media_type, utc_now()),
                )
