"""
Cost Budget System (Feature 2) — per-user daily/monthly cost caps with alerts.

Inspired by the Financial Data Analyst and Managed Agents projects that track
and cap costs per user/session. Integrates with the existing track_cost() flow.

Usage:
    from telechat_pkg.cost_budget import BudgetManager
    mgr = BudgetManager()
    warning = mgr.check("telegram", "123")  # returns warning str or None
    mgr.set_budget("telegram", "123", daily=1.0, monthly=20.0)
    report = mgr.usage_report("telegram", "123")
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_DAILY_BUDGET = float(os.getenv("COST_DAILY_BUDGET", "5.0"))
DEFAULT_MONTHLY_BUDGET = float(os.getenv("COST_MONTHLY_BUDGET", "50.0"))
WARN_THRESHOLD = float(os.getenv("COST_WARN_THRESHOLD", "0.8"))  # warn at 80%


class BudgetExceeded(Exception):
    """Raised when user has exceeded their cost budget."""
    pass


@dataclass
class Budget:
    platform: str
    user_id: str
    daily_limit: float
    monthly_limit: float
    alert_sent_daily: bool = False
    alert_sent_monthly: bool = False


@dataclass
class UsageReport:
    daily_cost: float
    daily_limit: float
    daily_pct: float
    monthly_cost: float
    monthly_limit: float
    monthly_pct: float
    daily_requests: int
    monthly_requests: int


class BudgetManager:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # Share the canonical DB location with store.py so budget rows
            # live in the same SQLite file as conversations — and so we
            # never write into the installed package directory.
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cost_budgets (
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                daily_limit REAL NOT NULL DEFAULT 5.0,
                monthly_limit REAL NOT NULL DEFAULT 50.0,
                alert_sent_daily INTEGER DEFAULT 0,
                alert_sent_monthly INTEGER DEFAULT 0,
                updated_at  REAL,
                PRIMARY KEY (platform, user_id)
            );
        """)
        conn.commit()

    def _get_budget(self, platform: str, user_id: str) -> Budget:
        row = self._conn().execute(
            "SELECT * FROM cost_budgets WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        ).fetchone()
        if row:
            return Budget(
                platform=row["platform"],
                user_id=row["user_id"],
                daily_limit=row["daily_limit"],
                monthly_limit=row["monthly_limit"],
                alert_sent_daily=bool(row["alert_sent_daily"]),
                alert_sent_monthly=bool(row["alert_sent_monthly"]),
            )
        return Budget(
            platform=platform,
            user_id=user_id,
            daily_limit=DEFAULT_DAILY_BUDGET,
            monthly_limit=DEFAULT_MONTHLY_BUDGET,
        )

    def _get_daily_cost(self, platform: str, user_id: str) -> tuple[float, int]:
        """Returns (total_cost, request_count) for today.

        ``date('now')`` is UTC, but ``store.track_cost`` stamps rows with
        ``date.today()`` — the local date. West of UTC those disagree for the
        last hours of every day, which made the budget stop seeing the day's
        spend right when it mattered. Both ends now agree on local time.
        """
        conn = self._conn()
        row = conn.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) as total,
                      COUNT(*) as cnt
               FROM cost_tracking
               WHERE platform = ? AND user_id = ?
                 AND date = date('now', 'localtime')""",
            (platform, user_id),
        ).fetchone()
        if row:
            return row["total"], row["cnt"]
        return 0.0, 0

    def _get_monthly_cost(self, platform: str, user_id: str) -> tuple[float, int]:
        """Returns (total_cost, request_count) for this month.

        Local time, for the same reason as the daily window — and here the
        disagreement is worse: on the evening of the last day of a month, a
        UTC ``start of month`` had already rolled over, so the entire month's
        spend fell outside the window and the monthly cap enforced nothing.
        """
        conn = self._conn()
        row = conn.execute(
            """SELECT COALESCE(SUM(cost_usd), 0) as total,
                      COUNT(*) as cnt
               FROM cost_tracking
               WHERE platform = ? AND user_id = ?
                 AND date >= date('now', 'localtime', 'start of month')""",
            (platform, user_id),
        ).fetchone()
        if row:
            return row["total"], row["cnt"]
        return 0.0, 0

    def check(self, platform: str, user_id: str) -> str | None:
        """Check budget. Returns warning/block message or None if OK."""
        try:
            budget = self._get_budget(platform, user_id)
            daily_cost, _ = self._get_daily_cost(platform, user_id)
            monthly_cost, _ = self._get_monthly_cost(platform, user_id)

            # Hard block if exceeded
            if daily_cost >= budget.daily_limit:
                return (
                    f"Daily budget exceeded (${daily_cost:.2f} / ${budget.daily_limit:.2f}). "
                    f"Use /budget to adjust or wait until tomorrow."
                )
            if monthly_cost >= budget.monthly_limit:
                return (
                    f"Monthly budget exceeded (${monthly_cost:.2f} / ${budget.monthly_limit:.2f}). "
                    f"Use /budget to adjust."
                )

            # Warning if approaching
            warnings = []
            daily_pct = daily_cost / budget.daily_limit if budget.daily_limit > 0 else 0
            monthly_pct = monthly_cost / budget.monthly_limit if budget.monthly_limit > 0 else 0

            if daily_pct >= WARN_THRESHOLD and not budget.alert_sent_daily:
                warnings.append(f"Daily cost at {daily_pct:.0%} (${daily_cost:.2f}/${budget.daily_limit:.2f})")
                self._mark_alert(platform, user_id, "daily")

            if monthly_pct >= WARN_THRESHOLD and not budget.alert_sent_monthly:
                warnings.append(f"Monthly cost at {monthly_pct:.0%} (${monthly_cost:.2f}/${budget.monthly_limit:.2f})")
                self._mark_alert(platform, user_id, "monthly")

            if warnings:
                return "Budget warning: " + "; ".join(warnings)

            return None
        except Exception as e:
            log.debug("Budget check failed: %s", e)
            return None

    def _mark_alert(self, platform: str, user_id: str, period: str):
        _VALID_PERIODS = {"daily", "monthly"}
        if period not in _VALID_PERIODS:
            raise ValueError(f"Invalid period: {period!r}")
        col = f"alert_sent_{period}"
        self._conn().execute(
            f"""INSERT INTO cost_budgets (platform, user_id, daily_limit, monthly_limit, {col}, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET {col} = 1, updated_at = ?""",
            (platform, user_id, DEFAULT_DAILY_BUDGET, DEFAULT_MONTHLY_BUDGET, time.time(), time.time()),
        )
        self._conn().commit()

    def set_budget(self, platform: str, user_id: str, *, daily: float | None = None, monthly: float | None = None):
        budget = self._get_budget(platform, user_id)
        d = daily if daily is not None else budget.daily_limit
        m = monthly if monthly is not None else budget.monthly_limit
        self._conn().execute(
            """INSERT INTO cost_budgets (platform, user_id, daily_limit, monthly_limit, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(platform, user_id) DO UPDATE
               SET daily_limit = ?, monthly_limit = ?, alert_sent_daily = 0, alert_sent_monthly = 0, updated_at = ?""",
            (platform, user_id, d, m, time.time(), d, m, time.time()),
        )
        self._conn().commit()

    def usage_report(self, platform: str, user_id: str) -> UsageReport:
        budget = self._get_budget(platform, user_id)
        daily_cost, daily_req = self._get_daily_cost(platform, user_id)
        monthly_cost, monthly_req = self._get_monthly_cost(platform, user_id)
        return UsageReport(
            daily_cost=daily_cost,
            daily_limit=budget.daily_limit,
            daily_pct=daily_cost / budget.daily_limit if budget.daily_limit > 0 else 0,
            monthly_cost=monthly_cost,
            monthly_limit=budget.monthly_limit,
            monthly_pct=monthly_cost / budget.monthly_limit if budget.monthly_limit > 0 else 0,
            daily_requests=daily_req,
            monthly_requests=monthly_req,
        )

    def reset_daily_alerts(self):
        """Call at midnight to reset daily alert flags."""
        self._conn().execute("UPDATE cost_budgets SET alert_sent_daily = 0")
        self._conn().commit()
