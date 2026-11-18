from typing import ClassVar

import pytest
from pydantic import BaseModel

from tests.router.fake import Clock, Fake
from themis.config import Env, load_config
from themis.ledger.dao import Ledger
from themis.ops import OpsDB
from themis.router.client import Router, TemplateRef

ROUTER_TOML = """
[defaults]
temperature = 0
timeout_s = 30
max_backoff_s = 10
max_retries_per_entry = 2
busy_timeout_ms = 5000
max_concurrency = 2

[network]
allowed_hosts = ["a.example", "b.example", "c.example"]

[providers.pa]
base_url = "https://a.example/v1/"
key_env = "KEY_A"
[providers.pb]
base_url = "https://b.example/v1/"
key_env = "KEY_B"
[providers.pc]
base_url = "https://c.example/v1/"
key_env = "KEY_C"

[[roles.extractor]]
provider = "pa"
model = "ext-1"
family = "fa"
[[roles.extractor]]
provider = "pb"
model = "ext-2"
family = "fb"
[[roles.extractor]]
provider = "pc"
model = "ext-3"
family = "fc"

[[roles.reasoner]]
provider = "pa"
model = "rea-1"
family = "fa"

[[roles.critic]]
provider = "pb"
model = "cri-1"
family = "fb"

[[roles.embedder]]
provider = "pc"
model = "emb-1"
family = "fe"
dims = 3
[[roles.embedder]]
provider = "pa"
model = "emb-2"
family = "fe2"
dims = 3

[sources.arxiv]
min_interval_s = 0
[sources.openalex]
mailto_env = "MAILTO"
[sources.semantic_scholar]
enabled = false
key_env = "S2_KEY"
"""

SECRETS = {"KEY_A": "sk-secret-aaaa", "KEY_B": "sk-secret-bbbb", "KEY_C": "sk-secret-cccc"}
TEMPLATE = TemplateRef("label_table", "abc123")


class Answer(BaseModel):
    SCHEMA_VERSION: ClassVar[int] = 1
    answer: str


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "providers.toml"
    p.write_text(ROUTER_TOML, encoding="utf-8")
    return load_config(p, Env(SECRETS))


@pytest.fixture
def fake():
    return Fake()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def ledger(tmp_path):
    lg = Ledger.open(tmp_path / "data")
    yield lg
    lg.close()


@pytest.fixture
def ops(tmp_path):
    db = OpsDB(tmp_path / "data" / "ops.db")
    yield db
    db.close()


@pytest.fixture
def make_router(cfg, fake, clock, ledger, ops):
    def make(**kw):
        return Router(
            cfg,
            Env(SECRETS),
            ledger,
            ops,
            transport=kw.get("transport", fake.transport),
            clock=clock.now,
            sleep=clock.sleep,
            jitter=lambda: 0.0,
        )

    return make


@pytest.fixture
def router(make_router):
    return make_router()
