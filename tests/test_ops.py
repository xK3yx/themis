"""Operational DB (M1 spec §3.6): quota events and run stats, separate from the ledger."""

from datetime import UTC, datetime, timedelta

from themis.ops import OpsDB

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def test_exhausted_until(tmp_path):
    ops = OpsDB(tmp_path / "ops.db")
    assert ops.exhausted_until("p", "m", T0) is None
    ops.record("p", "m", "exhausted", reset_at=T0 + timedelta(hours=1), at=T0)
    assert ops.exhausted_until("p", "m", T0) == T0 + timedelta(hours=1)
    assert ops.exhausted_until("p", "m", T0 + timedelta(hours=2)) is None
    assert ops.exhausted_until("p", "other", T0) is None
    ops.close()


def test_later_success_clears_exhaustion(tmp_path):
    ops = OpsDB(tmp_path / "ops.db")
    ops.record("p", "m", "exhausted", reset_at=T0 + timedelta(hours=1), at=T0)
    ops.record("p", "m", "headers", at=T0 + timedelta(minutes=1))
    assert ops.exhausted_until("p", "m", T0 + timedelta(minutes=2)) is None
    ops.close()


def test_persists_across_connections(tmp_path):
    ops = OpsDB(tmp_path / "ops.db")
    ops.record("p", "m", "exhausted", reset_at=T0 + timedelta(hours=1), at=T0)
    ops.close()
    again = OpsDB(tmp_path / "ops.db")
    assert again.exhausted_until("p", "m", T0) == T0 + timedelta(hours=1)
    again.close()


def test_stats(tmp_path):
    ops = OpsDB(tmp_path / "ops.db")
    ops.count("extractor", hit=True)
    ops.count("extractor", hit=False)
    ops.count("extractor", hit=True)
    assert ops.hit_rate("extractor") == (2, 1)
    ops.close()


def test_ops_db_is_not_the_ledger(tmp_path):
    from themis.ledger.dao import Ledger

    lg = Ledger.open(tmp_path / "data")
    ops = OpsDB(tmp_path / "data" / "ops.db")
    names = {r[0] for r in lg.conn.execute("SELECT name FROM sqlite_master")}
    assert "quota_events" not in names
    ops.close()
    lg.close()
