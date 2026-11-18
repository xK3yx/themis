"""Claim provenance (M1 spec §2.5): V21-V23 in the DB, V24 (bytes) in the DAO.

A claim is a `derived` node with rule `table_result`. Every `from` must be a parent (V21),
its value must come from a table cell (V22) whose parsed number it equals (V23), and every
number must re-parse from the exact bytes it came from (V24).
"""

import copy
import json
import sqlite3

import pytest

from tests.ledger.conftest import NOW, hx
from tests.ledger.helpers import PRODUCER
from themis.ledger.dao import Ledger
from themis.ledger.errors import ValidationError

TEX = (
    b"\\section{Setup} We use rank $r = 8$ for all runs.\n"
    b"\\begin{tabular}{lcc} Method & MNLI & SST-2 \\\\ "
    b"LoRA & 71.2 $\\pm$ 0.3 & 94.1 \\\\ "
    b"Full FT & 70.9 & 93.8 \\end{tabular}\n"
)


def _span(data: bytes, text: str, after: int = 0) -> dict:
    start = data.index(text.encode(), after)
    return {"type": "span", "start": start, "end": start + len(text.encode()), "text": text}


class Paper:
    """Builds atoms and claims over TEX through the DAO."""

    def __init__(self, ledger: Ledger):
        self.lg = ledger
        blob = ledger.blobs.put(TEX, "application/x-tex")
        self.src = ledger.add_source(blob, {"arxiv": "0000.00001", "version": "v1"})
        self.table = _span(TEX, TEX.decode()[TEX.decode().index("\\begin{tabular}") :].rstrip("\n"))

    def cell_node(self, cell_text, row, col, number=None):
        cell = _span(TEX, cell_text, self.table["start"])
        return {
            "kind": "cited",
            "content": {
                "type": "table_cell",
                "raw": cell_text,
                "number": number,
                "style": {},
                "missing": None,
                "parser_version": "test",
            },
            "provenance": {
                "source": self.src,
                "locator": {
                    "type": "table_cell",
                    "table": self.table,
                    "cell": cell,
                    "row": row,
                    "col": col,
                },
            },
        }

    def num(self, literal, value, cell_text, pm=None, pm_literal=None):
        cell = _span(TEX, cell_text, self.table["start"])
        start = TEX.index(literal.encode(), cell["start"])
        n = {
            "value": value,
            "pm": pm,
            "percent": False,
            "span": {"type": "span", "start": start, "end": start + len(literal), "text": literal},
            "pm_span": None,
        }
        if pm_literal is not None:
            p = TEX.index(pm_literal.encode(), start + len(literal))
            n["pm_span"] = {
                "type": "span",
                "start": p,
                "end": p + len(pm_literal),
                "text": pm_literal,
            }
        return n

    def add(self, node):
        return self.lg.add_node(node, PRODUCER)

    def atoms(self):
        lora_val = "71.2 $\\pm$ 0.3"
        self.value = self.add(
            self.cell_node(lora_val, 1, 1, self.num("71.2", 71.2, lora_val, 0.3, "0.3"))
        )
        self.method = self.add(self.cell_node("LoRA", 1, 0))
        self.metric = self.add(self.cell_node("MNLI", 0, 1))
        rank_quote = _span(TEX, "rank $r = 8$")
        rank_num = {
            "value": 8,
            "pm": None,
            "percent": False,
            "pm_span": None,
            "span": _span(TEX, "8", rank_quote["start"]),
        }
        self.rank = self.add(
            {
                "kind": "cited",
                "content": {"type": "text_span", "section": "Setup", "number": rank_num},
                "provenance": {"source": self.src, "locator": rank_quote},
            }
        )

    def claim(self, **overrides):
        content = {
            "type": "result",
            "evidence_grade": "table_cell",
            "value": {
                "number": 71.2,
                "pm": 0.3,
                "pm_meaning": "std",
                "percent": False,
                "from": self.value,
            },
            "method": {"text": "LoRA", "from": self.method},
            "metric": {"text": "MNLI", "direction": "higher", "from": self.metric},
            "regime": {
                "lora_rank": {"value": 8, "unit": None, "text": "rank $r = 8$", "from": self.rank}
            },
        }
        for path, val in overrides.items():
            target = content
            *head, last = path.split(".")
            for k in head:
                target = target[k]
            target[last] = val
        parents = overrides.pop("_parents", None) or sorted(
            {self.value, self.method, self.metric, self.rank}
        )
        return {
            "kind": "derived",
            "content": content,
            "provenance": {"parents": parents, "rule": "table_result"},
        }


@pytest.fixture
def paper(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    p = Paper(lg)
    p.atoms()
    yield p
    lg.close()


# ---------------------------------------------------------------- valid path


def test_valid_claim_accepted(paper):
    cid = paper.add(paper.claim())
    assert paper.lg.is_admissible(cid)
    assert {n.id for n in paper.lg.parents(cid)} == {
        paper.value,
        paper.method,
        paper.metric,
        paper.rank,
    }


def test_number_literal_grammar_in_atoms(paper):
    # 94.1 has no pm; its literal span is exactly "94.1".
    paper.add(paper.cell_node("94.1", 1, 2, paper.num("94.1", 94.1, "94.1")))


# ---------------------------------------------------------------- V24 on atoms (DAO)


def test_atom_number_must_reparse_from_bytes(paper):
    bad = paper.num("70.9", 70.9, "70.9")
    bad["value"] = 70.8
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node("70.9", 2, 1, bad))


def test_atom_number_span_text_must_match_bytes(paper):
    bad = paper.num("70.9", 70.9, "70.9")
    bad["span"]["text"] = "70.8"
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node("70.9", 2, 1, bad))


def test_atom_number_span_inside_cell(paper):
    bad = paper.num("70.9", 70.9, "70.9")
    bad["span"] = _span(TEX, "71.2")  # a real literal, but in another cell
    bad["value"] = 71.2
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node("70.9", 2, 1, bad))


def test_atom_number_needs_span(paper):
    bad = paper.num("70.9", 70.9, "70.9")
    del bad["span"]
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node("70.9", 2, 1, bad))


def test_atom_pm_must_reparse(paper):
    cell = "71.2 $\\pm$ 0.3"
    bad = paper.num("71.2", 71.2, cell, pm=0.4, pm_literal="0.3")
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node(cell, 1, 1, bad))


def test_atom_pm_needs_span(paper):
    cell = "71.2 $\\pm$ 0.3"
    bad = paper.num("71.2", 71.2, cell, pm=0.3)  # no pm_span
    with pytest.raises(ValidationError, match="V24"):
        paper.add(paper.cell_node(cell, 1, 1, bad))


# ---------------------------------------------------------------- claims via the DAO


def test_from_must_be_a_parent(paper):
    other = paper.add(paper.cell_node("Full FT", 2, 0))
    node = paper.claim(**{"method.from": other})
    with pytest.raises(ValidationError, match="V21"):
        paper.add(node)


def test_caption_from_must_be_a_parent(paper):
    node = paper.claim(source_table={"label": "tab:main", "caption_from": hx("ghost")})
    with pytest.raises(ValidationError, match="V21"):
        paper.add(node)


def test_value_must_come_from_a_table_cell(paper):
    node = paper.claim(**{"value.from": paper.rank, "value.number": 8, "value.pm": None})
    with pytest.raises(ValidationError, match="V22"):
        paper.add(node)


def test_value_must_equal_cell_number(paper):
    with pytest.raises(ValidationError, match="V23"):
        paper.add(paper.claim(**{"value.number": 71.3}))


def test_pm_must_equal_cell_pm(paper):
    with pytest.raises(ValidationError, match="V23"):
        paper.add(paper.claim(**{"value.pm": None}))


def test_regime_number_must_match_its_atom(paper):
    node = paper.claim(**{"regime.lora_rank.value": 16})
    with pytest.raises(ValidationError, match="V24"):
        paper.add(node)


def test_regime_number_from_atom_without_number(paper):
    node = paper.claim(**{"regime.lora_rank.from": paper.method})  # "LoRA" has no number
    with pytest.raises(ValidationError, match="V24"):
        paper.add(node)


def test_db_backstop_for_claims(paper, monkeypatch):
    """With every DAO check off, the DB still rejects V21-V23 violations."""
    monkeypatch.setattr(Ledger, "_validate", lambda self, node: None)
    with pytest.raises(ValidationError, match="V23"):
        paper.add(paper.claim(**{"value.number": 71.3}))


# ---------------------------------------------------------------- raw SQL (V21-V23)


def _raw_claim(conn, content, parents):
    conn.execute(
        "INSERT INTO nodes(id, kind, schema_version, content_json, provenance_json, created_at)"
        " VALUES (?, 'derived', 1, ?, ?, ?)",
        (
            hx(json.dumps(content) + str(parents)),
            json.dumps(content),
            json.dumps({"parents": parents, "rule": "table_result"}),
            NOW,
        ),
    )


@pytest.fixture
def raw_claim(paper):
    conn = paper.lg.conn
    base = paper.claim()
    return conn, base["content"], base["provenance"]["parents"]


def test_raw_valid_claim(raw_claim):
    conn, content, parents = raw_claim
    _raw_claim(conn, content, parents)


def test_raw_v21(raw_claim, paper):
    conn, content, parents = raw_claim
    c = copy.deepcopy(content)
    c["setting"] = [{"text": "r=8", "from": hx("ghost")}]
    with pytest.raises(sqlite3.IntegrityError, match="V21"):
        _raw_claim(conn, c, parents)


def test_raw_v21_non_text_from(raw_claim):
    conn, content, parents = raw_claim
    c = copy.deepcopy(content)
    c["method"]["from"] = 3
    with pytest.raises(sqlite3.IntegrityError, match="V21"):
        _raw_claim(conn, c, parents)


def test_raw_v22(raw_claim, paper):
    conn, content, parents = raw_claim
    c = copy.deepcopy(content)
    c["value"]["from"] = paper.rank
    with pytest.raises(sqlite3.IntegrityError, match="V22"):
        _raw_claim(conn, c, parents)
    del c["value"]["from"]
    with pytest.raises(sqlite3.IntegrityError, match="V22"):
        _raw_claim(conn, c, parents)


@pytest.mark.parametrize(
    "change",
    [{"number": 71.3}, {"pm": None}, {"pm": 0.4}, {"number": "71.2"}, {"number": None}],
)
def test_raw_v23(raw_claim, change):
    conn, content, parents = raw_claim
    c = copy.deepcopy(content)
    c["value"].update(change)
    with pytest.raises(sqlite3.IntegrityError, match="V23"):
        _raw_claim(conn, c, parents)


def test_other_derived_rules_unaffected(paper):
    """V21-V23 apply to claims (rule table_result), not to every derivation."""
    paper.add(
        {
            "kind": "derived",
            "content": {"note": "free-form content", "from": "not-checked"},
            "provenance": {"parents": sorted([paper.method, paper.metric]), "rule": "pool_mean"},
        }
    )
