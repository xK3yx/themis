"""Read-side admissibility: priors and superseded nodes never reach conclusions,
and inadmissibility propagates through derivations."""

import pytest

from tests.ledger.helpers import PRODUCER, add_paper, add_run, cited, derived, measured, prior
from themis.ledger.dao import Ledger


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


def test_prior_never_admissible(ledger):
    p = add(ledger, prior())
    assert not ledger.is_admissible(p)
    reasons = ledger.why_inadmissible(p)
    assert any(p in r and "prior" in r for r in reasons)


def test_cited_and_measured_admissible(ledger, src):
    assert ledger.is_admissible(add(ledger, cited(src)))
    assert ledger.is_admissible(add(ledger, measured(add_run(ledger))))
    assert ledger.why_inadmissible(add(ledger, cited(src, claim="z"))) == []


def test_superseded_inadmissible_head_admissible(ledger, src):
    a = add(ledger, cited(src, claim="v1"))
    b = ledger.supersede(a, cited(src, claim="v2"), PRODUCER)
    assert not ledger.is_admissible(a)
    assert any(b in r for r in ledger.why_inadmissible(a))
    assert ledger.is_admissible(b)


def test_derived_from_admissible_parents(ledger, src):
    a, b = add(ledger, cited(src, claim="a")), add(ledger, cited(src, claim="b"))
    assert ledger.is_admissible(add(ledger, derived([a, b])))


def test_derived_with_prior_parent_inadmissible(ledger, src):
    p, c = add(ledger, prior()), add(ledger, cited(src))
    d = add(ledger, derived([p, c]))
    assert not ledger.is_admissible(d)
    assert any(p in r for r in ledger.why_inadmissible(d))


def test_admissibility_is_transitive(ledger, src):
    a, b = add(ledger, cited(src, claim="a")), add(ledger, cited(src, claim="b"))
    d = add(ledger, derived([a, b]))
    assert ledger.is_admissible(d)
    a2 = ledger.supersede(a, cited(src, claim="a-fixed"), PRODUCER)
    assert not ledger.is_admissible(d)
    reasons = ledger.why_inadmissible(d)
    assert any(a in r for r in reasons) and any(a2 in r for r in reasons)


def test_inadmissibility_propagates_through_chain(ledger, src):
    p = add(ledger, prior())
    c1, c2 = add(ledger, cited(src, claim="1")), add(ledger, cited(src, claim="2"))
    d1 = add(ledger, derived([p, c1], claim="d1"))
    d2 = add(ledger, derived([d1, c2], claim="d2"))
    assert not ledger.is_admissible(d2)
    reasons = ledger.why_inadmissible(d2)
    assert any(d1 in r for r in reasons) and any(p in r for r in reasons)


def test_diamond_dag_reasons_not_duplicated(ledger):
    p = add(ledger, prior("root"))
    q = add(ledger, prior("other"))
    x = add(ledger, derived([p, q], claim="x"))
    y = add(ledger, derived([p, q], claim="y"))
    z = add(ledger, derived([x, y], claim="z"))
    reasons = ledger.why_inadmissible(z)
    assert len(reasons) == len(set(reasons))
