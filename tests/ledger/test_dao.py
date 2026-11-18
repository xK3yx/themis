"""DAO tests: readable validation, idempotency, append-only corrections, hash verification."""

import hashlib
import inspect
import sqlite3

import pytest

from tests.ledger.helpers import (
    PAPER,
    PRODUCER,
    add_paper,
    add_run,
    cited,
    derived,
    measured,
    prior,
    results,
    span_of,
)
from themis.ledger.dao import Ledger
from themis.ledger.errors import (
    LedgerCorruptError,
    NotFoundError,
    ValidationError,
)
from themis.ledger.models import hashable, node_id, parse_node


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    yield lg
    lg.close()


@pytest.fixture
def src(ledger):
    return add_paper(ledger)


def add(ledger, node):
    return ledger.add_node(node, PRODUCER)


# ---------------------------------------------------------------- Invariant 1 via the DAO


def test_cited_valid_roundtrip(ledger, src):
    nid = add(ledger, cited(src))
    stored = ledger.get(nid)
    assert stored.id == nid
    assert stored.kind == "cited"
    assert stored.node.provenance.locator.text == "rank 8 reaches 71.2"


def test_layout_on_disk(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    sha = lg.blobs.put(b"x", "text/plain")
    lg.close()
    assert (tmp_path / "data" / "ledger.db").is_file()
    assert (tmp_path / "data" / "store" / sha[:2] / sha).is_file()


def test_cited_unknown_source(ledger):
    with pytest.raises(ValidationError, match="V4"):
        add(ledger, cited("0" * 64))


def test_add_source_requires_registered_blob(ledger):
    with pytest.raises(NotFoundError):
        ledger.add_source("0" * 64, {"arxiv": "x"})


def test_cited_span_out_of_blob(ledger, src):
    node = cited(src)
    n = len(PAPER)
    node["provenance"]["locator"] = {"type": "span", "start": n - 2, "end": n + 3, "text": "abcde"}
    with pytest.raises(ValidationError, match="V5"):
        add(ledger, node)


@pytest.mark.parametrize(
    "loc",
    [
        {"type": "span", "start": 5, "end": 5, "text": ""},
        {"type": "span", "start": -1, "end": 4, "text": "abcde"},
        {"type": "span", "start": 0, "end": 3, "text": "toolong"},  # V6 length
    ],
)
def test_cited_span_malformed(ledger, src, loc):
    node = cited(src)
    node["provenance"]["locator"] = loc
    with pytest.raises(ValidationError):
        add(ledger, node)


def test_span_text_must_match_blob(ledger, src):
    node = cited(src)
    loc = span_of("rank 8 reaches 71.2")
    loc["text"] = "rank 9 reaches 71.2"  # same byte length, different bytes
    node["provenance"]["locator"] = loc
    with pytest.raises(ValidationError, match="V7"):
        add(ledger, node)


def test_span_multibyte_utf8(ledger, src):
    text = "Café-style ablation: r ≥ 16"
    loc = span_of(text)
    assert loc["end"] - loc["start"] == len(text) + 1 + 2  # é: +1 byte, ≥: +2 bytes
    add(ledger, cited(src, text=text))


def test_span_character_offsets_rejected(ledger, src):
    text = "r ≥ 16 gives no gain"
    char_start = PAPER.decode().index(text)
    node = cited(src)
    node["provenance"]["locator"] = {
        "type": "span",
        "start": char_start,
        "end": char_start + len(text.encode()),
        "text": text,
    }
    with pytest.raises(ValidationError, match="V7"):
        add(ledger, node)


TABLE_TEXT = "\\begin{tabular}{lc} Method & Acc \\\\ LoRA & 71.2 \\end{tabular}"


def _cell_node(src, cell_text="71.2", occurrence_in_table=True):
    table = span_of(TABLE_TEXT)
    start = PAPER.index(cell_text.encode(), table["start"] if occurrence_in_table else 0)
    cell = {
        "type": "span",
        "start": start,
        "end": start + len(cell_text.encode()),
        "text": cell_text,
    }
    node = cited(src)
    node["provenance"]["locator"] = {
        "type": "table_cell",
        "table": table,
        "cell": cell,
        "row": 1,
        "col": 1,
    }
    return node


def test_table_cell(ledger, src):
    node = _cell_node(src)
    add(ledger, node)
    node["provenance"]["locator"]["row"] = -1
    with pytest.raises(ValidationError):
        add(ledger, node)


def test_table_cell_requires_cell(ledger, src):
    node = _cell_node(src)
    del node["provenance"]["locator"]["cell"]
    with pytest.raises(ValidationError):
        add(ledger, node)


def test_table_cell_span_outside_table(ledger, src):
    # "71.2" also appears in the prose before the table: right bytes, wrong place.
    node = _cell_node(src, occurrence_in_table=False)
    with pytest.raises(ValidationError, match="V5b"):
        add(ledger, node)


def test_table_cell_bytes_must_match(ledger, src):
    node = _cell_node(src)
    node["provenance"]["locator"]["cell"]["text"] = "71.3"  # same length, wrong bytes
    with pytest.raises(ValidationError, match="V7b"):
        add(ledger, node)


def test_measured_requires_run(ledger):
    with pytest.raises(ValidationError, match="V8"):
        add(ledger, measured("0" * 64))


def test_measured_rejects_two_seeds(ledger):
    run = add_run(ledger, seeds=(1, 2))
    with pytest.raises(ValidationError, match="V9"):
        add(ledger, measured(run))


def test_measured_accepts_three_seeds(ledger):
    run = add_run(ledger, seeds=(1, 2, 3))
    add(ledger, measured(run))


def test_run_seeds_distinct(ledger):
    with pytest.raises(ValidationError, match="V10"):
        add_run(ledger, seeds=(1, 1, 2), metrics={})


@pytest.mark.parametrize("seeds", [[], [True, 2, 3], [1, 2, 3.0], [1, 2, "3"]])
def test_run_seeds_must_be_ints(ledger, seeds):
    with pytest.raises(ValidationError, match="V10"):
        ledger.add_run(b"{}", results({}), seeds)


def test_run_results_shape(ledger):
    with pytest.raises(ValidationError):
        ledger.add_run(b"{}", b"not json", [1, 2, 3])
    with pytest.raises(ValidationError):
        ledger.add_run(b"{}", b'{"metrics": {"acc": {"1": "high"}}}', [1, 2, 3])
    with pytest.raises(ValidationError):
        ledger.add_run(b"{}", b'{"metrics": {"acc": {"99": 0.5}}}', [1, 2, 3])


def test_measured_metric_all_seeds(ledger):
    run = add_run(ledger, metrics={"acc": {1: 0.7, 2: 0.71}})  # seed 3 missing
    with pytest.raises(ValidationError, match="V11"):
        add(ledger, measured(run, metric="acc"))
    with pytest.raises(ValidationError, match="V11"):
        add(ledger, measured(run, metric="f1"))


def test_derived_rejects_one_parent(ledger):
    a = add(ledger, prior())
    with pytest.raises(ValidationError):
        add(ledger, derived([a]))


def test_derived_rejects_duplicate_parents(ledger):
    a = add(ledger, prior())
    with pytest.raises(ValidationError):
        add(ledger, derived([a, a]))


def test_derived_rejects_missing_parent(ledger):
    a = add(ledger, prior())
    with pytest.raises(ValidationError, match="V13"):
        add(ledger, derived([a, "f" * 64]))


def test_derived_rejects_superseded_parent(ledger):
    a, b = add(ledger, prior("a")), add(ledger, prior("b"))
    ledger.supersede(a, prior("a2"), PRODUCER)
    with pytest.raises(ValidationError, match="V14"):
        add(ledger, derived([a, b]))


def test_derived_allows_prior_parent(ledger, src):
    a, b = add(ledger, prior()), add(ledger, cited(src))
    d = add(ledger, derived([a, b]))
    assert [p.id for p in ledger.parents(d)] == [a, b]


def test_derived_rule_must_be_identifier(ledger):
    a, b = add(ledger, prior("a")), add(ledger, prior("b"))
    with pytest.raises(ValidationError):
        add(ledger, derived([a, b], rule="Average it"))


def test_unknown_provenance_key_rejected(ledger):
    node = prior()
    node["provenance"]["note"] = "trust me"
    with pytest.raises(ValidationError):
        add(ledger, node)


def test_db_is_the_backstop(ledger, monkeypatch):
    """With DAO checks disabled, the database still rejects the node."""
    run = add_run(ledger, seeds=(1, 2))
    monkeypatch.setattr(Ledger, "_validate", lambda self, node: None)
    with pytest.raises(ValidationError, match="V9"):
        add(ledger, measured(run))


# ---------------------------------------------------------------- Invariant 2 via the DAO


def test_dao_has_no_mutators():
    banned = ("update", "delete", "remove", "replace", "set", "edit", "drop", "clear")
    public = [n for n, _ in inspect.getmembers(Ledger) if not n.startswith("_")]
    assert not [n for n in public if n.startswith(banned)]


def _raw_row(ledger, nid):
    return ledger.conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone()


def test_supersede_creates_new_node(ledger):
    a = add(ledger, prior("old"))
    before = _raw_row(ledger, a)
    b = ledger.supersede(a, prior("new"), PRODUCER)
    assert b != a
    assert _raw_row(ledger, a) == before
    assert ledger.successor(a) == b
    assert ledger.get(b).node.provenance.supersedes == a


def test_supersede_once_only(ledger):
    a = add(ledger, prior("old"))
    ledger.supersede(a, prior("fix1"), PRODUCER)
    with pytest.raises(ValidationError, match="V15"):
        ledger.supersede(a, prior("fix2"), PRODUCER)


def test_second_correction_supersedes_first(ledger):
    a = add(ledger, prior("v1"))
    b = ledger.supersede(a, prior("v2"), PRODUCER)
    c = ledger.supersede(b, prior("v3"), PRODUCER)
    assert ledger.current(a) == c
    assert ledger.current(b) == c
    assert ledger.current(c) == c


def test_supersede_unknown_target(ledger):
    with pytest.raises(NotFoundError):
        ledger.supersede("e" * 64, prior(), PRODUCER)


def test_supersede_conflicting_field(ledger):
    a, b = add(ledger, prior("a")), add(ledger, prior("b"))
    node = prior("c")
    node["provenance"]["supersedes"] = b
    with pytest.raises(ValidationError):
        ledger.supersede(a, node, PRODUCER)


def test_correction_may_change_kind(ledger, src):
    a = add(ledger, prior("guess"))
    b = ledger.supersede(a, cited(src), PRODUCER)
    assert ledger.get(b).kind == "cited"


# ---------------------------------------------------------------- Invariant 3 via the DAO


GOLDEN_BYTES = (
    b'{"content":{"claim":"lora rank 8 suffices"},"kind":"prior",'
    b'"provenance":{"asserted_by":"reasoner"},"schema_version":1}'
)
GOLDEN_ID = "ef8e9dcaf113c8793b3f20f399269b100f5d57d929f0d43c489d72a6b39aaaa7"


def test_id_golden_vector():
    node = parse_node(prior("lora rank 8 suffices"))
    assert hashlib.sha256(GOLDEN_BYTES).hexdigest() == GOLDEN_ID
    assert node_id(node) == GOLDEN_ID


def test_id_includes_schema_version():
    node = parse_node(prior())
    assert node_id(node, schema_version=1) != node_id(node, schema_version=2)


def test_id_omits_absent_supersedes():
    node = parse_node(prior())
    assert "supersedes" not in hashable(node)["provenance"]


def test_id_changes_with_content_and_provenance(src):
    base = node_id(parse_node(cited(src)))
    assert node_id(parse_node(cited(src, claim="other"))) != base
    assert node_id(parse_node(cited(src, text="71.2 on MNLI"))) != base


def test_reinsert_idempotent(ledger, src):
    a = add(ledger, cited(src))
    b = add(ledger, cited(src))
    assert a == b
    assert ledger.conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1


def test_reinsert_after_parent_superseded_is_still_idempotent(ledger):
    a, b = add(ledger, prior("a")), add(ledger, prior("b"))
    d = add(ledger, derived([a, b]))
    ledger.supersede(a, prior("a2"), PRODUCER)
    assert add(ledger, derived([a, b])) == d


def test_reingest_source_and_run_idempotent(ledger, src):
    assert add_paper(ledger) == src
    assert add_run(ledger) == add_run(ledger)
    assert ledger.add_run(b"{}", results({}), [3, 1, 2]) == ledger.add_run(
        b"{}", results({}), [1, 2, 3]
    )
    assert ledger.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_get_detects_forged_id(ledger):
    forged = "a" * 64
    ledger.conn.execute(
        "INSERT INTO nodes(id, kind, schema_version, content_json, provenance_json, created_at)"
        " VALUES (?, 'prior', 1, '{\"claim\":\"x\"}', '{\"asserted_by\":\"x\"}', 't')",
        (forged,),
    )
    with pytest.raises(LedgerCorruptError):
        ledger.get(forged)
    assert ledger.verify_all() == [forged]


def test_verify_all_clean(ledger, src):
    add(ledger, cited(src))
    add(ledger, prior())
    assert ledger.verify_all() == []


def test_get_unknown(ledger):
    with pytest.raises(NotFoundError):
        ledger.get("0" * 64)


def test_float_content_roundtrips(ledger):
    node = prior()
    node["content"] = {"acc": 0.1 + 0.2, "n": 3, "flag": True, "none": None}
    nid = add(ledger, node)
    assert ledger.get(nid).node.content == node["content"]


def test_non_finite_content_rejected(ledger):
    node = prior()
    node["content"] = {"acc": float("nan")}
    with pytest.raises(ValidationError):
        add(ledger, node)


def test_integrity_error_is_wrapped(ledger):
    assert not issubclass(sqlite3.IntegrityError, ValidationError)
    with pytest.raises(ValidationError):
        add(ledger, measured("0" * 64))
