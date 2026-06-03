"""
Diagnostic doctor — check configuration, connectivity, and health.

to diagnose common issues and suggest fixes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str
    fix_hint: str = ""
    severity: str = "info"  # info, warning, error


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)
    passed: int = 0
    warnings: int = 0
    errors: int = 0

    def add(self, result: CheckResult):
        self.checks.append(result)
        if result.passed:
            self.passed += 1
        elif result.severity == "warning":
            self.warnings += 1
        else:
            self.errors += 1

    @property
    def healthy(self) -> bool:
        return self.errors == 0

    def format(self) -> str:
        lines = ["🩺 Telechat Doctor Report\n"]
        for c in self.checks:
            icon = "✅" if c.passed else ("⚠️" if c.severity == "warning" else "❌")
            lines.append(f"{icon} {c.name}: {c.message}")
            if not c.passed and c.fix_hint:
                lines.append(f"   💡 {c.fix_hint}")
        lines.append(f"\n{'─' * 40}")
        lines.append(f"Passed: {self.passed}  Warnings: {self.warnings}  Errors: {self.errors}")
        if self.healthy:
            lines.append("✅ All good!")
        else:
            lines.append("❌ Issues found — see hints above.")
        return "\n".join(lines)


def check_python_version() -> CheckResult:
    v = sys.version_info
    if v >= (3, 10):
        return CheckResult("Python version", True, f"{v.major}.{v.minor}.{v.micro}")
    return CheckResult(
        "Python version", False, f"{v.major}.{v.minor}.{v.micro} (need 3.10+)",
        fix_hint="Upgrade Python to 3.10 or later.",
        severity="error",
    )


def check_claude_cli() -> CheckResult:
    claude_path = shutil.which("claude")
    if claude_path:
        return CheckResult("Claude CLI", True, f"Found at {claude_path}")
    return CheckResult(
        "Claude CLI", False, "Not found in PATH",
        fix_hint="Install: npm install -g @anthropic-ai/claude-code && claude auth login",
        severity="error",
    )


def check_env_file() -> CheckResult:
    from . import store
    env_path = Path(store.DB_PATH).parent / ".env"
    # Also check standard locations
    candidates = [
        env_path,
        Path.home() / ".telechat" / ".env",
        Path(".env"),
    ]
    for p in candidates:
        if p.exists():
            return CheckResult("Environment file", True, f"Found: {p}")
    return CheckResult(
        "Environment file", False, "No .env file found",
        fix_hint="Run: telechat init",
        severity="error",
    )


def check_bot_token() -> CheckResult:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if token and token != "CHANGE_ME_ROTATE_TOKEN" and ":" in token:
        return CheckResult("Telegram bot token", True, f"Set ({token[:8]}...)")
    if not token:
        mode = os.getenv("BOT_MODE", "telegram")
        if "telegram" not in mode:
            return CheckResult("Telegram bot token", True, "Not needed (Telegram not enabled)")
        return CheckResult(
            "Telegram bot token", False, "Not set",
            fix_hint="Set TELEGRAM_BOT_TOKEN in .env",
            severity="error",
        )
    return CheckResult(
        "Telegram bot token", False, "Invalid or placeholder token",
        fix_hint="Get a real token from @BotFather on Telegram",
        severity="error",
    )


def check_database() -> CheckResult:
    from . import store
    try:
        conn = store._get_conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        expected = {"conversations", "usage"}
        found = expected & tables
        if found == expected:
            row_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            return CheckResult("Database", True, f"OK ({len(tables)} tables, {row_count} messages)")
        missing = expected - found
        return CheckResult(
            "Database", False, f"Missing tables: {missing}",
            fix_hint="Database will be initialized on first run.",
            severity="warning",
        )
    except Exception as e:
        return CheckResult(
            "Database", False, f"Error: {e}",
            fix_hint="Check DB_PATH and file permissions.",
            severity="error",
        )


def check_disk_space() -> CheckResult:
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024 ** 3)
        if free_gb > 1:
            return CheckResult("Disk space", True, f"{free_gb:.1f} GB free")
        return CheckResult(
            "Disk space", False, f"Only {free_gb:.2f} GB free",
            fix_hint="Free up disk space. Bot needs space for SQLite database and temp files.",
            severity="warning",
        )
    except Exception:
        return CheckResult("Disk space", True, "Could not check (skipped)")


def check_dependencies() -> CheckResult:
    missing = []
    optional_missing = []

    # Required
    for mod in ["aiohttp", "dotenv"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    # Optional
    for mod, desc in [("fitz", "PDF extraction"), ("docx", "DOCX extraction"),
                       ("playwright", "browser automation")]:
        try:
            __import__(mod)
        except ImportError:
            optional_missing.append(f"{mod} ({desc})")

    if missing:
        return CheckResult(
            "Dependencies", False, f"Missing required: {', '.join(missing)}",
            fix_hint=f"pip install {' '.join(missing)}",
            severity="error",
        )
    msg = "All required installed"
    if optional_missing:
        msg += f" (optional missing: {', '.join(optional_missing)})"
    return CheckResult("Dependencies", True, msg)


def check_rate_limits() -> CheckResult:
    limit = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))
    window = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
    if limit > 0 and window > 0:
        return CheckResult("Rate limiting", True, f"{limit} requests per {window}s")
    return CheckResult(
        "Rate limiting", False, "Rate limiting disabled",
        fix_hint="Set RATE_LIMIT_REQUESTS and RATE_LIMIT_WINDOW in .env",
        severity="warning",
    )


def check_allowed_users() -> CheckResult:
    tg = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    wa = os.getenv("WHATSAPP_ALLOWED_NUMBERS", "")
    sl = os.getenv("SLACK_ALLOWED_USER_IDS", "")
    configured = []
    if tg:
        configured.append(f"Telegram ({len(tg.split(','))} users)")
    if wa:
        configured.append(f"WhatsApp ({len(wa.split(','))} numbers)")
    if sl:
        configured.append(f"Slack ({len(sl.split(','))} users)")

    if configured:
        return CheckResult("Access control", True, ", ".join(configured))
    return CheckResult(
        "Access control", False, "No user allowlists configured — bot is open to anyone",
        fix_hint="Set TELEGRAM_ALLOWED_USER_IDS, WHATSAPP_ALLOWED_NUMBERS, or SLACK_ALLOWED_USER_IDS",
        severity="warning",
    )


async def check_telegram_connectivity() -> CheckResult:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or ":" not in token:
        return CheckResult("Telegram API", True, "Skipped (no token)")
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        bot_name = data["result"].get("username", "???")
                        return CheckResult("Telegram API", True, f"Connected (@{bot_name})")
                return CheckResult(
                    "Telegram API", False, f"HTTP {resp.status}",
                    fix_hint="Check your TELEGRAM_BOT_TOKEN — it may be invalid or revoked.",
                    severity="error",
                )
    except Exception as e:
        return CheckResult(
            "Telegram API", False, f"Connection error: {e}",
            fix_hint="Check internet connectivity.",
            severity="error",
        )


def run_doctor_sync() -> DoctorReport:
    """Run all synchronous diagnostic checks."""
    report = DoctorReport()
    report.add(check_python_version())
    report.add(check_claude_cli())
    report.add(check_env_file())
    report.add(check_bot_token())
    report.add(check_database())
    report.add(check_disk_space())
    report.add(check_dependencies())
    report.add(check_rate_limits())
    report.add(check_allowed_users())
    return report


async def run_doctor() -> DoctorReport:
    """Run all diagnostic checks (sync + async)."""
    report = run_doctor_sync()
    report.add(await check_telegram_connectivity())
    return report
