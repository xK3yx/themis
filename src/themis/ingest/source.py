"""arXiv LaTeX source: fetch the pinned e-print and unpack it in memory (M1 spec §4.3).

An e-print is untrusted input. Nothing is ever extracted to disk: members are read from an
in-memory tar stream, checked, and stored as blobs. Path traversal or absolute paths reject
the whole archive; links and device files are skipped with a reason; sizes and member
counts are capped by `[ingest]` in config. Files are not flattened: each kept file becomes
its own blob and `sources` row, so byte offsets always refer to bytes exactly as served.
"""

import gzip
import io
import re
import tarfile
from dataclasses import dataclass, field

from themis.config import IngestLimits
from themis.ingest.http import Fetcher
from themis.ledger.dao import Ledger

EPRINT = "https://arxiv.org/e-print/{id}v{version}"
MEDIA = {
    ".tex": "application/x-tex",
    ".bbl": "application/x-tex",
    ".sty": "application/x-tex",
    ".cls": "application/x-tex",
    ".bib": "application/x-bibtex",
}
_DOCCLASS = re.compile(rb"^[^%\n]*\\documentclass", re.MULTILINE)
_BEGIN_DOC = re.compile(rb"^[^%\n]*\\begin\{document\}", re.MULTILINE)
_TEXLIKE = re.compile(rb"\\(documentclass|begin\{document\}|section|input|usepackage)\b")


class UnsafeArchive(Exception):
    """The e-print is malformed or hostile; nothing from it is kept."""


@dataclass(frozen=True)
class UnpackedFile:
    path: str  # POSIX, relative, normalised
    data: bytes


@dataclass
class Unpacked:
    kind: str  # 'tar' | 'single_tex' | 'pdf' | 'unknown'
    files: list[UnpackedFile] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class SourceFetch:
    kind: str  # Unpacked.kind, or 'not_found'
    sources: list[tuple[str, str]]  # (path, source_id)
    skipped: list[tuple[str, str]]
    main: str | None
    archive_blob: str | None


def _safe_path(name: str) -> str:
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UnsafeArchive(f"unsafe member path {name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafeArchive(f"unsafe member path {name!r}")
    return "/".join(parts)


def _gunzip(data: bytes, cap: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            out = gz.read(cap + 1)
    except (OSError, EOFError) as e:
        raise UnsafeArchive(f"corrupt gzip stream: {e}") from None
    if len(out) > cap:
        raise UnsafeArchive(f"decompressed size exceeds {cap} bytes")
    return out


def _is_tar(payload: bytes) -> bool:
    return len(payload) >= 262 and payload[257:262] == b"ustar"


def unpack(data: bytes, limits: IngestLimits, *, single_name: str) -> Unpacked:
    if data[:5] == b"%PDF-":
        return Unpacked("pdf")
    payload = _gunzip(data, limits.max_total_bytes) if data[:2] == b"\x1f\x8b" else data
    if _is_tar(payload):
        return _unpack_tar(payload, limits)
    if payload[:5] == b"%PDF-":
        return Unpacked("pdf")
    if _TEXLIKE.search(payload[:200_000]) and b"\x00" not in payload[:4096]:
        return Unpacked("single_tex", [UnpackedFile(single_name, payload)])
    return Unpacked("unknown")


def _open_tar(payload: bytes) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as e:
        raise UnsafeArchive(f"corrupt tar: {e}") from None


def _unpack_tar(payload: bytes, limits: IngestLimits) -> Unpacked:
    keep = {s.lower() for s in limits.keep_suffixes}
    out = Unpacked("tar")
    seen: set[str] = set()
    total = 0
    with _open_tar(payload) as tar:
        members = tar.getmembers()
        if len(members) > limits.max_members:
            raise UnsafeArchive(f"{len(members)} members exceeds max_members {limits.max_members}")
        for m in members:
            path = _safe_path(m.name)  # raises for traversal / absolute paths
            if m.isdir():
                continue
            if not m.isreg():
                kind = (
                    "symlink"
                    if m.issym()
                    else "hardlink"
                    if m.islnk()
                    else "fifo"
                    if m.isfifo()
                    else "device"
                )
                out.skipped.append((path, f"not a regular file ({kind})"))
                continue
            total += m.size
            if total > limits.max_total_bytes:
                raise UnsafeArchive(
                    f"total size of members exceeds max_total_bytes {limits.max_total_bytes}"
                )
            suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
            if suffix not in keep:
                out.skipped.append((path, "suffix not kept"))
                continue
            if m.size > limits.max_file_bytes:
                out.skipped.append((path, f"larger than max_file_bytes {limits.max_file_bytes}"))
                continue
            if path in seen:
                out.skipped.append((path, "duplicate path"))
                continue
            fh = tar.extractfile(m)
            data = fh.read(m.size + 1) if fh else b""
            if len(data) != m.size:
                raise UnsafeArchive(f"member {path} size mismatch")
            seen.add(path)
            out.files.append(UnpackedFile(path, data))
    out.files.sort(key=lambda f: f.path)
    return out


def find_main(files: dict[str, bytes]) -> str | None:
    """The main .tex: has an uncommented \\documentclass; ties broken by \\begin{document},
    then the name main.tex, then shortest path, then alphabetical."""
    cands = [p for p, d in files.items() if p.lower().endswith(".tex") and _DOCCLASS.search(d)]
    if not cands:
        return None
    return min(
        cands,
        key=lambda p: (
            not _BEGIN_DOC.search(files[p]),
            p.rsplit("/", 1)[-1].lower() != "main.tex",
            p.count("/"),
            p,
        ),
    )


def fetch_source(fetcher: Fetcher, ledger: Ledger, arxiv_id: str, version: int) -> SourceFetch:
    """Fetch the pinned e-print and register each kept file as a ledger source."""
    url = EPRINT.format(id=arxiv_id, version=version)
    r = fetcher.get(url, media_type="application/octet-stream")
    if r.status != 200:
        return SourceFetch("not_found", [], [], None, None)
    archive_blob = ledger.blobs.put(r.body, "application/octet-stream")
    safe_id = arxiv_id.replace("/", "_")
    un = unpack(r.body, fetcher.config.ingest, single_name=f"{safe_id}.tex")
    sources = []
    for f in un.files:
        suffix = "." + f.path.rsplit(".", 1)[-1].lower()
        blob = ledger.blobs.put(f.data, MEDIA.get(suffix, "application/x-tex"))
        sid = ledger.add_source(
            blob,
            {"arxiv": arxiv_id, "version": f"v{version}", "path": f.path, "archive": archive_blob},
        )
        sources.append((f.path, sid))
    main = find_main({f.path: f.data for f in un.files})
    return SourceFetch(un.kind, sources, un.skipped, main, archive_blob)
