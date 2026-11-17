"""Raw-SQL tests: the database rejects invalid rows even when the DAO is bypassed."""

import json
import sqlite3

import pytest

from tests.ledger.conftest import NOW, hx

Err = sqlite3.IntegrityError

# ---------------------------------------------------------------- Invariant 1: provenance


def test_valid_rows_accepted(raw):
    c = raw.cited()
    p = raw.prior()
    d = raw.derived([c, p])
    r = raw.run()
    m = raw.node("measured", {"run": r, "metric": "acc"})
    assert all([c, p, d, m])


def test_kind_must_be_known(raw):
    with pytest.raises(Err):
        raw.node("opinion", {"asserted_by": "reasoner"})


def test_provenance_keys_match_kind(raw):
    a, b = raw.prior(), raw.prior()
    src = raw.source()
    with pytest.raises(Err, match="V3"):
        raw.node("cited", {"source": src, "locator": raw.span(), "parents": [a, b]})
    with pytest.raises(Err, match="V3"):
        raw.node("prior", {"asserted_by": "reasoner", "run": hx("r")})


def test_provenance_required_keys(raw):
    with pytest.raises(Err, match="V3"):
        raw.node("prior", {})
    with pytest.raises(Err, match="V3"):
        raw.node("derived", {"parents": [raw.prior(), raw.prior()]})  # no rule


def test_derived_rule_is_identifier(raw):
    a, b = raw.prior(), raw.prior()
    with pytest.raises(Err, match="V3"):
        raw.node("derived", {"parents": [a, b], "rule": "Pool Mean!"})


def test_cited_requires_source_column(raw):
    src = raw.source()
    with pytest.raises(Err):
        raw.node("cited", {"source": src, "locator": raw.span()}, source_id=None)


def test_cited_unknown_source(raw):
    with pytest.raises(Err, match="V4"):
        raw.node("cited", {"source": hx("nope"), "locator": raw.span()})


def test_cited_source_column_matches_provenance(raw):
    s1, s2 = raw.source(), raw.source()
    with pytest.raises(Err, match="V4"):
        raw.node("cited", {"source": s1, "locator": raw.span()}, source_id=s2)


def test_non_cited_cannot_set_source_column(raw):
    src = raw.source()
    with pytest.raises(Err):
        raw.node("prior", {"asserted_by": "x"}, source_id=src)


@pytest.mark.parametrize(
    "span",
    [
        {"type": "span", "start": 96, "end": 101, "text": "hello"},  # end > size (100)
        {"type": "span", "start": 5, "end": 5, "text": ""},  # start >= end
        {"type": "span", "start": -1, "end": 4, "text": "hello"},  # negative
        {"type": "span", "start": "0", "end": 5, "text": "hello"},  # wrong type
        {"type": "span", "start": 0, "end": 5},  # no text
        {"type": "bogus", "start": 0, "end": 5, "text": "hello"},  # unknown locator
    ],
)
def test_cited_span_bounds(raw, span):
    src = raw.source(size=100)
    with pytest.raises(Err, match="V5"):
        raw.node("cited", {"source": src, "locator": span})


def test_span_at_end_of_blob_accepted(raw):
    src = raw.source(size=100)
    raw.node("cited", {"source": src, "locator": raw.span(95, 100, "hello")})


def test_span_text_length_checked_in_db(raw):
    src = raw.source()
    with pytest.raises(Err, match="V6"):
        raw.node("cited", {"source": src, "locator": raw.span(0, 5, "hell")})
    # 'é' is 2 bytes in UTF-8: 5 characters but 6 bytes.
    with pytest.raises(Err, match="V6"):
        raw.node("cited", {"source": src, "locator": raw.span(0, 5, "héllo")})
    raw.node("cited", {"source": src, "locator": raw.span(0, 6, "héllo")})


def test_table_cell_nonnegative(raw):
    src = raw.source()
    table = raw.span(0, 5, "hello")
    ok = {"type": "table_cell", "table": table, "row": 0, "col": 2}
    raw.node("cited", {"source": src, "locator": ok})
    for bad in ({"row": -1, "col": 0}, {"row": 0, "col": -3}, {"row": 1.5, "col": 0}):
        loc = {"type": "table_cell", "table": table, **bad}
        with pytest.raises(Err, match="V5"):
            raw.node("cited", {"source": src, "locator": loc})


def test_measured_requires_run(raw):
    with pytest.raises(Err, match="V8"):
        raw.node("measured", {"run": hx("nope"), "metric": "acc"})
    r = raw.run()
    with pytest.raises(Err):
        raw.node("measured", {"run": r, "metric": "acc"}, run_id=None)


def test_measured_run_column_matches_provenance(raw):
    r1, r2 = raw.run(), raw.run()
    with pytest.raises(Err, match="V8"):
        raw.node("measured", {"run": r1, "metric": "acc"}, run_id=r2)


def test_measured_rejects_two_seeds(raw):
    r = raw.run(seeds=(1, 2))
    with pytest.raises(Err, match="V9"):
        raw.node("measured", {"run": r, "metric": "acc"})


def test_measured_accepts_three_seeds(raw):
    r = raw.run(seeds=(7, 8, 9))
    raw.node("measured", {"run": r, "metric": "acc"})


def test_run_seeds_distinct(raw):
    with pytest.raises(Err, match="V10"):
        raw.run(seeds=(1, 1, 2))


def test_run_seeds_integers(raw):
    with pytest.raises(Err, match="V10"):
        raw.run(seeds=(1, 2, "3"))


def test_n_seeds_is_generated(raw, conn):
    m, r = raw.blob(), raw.blob()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "INSERT INTO runs(id, manifest_blob, results_blob, seeds_json, n_seeds, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (hx("r"), m, r, "[1]", 3, NOW),
        )


def test_derived_rejects_one_parent(raw):
    with pytest.raises(Err, match="V12"):
        raw.derived([raw.prior()])


def test_derived_rejects_duplicate_parents(raw):
    a = raw.prior()
    with pytest.raises(Err, match="V12"):
        raw.derived([a, a])


def test_derived_rejects_missing_parent(raw):
    with pytest.raises(Err, match="V13"):
        raw.derived([raw.prior(), hx("ghost")])


def test_derived_rejects_superseded_parent(raw):
    a, b = raw.prior(), raw.prior()
    raw.supersede(a)
    with pytest.raises(Err, match="V14"):
        raw.derived([a, b])


def test_derived_allows_prior_parent(raw):
    raw.derived([raw.prior(), raw.prior()])


def test_derived_edges_materialised(raw, conn):
    a, b = raw.prior(), raw.cited()
    d = raw.derived([a, b])
    rows = conn.execute("SELECT dst, kind FROM edges WHERE src = ?", (d,)).fetchall()
    assert sorted(rows) == sorted([(a, "derived_from"), (b, "derived_from")])


# ---------------------------------------------------------------- Invariant 2: append-only

TABLES = ["blobs", "sources", "runs", "nodes", "edges", "observations"]


@pytest.fixture
def populated(raw, conn):
    a, b = raw.prior(), raw.cited()
    d = raw.derived([a, b])
    raw.node("measured", {"run": raw.run(), "metric": "acc"})
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?)", (d, hx("p"), '{"role":"test"}', NOW)
    )
    return conn


@pytest.mark.parametrize("table", TABLES)
def test_update_rejected(populated, table):
    col = populated.execute(f"SELECT name FROM pragma_table_info('{table}') LIMIT 1").fetchone()[0]
    with pytest.raises(Err, match="V16"):
        populated.execute(f"UPDATE {table} SET {col} = {col}")


@pytest.mark.parametrize("table", TABLES)
def test_delete_rejected(populated, table):
    with pytest.raises(Err, match="V17"):
        populated.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table", TABLES)
def test_insert_or_replace_rejected(populated, table):
    before = populated.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
    cols = [
        r[1]
        for r in populated.execute(f"PRAGMA table_xinfo('{table}')")
        if r[6] == 0  # skip generated columns
    ]
    row = populated.execute(f"SELECT {','.join(cols)} FROM {table} LIMIT 1").fetchone()
    qs = ",".join("?" * len(cols))
    with pytest.raises(Err):
        populated.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES ({qs})", row)
    assert populated.execute(f"SELECT * FROM {table} LIMIT 1").fetchone() == before


def test_insert_or_replace_cannot_change_node(raw, conn):
    a = raw.prior()
    with pytest.raises(Err, match="V18"):
        conn.execute(
            "INSERT OR REPLACE INTO nodes(id, kind, schema_version, content_json,"
            " provenance_json, created_at) VALUES (?,?,?,?,?,?)",
            (a, "prior", 1, '{"claim":"changed"}', '{"asserted_by":"x"}', NOW),
        )
    content = conn.execute("SELECT content_json FROM nodes WHERE id=?", (a,)).fetchone()[0]
    assert json.loads(content) == {"claim": "x"}


def test_edges_not_insertable_directly(raw, conn):
    a, b = raw.prior(), raw.prior()
    with pytest.raises(Err, match="V19"):
        conn.execute("INSERT INTO edges VALUES (?,?,?)", (a, b, "derived_from"))
    with pytest.raises(Err, match="V19"):
        conn.execute("INSERT INTO edges VALUES (?,?,?)", (a, b, "supersedes"))


def test_supersede_edge_materialised(raw, conn):
    a = raw.prior()
    b = raw.supersede(a)
    rows = conn.execute("SELECT src FROM edges WHERE dst=? AND kind='supersedes'", (a,))
    assert rows.fetchall() == [(b,)]


def test_supersede_once_only(raw):
    a = raw.prior()
    raw.supersede(a)
    with pytest.raises(Err, match="V15"):
        raw.node("prior", {"asserted_by": "critic", "supersedes": a})


def test_supersede_unknown_target(raw):
    with pytest.raises(Err, match="V15"):
        raw.supersede(hx("ghost"))


def test_cannot_derive_from_node_being_superseded(raw):
    a, b = raw.prior(), raw.prior()
    with pytest.raises(Err, match="V15"):
        raw.node("derived", {"parents": [a, b], "rule": "fix", "supersedes": a})


# ---------------------------------------------------------------- Invariant 3: id format


@pytest.mark.parametrize("bad", ["abc", "G" * 64, "A" * 64, "a" * 63, "a" * 65])
def test_id_format_check(raw, bad):
    with pytest.raises(Err):
        raw.prior(node_id=bad)


@pytest.mark.parametrize("table", ["blobs", "sources", "runs", "nodes"])
def test_primary_key_not_null(conn, table):
    # SQLite allows NULL in non-integer primary keys unless NOT NULL is explicit,
    # and CHECK constraints pass when they evaluate to NULL.
    notnull = {
        r[1]: r[3] for r in conn.execute(f"PRAGMA table_info('{table}')") if r[5]  # pk cols
    }
    assert notnull and all(notnull.values())


# ---------------------------------------------------------------- Observations


def test_observation_requires_role(raw, conn):
    a = raw.prior()
    with pytest.raises(Err):
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?)", (a, hx("p"), '{"model":"m"}', NOW)
        )


def test_observation_requires_existing_node(conn):
    with pytest.raises(Err):
        conn.execute(
            "INSERT INTO observations VALUES (?,?,?,?)",
            (hx("ghost"), hx("p"), '{"role":"test"}', NOW),
        )
