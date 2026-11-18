"""Invariant 3 for raw bytes: blobs are addressed by sha256, sharded, verified, atomic."""

import hashlib
import os
import stat
from pathlib import Path

import pytest

from themis.ledger.blobstore import BlobStore
from themis.ledger.db import connect
from themis.ledger.errors import BlobCorruptError, BlobMissingError, ValidationError


@pytest.fixture
def store(tmp_path, conn):
    return BlobStore(tmp_path / "store", conn)


def _make_writable(p: Path) -> None:
    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


def test_blob_address_and_shard(store, tmp_path):
    data = b"\\begin{table} 1 & 2 \\end{table}"
    sha = store.put(data, "application/x-tex")
    assert sha == hashlib.sha256(data).hexdigest()
    p = store.path(sha)
    assert p == tmp_path / "store" / sha[:2] / sha
    assert p.read_bytes() == data


def test_blob_row_registered(store, conn):
    sha = store.put(b"abc", "text/plain")
    row = conn.execute("SELECT size_bytes, media_type FROM blobs WHERE sha256=?", (sha,)).fetchone()
    assert row == (3, "text/plain")


def test_blob_put_idempotent(store, conn):
    a = store.put(b"same", "text/plain")
    b = store.put(b"same", "text/plain")
    assert a == b
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
    files = [p for p in store.root.rglob("*") if p.is_file() and ".tmp" not in p.parts]
    assert len(files) == 1


def test_blob_get_roundtrip(store):
    sha = store.put(b"payload", "text/plain")
    assert store.get(sha) == b"payload"


def test_blob_is_read_only_after_put(store):
    sha = store.put(b"ro", "text/plain")
    assert not os.access(store.path(sha), os.W_OK)


def test_blob_get_detects_corruption(store):
    sha = store.put(b"original", "text/plain")
    p = store.path(sha)
    _make_writable(p)
    p.write_bytes(b"tampered")
    with pytest.raises(BlobCorruptError):
        store.get(sha)
    assert store.get(sha, verify=False) == b"tampered"


def test_blob_missing(store):
    sha = hashlib.sha256(b"never stored").hexdigest()
    with pytest.raises(BlobMissingError):
        store.get(sha)


def test_blob_atomic_write(store, monkeypatch, conn):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    data = b"half-written"
    sha = hashlib.sha256(data).hexdigest()
    with pytest.raises(OSError):
        store.put(data, "text/plain")
    assert not store.path(sha).exists()
    assert conn.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    assert not any((store.root / ".tmp").glob("*"))


def test_blob_put_repairs_missing_row(tmp_path):
    root = tmp_path / "store"
    c1 = connect(tmp_path / "a.db")
    sha = BlobStore(root, c1).put(b"shared", "text/plain")
    c1.close()
    c2 = connect(tmp_path / "b.db")  # same files, empty registry
    assert BlobStore(root, c2).put(b"shared", "text/plain") == sha
    assert c2.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1
    c2.close()


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "../" + "a" * 61, "a" * 63])
def test_blob_path_rejects_bad_address(store, bad):
    with pytest.raises(ValidationError):
        store.path(bad)


def test_blob_paths_use_pathlib(store):
    sha = store.put(b"p", "text/plain")
    p = store.path(sha)
    assert isinstance(p, Path)
    assert p.is_relative_to(store.root)
