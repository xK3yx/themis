from pathlib import Path

import pytest

from themis.config import Env, load_config
from themis.ingest.http import Fetcher
from themis.ledger.dao import Ledger

ROOT = Path(__file__).parents[2]


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    yield lg
    lg.close()


@pytest.fixture
def fetcher_for(ledger):
    """A Fetcher on the committed config, with a given (replay) transport and no sleeping."""
    cfg = load_config(ROOT / "config" / "providers.toml", Env({}), require_enabled=False)

    def make(transport):
        return Fetcher(cfg, Env({}), ledger, transport=transport, sleep=lambda s: None)

    return make
