"""arXiv e-print fetch and in-memory unpacking (M1 spec §4.3). Archives here are synthetic."""

import gzip
import io
import tarfile

import httpx
import pytest

from themis.config import IngestLimits
from themis.ingest.source import UnsafeArchive, fetch_source, find_main, unpack

MAIN = b"\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\end{document}\n"
INTRO = b"\\section{Intro} Text.\n"
LIMITS = IngestLimits(max_members=50, max_total_bytes=50_000, max_file_bytes=10_000)


def make_tar(members, gz=True) -> bytes:
    """members: (name, data | None, kind); kind is file, dir, symlink, hardlink or fifo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE,
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = "main.tex"
            tar.addfile(info)
    raw = buf.getvalue()
    return gzip.compress(raw) if gz else raw


@pytest.fixture(autouse=True)
def _never_extract_to_disk(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("unpacking must never extract to disk")

    monkeypatch.setattr(tarfile.TarFile, "extract", refuse)
    monkeypatch.setattr(tarfile.TarFile, "extractall", refuse)


# ---------------------------------------------------------------- unpack


def test_tar_gz_keeps_tex_and_skips_others():
    data = make_tar(
        [
            ("main.tex", MAIN, "file"),
            ("sections", None, "dir"),
            ("sections/intro.tex", INTRO, "file"),
            ("figs/plot.png", b"\x89PNG....", "file"),
            ("refs.BIB", b"@article{x}", "file"),
            ("./style/my.sty", b"% sty", "file"),
        ]
    )
    u = unpack(data, LIMITS, single_name="2401.00001.tex")
    assert u.kind == "tar"
    assert [f.path for f in u.files] == [
        "main.tex",
        "refs.BIB",
        "sections/intro.tex",
        "style/my.sty",
    ]
    assert ("figs/plot.png", "suffix not kept") in u.skipped


def test_plain_tar_without_gzip():
    u = unpack(make_tar([("main.tex", MAIN, "file")], gz=False), LIMITS, single_name="x.tex")
    assert u.kind == "tar" and [f.path for f in u.files] == ["main.tex"]


def test_single_gzipped_tex():
    u = unpack(gzip.compress(MAIN), LIMITS, single_name="2401.00001.tex")
    assert u.kind == "single_tex" and [(f.path, f.data) for f in u.files] == [
        ("2401.00001.tex", MAIN)
    ]


def test_pdf_only():
    u = unpack(b"%PDF-1.5\n...", LIMITS, single_name="x.tex")
    assert u.kind == "pdf" and u.files == []


def test_unknown_payload():
    u = unpack(gzip.compress(b"\x00\x01binary junk"), LIMITS, single_name="x.tex")
    assert u.kind == "unknown" and u.files == []


@pytest.mark.parametrize(
    "name",
    [
        "../evil.tex",
        "a/../../evil.tex",
        "/etc/evil.tex",
        "C:/evil.tex",
        "C:\\evil.tex",
        "a\\..\\evil.tex",
    ],
)
def test_traversal_and_absolute_paths_reject_the_archive(name):
    data = make_tar([("main.tex", MAIN, "file"), (name, b"x", "file")])
    with pytest.raises(UnsafeArchive):
        unpack(data, LIMITS, single_name="x.tex")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_links_and_devices_are_skipped(kind):
    data = make_tar([("main.tex", MAIN, "file"), ("link.tex", None, kind)])
    u = unpack(data, LIMITS, single_name="x.tex")
    assert [f.path for f in u.files] == ["main.tex"]
    assert ("link.tex", f"not a regular file ({kind})") in u.skipped


def test_oversized_file_skipped():
    data = make_tar([("main.tex", MAIN, "file"), ("big.tex", b"%" * 10_001, "file")])
    u = unpack(data, LIMITS, single_name="x.tex")
    assert [f.path for f in u.files] == ["main.tex"]
    assert any(p == "big.tex" and "larger" in r for p, r in u.skipped)


def test_total_size_cap():
    # Plain tar, so the decompression cap (tested by the gzip bomb) doesn't fire first.
    members = [(f"f{i}.tex", b"%" * 9_000, "file") for i in range(7)]  # 63 kB > 50 kB
    with pytest.raises(UnsafeArchive, match="total size of members"):
        unpack(make_tar(members, gz=False), LIMITS, single_name="x.tex")


def test_member_count_cap():
    members = [(f"f{i}.tex", b"%", "file") for i in range(51)]
    with pytest.raises(UnsafeArchive, match="max_members"):
        unpack(make_tar(members, gz=False), LIMITS, single_name="x.tex")


def test_gzip_bomb():
    bomb = gzip.compress(b"\0" * 5_000_000)  # tiny compressed, huge expanded
    assert len(bomb) < 10_000
    with pytest.raises(UnsafeArchive, match="decompress"):
        unpack(bomb, LIMITS, single_name="x.tex")


def test_duplicate_member_names():
    data = make_tar([("main.tex", MAIN, "file"), ("./main.tex", b"% other", "file")])
    u = unpack(data, LIMITS, single_name="x.tex")
    assert [f.data for f in u.files] == [MAIN]
    assert ("main.tex", "duplicate path") in u.skipped


# ---------------------------------------------------------------- main file


def test_find_main():
    files = {
        "appendix.tex": b"\\section{A}",
        "z/paper.tex": b"\\documentclass{article}\n\\begin{document}\n",
        "template.tex": b"% \\documentclass{article} commented out\n",
        "standalone.tex": b"\\documentclass{standalone}\n",  # no \\begin{document}
    }
    assert find_main(files) == "z/paper.tex"
    files["main.tex"] = b"\\documentclass{article}\n\\begin{document}\n"
    assert find_main(files) == "main.tex"
    assert find_main({"a.tex": b"\\section{x}"}) is None


# ---------------------------------------------------------------- fetch_source


def _serve(payload: bytes, status: int = 200):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=payload)

    return httpx.MockTransport(handler), requests


def test_fetch_source_registers_one_source_per_file(fetcher_for, ledger):
    archive = make_tar([("main.tex", MAIN, "file"), ("sections/intro.tex", INTRO, "file")])
    transport, requests = _serve(archive)
    f = fetcher_for(transport)
    res = fetch_source(f, ledger, "2401.00001", 2)
    assert str(requests[0].url) == "https://arxiv.org/e-print/2401.00001v2"
    assert res.kind == "tar" and res.main == "main.tex"
    by_path = dict(res.sources)
    assert set(by_path) == {"main.tex", "sections/intro.tex"}
    blob, origin = ledger.conn.execute(
        "SELECT blob_sha256, origin_json FROM sources WHERE id = ?", (by_path["main.tex"],)
    ).fetchone()
    assert ledger.blobs.get(blob) == MAIN  # bytes exactly as served: offsets will refer to these
    assert '"version":"v2"' in origin and '"path":"main.tex"' in origin and '"archive":' in origin
    again = fetch_source(f, ledger, "2401.00001", 2)
    assert again.sources == res.sources and len(requests) == 1


def test_fetch_source_not_found(fetcher_for, ledger):
    transport, _ = _serve(b"no such paper", status=404)
    res = fetch_source(fetcher_for(transport), ledger, "2401.99999", 1)
    assert res.kind == "not_found" and res.sources == []


def test_fetch_source_pdf_only(fetcher_for, ledger):
    transport, _ = _serve(b"%PDF-1.4 ...")
    res = fetch_source(fetcher_for(transport), ledger, "2401.00002", 1)
    assert res.kind == "pdf" and res.sources == []
