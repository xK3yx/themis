"""Config loading (M1 spec §3.1-3.2): .env parser, TOML schema, validation rules."""

import os

import pytest

from themis.config import ROLES, ConfigError, Env, load_config, parse_env

BASE = """
[defaults]
temperature = 0
timeout_s = 120
max_backoff_s = 60
max_retries_per_entry = 3
busy_timeout_ms = 5000
max_concurrency = 4

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
family = "fam_a"
context = 1000000

[[roles.reasoner]]
provider = "pa"
model = "rea-1"
family = "fam_a"
[[roles.reasoner]]
provider = "pb"
model = "rea-2"
family = "fam_b"

[[roles.critic]]
provider = "pb"
model = "cri-1"
family = "fam_c"
[[roles.critic]]
provider = "pc"
model = "cri-2"
family = "fam_d"

[[roles.embedder]]
provider = "pc"
model = "emb-1"
family = "fam_e"
dims = 1024

[sources.arxiv]
min_interval_s = 3
[sources.openalex]
mailto_env = "MAILTO"
[sources.semantic_scholar]
enabled = false
key_env = "S2_KEY"
"""

ALL_KEYS = {"KEY_A": "ka", "KEY_B": "kb", "KEY_C": "kc"}


def write(tmp_path, text=BASE):
    p = tmp_path / "providers.toml"
    p.write_text(text, encoding="utf-8")
    return p


def load(tmp_path, text=BASE, env=None):
    return load_config(write(tmp_path, text), Env(env if env is not None else ALL_KEYS))


# ---------------------------------------------------------------- .env


def test_parse_env_basics():
    text = (
        "# comment\n"
        "A=1\n"
        "export B = two \n"
        'C="quoted # not a comment"\n'
        "D='single'\n"
        "E=\n"
        "F=value # trailing comment\n"
        "\n"
        "not a line\n"
    )
    assert parse_env(text) == {
        "A": "1",
        "B": "two",
        "C": "quoted # not a comment",
        "D": "single",
        "E": "",
        "F": "value",
    }


def test_env_blank_is_none_and_process_env_overrides(monkeypatch):
    monkeypatch.setenv("THEMIS_TEST_OVERRIDE", "from-process")
    env = Env({"X": "", "THEMIS_TEST_OVERRIDE": "from-file"})
    assert env.get("X") is None
    assert env.get("MISSING") is None
    assert env.get("THEMIS_TEST_OVERRIDE") == "from-process"


def test_env_from_file(tmp_path):
    (tmp_path / ".env").write_text("K=v\n", encoding="utf-8")
    assert Env.from_file(tmp_path / ".env").get("K") == "v"
    assert Env.from_file(tmp_path / "absent.env").get("K") is None


def test_env_repr_hides_values():
    env = Env({"SECRET": "hunter2"})
    assert "hunter2" not in repr(env) and "hunter2" not in str(env)


# ---------------------------------------------------------------- happy path


def test_loads_valid_config(tmp_path):
    cfg = load(tmp_path)
    assert set(cfg.roles) == set(ROLES)
    assert [e.model for e in cfg.enabled_entries("reasoner")] == ["rea-1", "rea-2"]
    assert cfg.defaults.max_backoff_s == 60
    assert cfg.warnings == []


def test_order_is_preserved(tmp_path):
    cfg = load(tmp_path)
    assert [e.provider for e in cfg.roles["critic"]] == ["pb", "pc"]


# ---------------------------------------------------------------- critic family rule


def test_critic_family_overlap_first_entry(tmp_path):
    text = BASE.replace('model = "cri-1"\nfamily = "fam_c"', 'model = "cri-1"\nfamily = "fam_a"')
    with pytest.raises(ConfigError, match=r"critic.*fam_a"):
        load(tmp_path, text)


def test_critic_family_overlap_in_a_fallback(tmp_path):
    # The overlap is between the critic's 2nd entry and the reasoner's 2nd entry.
    text = BASE.replace('model = "cri-2"\nfamily = "fam_d"', 'model = "cri-2"\nfamily = "fam_b"')
    with pytest.raises(ConfigError, match="fam_b"):
        load(tmp_path, text)


def test_critic_family_check_includes_disabled_entries(tmp_path):
    # pb's key is blank, so reasoner entry 2 is disabled; the rule still applies to it.
    text = BASE.replace('model = "cri-2"\nfamily = "fam_d"', 'model = "cri-2"\nfamily = "fam_b"')
    with pytest.raises(ConfigError, match="fam_b"):
        load(tmp_path, text, env={"KEY_A": "ka", "KEY_C": "kc"})


def test_critic_family_case_insensitive(tmp_path):
    text = BASE.replace('model = "cri-1"\nfamily = "fam_c"', 'model = "cri-1"\nfamily = "FAM_A"')
    with pytest.raises(ConfigError):
        load(tmp_path, text)


def test_shared_provider_across_critic_and_reasoner_is_allowed(tmp_path):
    cfg = load(tmp_path)  # pb serves both reasoner and critic in BASE
    assert {e.provider for e in cfg.roles["critic"]} & {e.provider for e in cfg.roles["reasoner"]}


# ---------------------------------------------------------------- enabling / disabling


def test_blank_key_disables_entry_with_warning(tmp_path):
    cfg = load(tmp_path, env={"KEY_A": "ka", "KEY_B": "", "KEY_C": "kc"})
    assert [e.model for e in cfg.enabled_entries("reasoner")] == ["rea-1"]
    assert any("KEY_B" in w for w in cfg.warnings)


def test_placeholder_model_disables_entry(tmp_path):
    text = BASE.replace('model = "rea-2"', 'model = "<model id>"')
    cfg = load(tmp_path, text)
    assert [e.model for e in cfg.enabled_entries("reasoner")] == ["rea-1"]
    assert any("placeholder" in w for w in cfg.warnings)


def test_role_with_no_enabled_entry_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="critic"):
        load(tmp_path, env={"KEY_A": "ka"})


def test_check_enabled_can_be_skipped_for_offline_tools(tmp_path):
    cfg = load_config(write(tmp_path), Env({}), require_enabled=False)
    assert cfg.enabled_entries("critic") == []


# ---------------------------------------------------------------- structure


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        (
            'provider = "pc"\nmodel = "emb-1"',
            'provider = "nope"\nmodel = "emb-1"',
            "unknown provider",
        ),
        ("dims = 1024\n", "", "dims"),
        ('base_url = "https://c.example/v1/"', 'base_url = "http://c.example/v1/"', "https"),
        ('base_url = "https://c.example/v1/"', 'base_url = "https://evil.example/v1/"', "allowed"),
        ("[[roles.embedder]]", "[[roles.oracle]]", "oracle|embedder"),
        ("enabled = false", "enabled = true", "semantic_scholar"),
        ("temperature = 0", "temperature = 0\nsurprise = 1", "surprise"),
    ],
)
def test_structural_errors(tmp_path, old, new, match):
    assert old in BASE
    with pytest.raises(ConfigError, match=match):
        load(tmp_path, BASE.replace(old, new, 1))


def test_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="TOML"):
        load(tmp_path, "this is = = not toml")


def test_no_model_ids_or_keys_in_source():
    """Model IDs live only in config; keys only in .env (CLAUDE.md)."""
    from pathlib import Path

    src = Path(__file__).parents[1] / "src" / "themis"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENROUTER_API_KEY",
            "JINA_API_KEY",
            "HF_TOKEN",
            "gpt-",
            "llama-3",
            "gemini-1",
            "gemini-2",
            "claude-",
        ):
            assert needle not in text, f"{needle!r} hardcoded in {path.name}"


def test_committed_config_parses():
    """The real config/providers.toml loads (placeholders just disable entries)."""
    from pathlib import Path

    path = Path(__file__).parents[1] / "config" / "providers.toml"
    cfg = load_config(path, Env({}), require_enabled=False)
    assert set(cfg.roles) == set(ROLES)
    assert os.sep not in cfg.network.allowed_hosts[0]
