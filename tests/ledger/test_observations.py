"""Observations: who asserted a node, one row per distinct (node, producer)."""

import pytest

from tests.ledger.helpers import prior
from themis.ledger.canonical import content_hash
from themis.ledger.dao import Ledger
from themis.ledger.errors import ValidationError

EXTRACTOR = {"role": "extractor", "model": "provider-a/model-x", "prompt_sha256": "ab" * 32}
CRITIC = {"role": "critic", "model": "provider-b/model-y"}


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    yield lg
    lg.close()


def test_add_node_records_observation(ledger):
    nid = ledger.add_node(prior(), EXTRACTOR)
    [obs] = ledger.observations(nid)
    assert obs.node_id == nid
    assert obs.producer == EXTRACTOR
    assert obs.producer_sha256 == content_hash(EXTRACTOR)
    assert obs.observed_at


def test_same_producer_twice_one_observation(ledger):
    a = ledger.add_node(prior(), EXTRACTOR)
    first = ledger.observations(a)[0].observed_at
    ledger.add_node(prior(), dict(reversed(EXTRACTOR.items())))  # key order irrelevant
    obs = ledger.observations(a)
    assert len(obs) == 1
    assert obs[0].observed_at == first


def test_two_producers_one_node_two_observations(ledger):
    a = ledger.add_node(prior(), EXTRACTOR)
    assert ledger.add_node(prior(), CRITIC) == a
    assert {o.producer["role"] for o in ledger.observations(a)} == {"extractor", "critic"}


@pytest.mark.parametrize("bad", [{}, {"role": ""}, {"role": 3}, {"model": "m"}, "extractor"])
def test_producer_requires_role(ledger, bad):
    with pytest.raises(ValidationError):
        ledger.add_node(prior(), bad)
    assert ledger.conn.execute("SELECT count(*) FROM nodes").fetchone()[0] == 0


def test_supersede_records_observation(ledger):
    a = ledger.add_node(prior("v1"), EXTRACTOR)
    b = ledger.supersede(a, prior("v2"), CRITIC)
    assert [o.producer for o in ledger.observations(b)] == [CRITIC]
