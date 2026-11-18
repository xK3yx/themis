"""Configuration: `.env` secrets and `config/providers.toml` (M1 spec §3.1-3.2).

Model IDs, providers and limits live only in config; keys only in `.env` (or the real
environment). Code refers to keys by the `key_env` names that config gives it.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import pydantic
from pydantic import BaseModel, ConfigDict, Field

ROLES = ("extractor", "reasoner", "critic", "embedder")


class ConfigError(Exception):
    """Invalid configuration."""


# ---------------------------------------------------------------- .env

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` text: KEY=value, optional `export`, quotes, `#` comments, blank values."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


class Env:
    """Read-only access to secrets. Process environment overrides `.env`. Never prints values."""

    def __init__(self, values: Mapping[str, str] | None = None):
        self._file = dict(values or {})

    @classmethod
    def from_file(cls, path: Path) -> "Env":
        path = Path(path)
        if not path.is_file():
            return cls({})
        return cls(parse_env(path.read_text(encoding="utf-8")))

    def get(self, name: str) -> str | None:
        """The value, or None when unset or blank."""
        value = os.environ.get(name, self._file.get(name))
        return value if value else None

    def values(self) -> list[str]:
        """All non-blank secret values (for scrubbing recorded fixtures)."""
        names = set(self._file)
        return [v for n in names if (v := self.get(n))]

    def __repr__(self) -> str:
        return f"Env(<{len(self._file)} names>)"

    __str__ = __repr__


# ---------------------------------------------------------------- TOML schema


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Defaults(_Strict):
    temperature: float = 0
    timeout_s: Annotated[float, Field(gt=0)]
    max_backoff_s: Annotated[float, Field(ge=0)]
    max_retries_per_entry: Annotated[int, Field(ge=0)]
    busy_timeout_ms: Annotated[int, Field(ge=0)]
    max_concurrency: Annotated[int, Field(ge=1)]


class Network(_Strict):
    allowed_hosts: list[str]


class Provider(_Strict):
    base_url: str
    key_env: str


class RoleEntry(_Strict):
    provider: str
    model: str
    family: str
    context: int | None = None
    dims: int | None = None
    rpm_hint: int | None = None
    rpd_hint: int | None = None


class ArxivSource(_Strict):
    min_interval_s: Annotated[float, Field(ge=0)]


class OpenAlexSource(_Strict):
    mailto_env: str


class SemanticScholarSource(_Strict):
    enabled: bool = False
    key_env: str


class Sources(_Strict):
    arxiv: ArxivSource
    openalex: OpenAlexSource
    semantic_scholar: SemanticScholarSource


class IngestLimits(_Strict):
    """Caps for unpacking untrusted arXiv e-prints in memory (spec §4.3)."""

    max_download_bytes: Annotated[int, Field(gt=0)] = 100_000_000
    max_members: Annotated[int, Field(gt=0)] = 2_000
    max_total_bytes: Annotated[int, Field(gt=0)] = 200_000_000
    max_file_bytes: Annotated[int, Field(gt=0)] = 20_000_000
    keep_suffixes: list[str] = [".tex", ".bbl", ".sty", ".cls", ".bib"]


class _File(_Strict):
    defaults: Defaults
    network: Network
    providers: dict[str, Provider]
    roles: dict[str, list[RoleEntry]]
    sources: Sources
    ingest: IngestLimits = IngestLimits()


class Config:
    """Validated config plus which role entries are usable with the current secrets."""

    def __init__(self, raw: _File, enabled: dict[str, list[RoleEntry]], warnings: list[str]):
        self.defaults = raw.defaults
        self.network = raw.network
        self.providers = raw.providers
        self.roles = raw.roles
        self.sources = raw.sources
        self.ingest = raw.ingest
        self._enabled = enabled
        self.warnings = warnings

    def enabled_entries(self, role: str) -> list[RoleEntry]:
        if role not in ROLES:
            raise ConfigError(f"unknown role {role!r}")
        return list(self._enabled[role])


def _is_placeholder(model: str) -> bool:
    return "<" in model or ">" in model


def load_config(path: Path, env: Env, *, require_enabled: bool = True) -> Config:
    """Load and validate `providers.toml`.

    `require_enabled=False` skips the "every role has a usable entry" check, for offline
    tools (status, eval scoring from cache) that never call a provider.
    """
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None
    try:
        raw = _File.model_validate(data)
    except pydantic.ValidationError as e:
        raise ConfigError(f"{path}: {e}") from None

    problems: list[str] = []
    unknown = set(raw.roles) - set(ROLES)
    missing = set(ROLES) - set(raw.roles)
    if unknown:
        problems.append(f"unknown roles: {sorted(unknown)}")
    if missing:
        problems.append(f"missing roles: {sorted(missing)}")

    allowed = set(raw.network.allowed_hosts)
    for name, prov in raw.providers.items():
        url = urlsplit(prov.base_url)
        if url.scheme != "https":
            problems.append(f"provider {name}: base_url must use https")
        if url.hostname not in allowed:
            problems.append(f"provider {name}: host {url.hostname!r} is not in allowed_hosts")

    for role, entries in raw.roles.items():
        if not entries:
            problems.append(f"role {role}: no entries")
        for i, e in enumerate(entries):
            if e.provider not in raw.providers:
                problems.append(f"role {role}[{i}]: unknown provider {e.provider!r}")
            if role == "embedder" and e.dims is None:
                problems.append(f"role embedder[{i}]: embedder entries must declare dims")

    # The critic must never share a model family with the reasoner, across every entry,
    # including disabled ones (enabling a key later must not create a violation).
    reasoner = {e.family.casefold() for e in raw.roles.get("reasoner", [])}
    for i, e in enumerate(raw.roles.get("critic", [])):
        if e.family.casefold() in reasoner:
            problems.append(
                f"role critic[{i}] ({e.provider}/{e.model}) has family {e.family!r}, "
                "which also appears in role reasoner; the critic must be a different family"
            )

    if raw.sources.semantic_scholar.enabled:
        problems.append("sources.semantic_scholar is out of scope for M1; set enabled = false")
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))

    warnings: list[str] = []
    enabled: dict[str, list[RoleEntry]] = {}
    for role in ROLES:
        enabled[role] = []
        for i, e in enumerate(raw.roles[role]):
            key_env = raw.providers[e.provider].key_env
            if _is_placeholder(e.model):
                warnings.append(f"role {role}[{i}] disabled: model id is a placeholder")
            elif env.get(key_env) is None:
                warnings.append(f"role {role}[{i}] disabled: {key_env} is unset or blank")
            else:
                enabled[role].append(e)
        if require_enabled and not enabled[role]:
            raise ConfigError(f"{path}: role {role} has no enabled entry ({'; '.join(warnings)})")
    return Config(raw, enabled, warnings)
