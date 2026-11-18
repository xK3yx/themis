"""Operational DB `data/ops.db` (M1 spec §3.6): learned quota state and run stats.

Operational state, not evidence: not append-only enforced, not in the ledger, may be
deleted. Nothing in the ledger depends on it; losing it costs a few wasted requests.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

KINDS = ("headers", "rate_limited", "exhausted", "error", "disabled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_events (
  id          INTEGER PRIMARY KEY,
  provider    TEXT NOT NULL,
  model       TEXT NOT NULL,
  kind        TEXT NOT NULL
              CHECK (kind IN ('headers', 'rate_limited', 'exhausted', 'error', 'disabled')),
  reset_at    TEXT,
  detail_json TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_events_entry ON quota_events(provider, model, id);
CREATE TABLE IF NOT EXISTS run_stats (
  run_id       TEXT NOT NULL,
  role         TEXT NOT NULL,
  cache_hits   INTEGER NOT NULL DEFAULT 0,
  cache_misses INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, role)
);
"""


class OpsDB:
    def __init__(self, path: Path, run_id: str = "default"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self.run_id = run_id

    def close(self) -> None:
        self.conn.close()

    def record(
        self,
        provider: str,
        model: str,
        kind: str,
        *,
        reset_at: datetime | None = None,
        detail: dict | None = None,
        at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO quota_events(provider, model, kind, reset_at, detail_json, observed_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                provider,
                model,
                kind,
                reset_at.isoformat() if reset_at else None,
                json.dumps(detail or {}, sort_keys=True),
                at.isoformat(),
            ),
        )

    def exhausted_until(self, provider: str, model: str, now: datetime) -> datetime | None:
        """Reset time if the latest decisive event says this entry is exhausted past `now`."""
        row = self.conn.execute(
            "SELECT kind, reset_at FROM quota_events WHERE provider = ? AND model = ?"
            " AND kind IN ('exhausted', 'headers') ORDER BY id DESC LIMIT 1",
            (provider, model),
        ).fetchone()
        if row is None or row[0] != "exhausted" or row[1] is None:
            return None
        reset = datetime.fromisoformat(row[1])
        return reset if reset > now else None

    def count(self, role: str, *, hit: bool) -> None:
        col = "cache_hits" if hit else "cache_misses"
        self.conn.execute(
            f"INSERT INTO run_stats(run_id, role, {col}) VALUES (?, ?, 1)"
            f" ON CONFLICT(run_id, role) DO UPDATE SET {col} = {col} + 1",
            (self.run_id, role),
        )

    def hit_rate(self, role: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT coalesce(sum(cache_hits), 0), coalesce(sum(cache_misses), 0)"
            " FROM run_stats WHERE role = ?",
            (role,),
        ).fetchone()
        return int(row[0]), int(row[1])
