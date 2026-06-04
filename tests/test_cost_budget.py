"""Behavior tests for cost_budget.BudgetManager.

Focus (ticket 0016): boundary cases, no-rows handling, warning vs. block
thresholds, alert-flag persistence, and the usage report. Each test uses an
isolated temp SQLite DB with a hand-rolled ``cost_tracking`` table so the
budget manager's joins against cost rows are exercised without depending on
store.py's writer thread.
"""

import os
import sqlite3
import time

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg.cost_budget import (
    BudgetManager,
    DEFAULT_DAILY_BUDGET,
    DEFAULT_MONTHLY_BUDGET,
    WARN_THRESHOLD,
)


def _make_cost_table(db_path: str) -> None:
    """Create the cost_tracking table the BudgetManager reads from."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cost_tracking (
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            requests INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            PRIMARY KEY (platform, user_id, date))"""
    )
    conn.commit()
    conn.close()


def _add_cost(db_path: str, platform: str, user_id: str, cost: float, *, date: str = "now"):
    conn = sqlite3.connect(db_path)
    if date == "now":
        conn.execute(
            "INSERT INTO cost_tracking (platform, user_id, date, requests, cost_usd) "
            "VALUES (?, ?, date('now'), 1, ?)",
            (platform, user_id, cost),
        )
    else:
        conn.execute(
            "INSERT INTO cost_tracking (platform, user_id, date, requests, cost_usd) "
            "VALUES (?, ?, ?, 1, ?)",
            (platform, user_id, date, cost),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "budget.db")
    _make_cost_table(p)
    return p


@pytest.fixture
def mgr(db_path):
    return BudgetManager(db_path)


class TestSchemaAndDefaults:
    def test_creates_budgets_table(self, mgr, db_path):
        conn = sqlite3.connect(db_path)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "cost_budgets" in names

    def test_default_budget_when_no_row(self, mgr):
        b = mgr._get_budget("tg", "newbie")
        assert b.daily_limit == DEFAULT_DAILY_BUDGET
        assert b.monthly_limit == DEFAULT_MONTHLY_BUDGET
        assert b.alert_sent_daily is False
        assert b.alert_sent_monthly is False

    def test_default_db_path_uses_store(self, monkeypatch, tmp_path):
        # db_path=None branch -> falls back to store.DB_PATH
        from telechat_pkg import store
        p = str(tmp_path / "store_default.db")
        _make_cost_table(p)
        monkeypatch.setattr(store, "DB_PATH", p)
        m = BudgetManager()
        assert m._db_path == p


class TestNoRowsAndBoundaries:
    def test_no_cost_rows_returns_zero(self, mgr):
        cost, cnt = mgr._get_daily_cost("tg", "u1")
        assert cost == 0.0
        assert cnt == 0

    def test_no_monthly_rows_returns_zero(self, mgr):
        cost, cnt = mgr._get_monthly_cost("tg", "u1")
        assert cost == 0.0
        assert cnt == 0

    def test_check_no_usage_returns_none(self, mgr):
        assert mgr.check("tg", "u1") is None

    def test_daily_cost_none_row_fallback(self, mgr, monkeypatch):
        # The aggregate query always returns a row in SQLite, so the
        # ``return 0.0, 0`` fallback (line 127) is otherwise dead. Force a
        # None fetchone to exercise it.
        import unittest.mock as m
        fake = m.MagicMock()
        fake.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(mgr, "_conn", lambda: fake)
        assert mgr._get_daily_cost("tg", "u1") == (0.0, 0)

    def test_monthly_cost_none_row_fallback(self, mgr, monkeypatch):
        import unittest.mock as m
        fake = m.MagicMock()
        fake.execute.return_value.fetchone.return_value = None
        monkeypatch.setattr(mgr, "_conn", lambda: fake)
        assert mgr._get_monthly_cost("tg", "u1") == (0.0, 0)

    def test_check_below_threshold_returns_none(self, mgr, db_path):
        # Well below the 80% warn threshold for the default $5 daily budget.
        _add_cost(db_path, "tg", "u1", 0.10)
        assert mgr.check("tg", "u1") is None


class TestWarnings:
    def test_daily_warning_at_threshold(self, mgr, db_path):
        # Push to >= 80% of $5 = $4.00 but below $5 (block).
        _add_cost(db_path, "tg", "u1", DEFAULT_DAILY_BUDGET * WARN_THRESHOLD)
        msg = mgr.check("tg", "u1")
        assert msg is not None
        assert "warning" in msg.lower()
        assert "Daily" in msg

    def test_warning_only_sent_once(self, mgr, db_path):
        _add_cost(db_path, "tg", "u1", DEFAULT_DAILY_BUDGET * WARN_THRESHOLD)
        first = mgr.check("tg", "u1")
        assert first is not None
        # Alert flag now set -> second check at same level produces no warning.
        second = mgr.check("tg", "u1")
        assert second is None

    def test_monthly_warning(self, mgr, db_path):
        # Below daily limit but above monthly warn threshold.
        # Daily budget default $5, monthly $50. Put a charge on a prior day this
        # month so daily cost is 0 but monthly cost crosses 80% of $50 = $40.
        prior = time.strftime("%Y-%m-01")
        _add_cost(db_path, "tg", "u1", DEFAULT_MONTHLY_BUDGET * WARN_THRESHOLD, date=prior)
        msg = mgr.check("tg", "u1")
        assert msg is not None
        assert "Monthly" in msg


class TestBlocking:
    def test_daily_block(self, mgr, db_path):
        _add_cost(db_path, "tg", "u1", DEFAULT_DAILY_BUDGET + 1.0)
        msg = mgr.check("tg", "u1")
        assert msg is not None
        assert "Daily budget exceeded" in msg

    def test_monthly_block(self, mgr, db_path):
        # Monthly exceeded but today's spend below daily limit.
        prior = time.strftime("%Y-%m-01")
        _add_cost(db_path, "tg", "u1", DEFAULT_MONTHLY_BUDGET + 5.0, date=prior)
        msg = mgr.check("tg", "u1")
        assert msg is not None
        assert "Monthly budget exceeded" in msg

    def test_check_swallows_errors_returns_none(self, mgr, monkeypatch):
        monkeypatch.setattr(mgr, "_get_budget", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert mgr.check("tg", "u1") is None


class TestSetBudget:
    def test_set_then_get(self, mgr):
        mgr.set_budget("tg", "u1", daily=1.0, monthly=10.0)
        b = mgr._get_budget("tg", "u1")
        assert b.daily_limit == 1.0
        assert b.monthly_limit == 10.0

    def test_set_partial_keeps_other(self, mgr):
        mgr.set_budget("tg", "u1", daily=2.0, monthly=20.0)
        mgr.set_budget("tg", "u1", daily=3.0)  # monthly unspecified
        b = mgr._get_budget("tg", "u1")
        assert b.daily_limit == 3.0
        assert b.monthly_limit == 20.0

    def test_set_budget_resets_alert_flags(self, mgr, db_path):
        _add_cost(db_path, "tg", "u1", DEFAULT_DAILY_BUDGET * WARN_THRESHOLD)
        mgr.check("tg", "u1")  # sets alert_sent_daily
        mgr.set_budget("tg", "u1", daily=DEFAULT_DAILY_BUDGET)
        b = mgr._get_budget("tg", "u1")
        assert b.alert_sent_daily is False

    def test_lower_daily_limit_triggers_block(self, mgr, db_path):
        _add_cost(db_path, "tg", "u1", 0.50)
        assert mgr.check("tg", "u1") is None
        mgr.set_budget("tg", "u1", daily=0.25)
        msg = mgr.check("tg", "u1")
        assert msg is not None and "Daily budget exceeded" in msg


class TestMarkAlert:
    def test_invalid_period_raises(self, mgr):
        with pytest.raises(ValueError):
            mgr._mark_alert("tg", "u1", "weekly")

    def test_mark_daily_persists(self, mgr):
        mgr._mark_alert("tg", "u1", "daily")
        assert mgr._get_budget("tg", "u1").alert_sent_daily is True

    def test_mark_monthly_persists(self, mgr):
        mgr._mark_alert("tg", "u1", "monthly")
        assert mgr._get_budget("tg", "u1").alert_sent_monthly is True


class TestUsageReport:
    def test_report_zero_usage(self, mgr):
        r = mgr.usage_report("tg", "u1")
        assert r.daily_cost == 0.0
        assert r.daily_pct == 0.0
        assert r.monthly_requests == 0

    def test_report_with_usage(self, mgr, db_path):
        _add_cost(db_path, "tg", "u1", 1.0)
        r = mgr.usage_report("tg", "u1")
        assert r.daily_cost == 1.0
        assert r.daily_requests == 1
        assert r.daily_pct == pytest.approx(1.0 / DEFAULT_DAILY_BUDGET)
        assert r.monthly_cost == 1.0

    def test_report_zero_limit_no_divzero(self, mgr):
        mgr.set_budget("tg", "u1", daily=0.0, monthly=0.0)
        r = mgr.usage_report("tg", "u1")
        assert r.daily_pct == 0
        assert r.monthly_pct == 0


class TestResetDailyAlerts:
    def test_reset_clears_flags(self, mgr):
        mgr._mark_alert("tg", "u1", "daily")
        mgr._mark_alert("tg", "u2", "daily")
        mgr.reset_daily_alerts()
        assert mgr._get_budget("tg", "u1").alert_sent_daily is False
        assert mgr._get_budget("tg", "u2").alert_sent_daily is False
