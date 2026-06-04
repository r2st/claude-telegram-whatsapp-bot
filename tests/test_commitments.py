"""
Behavior-organized tests for telechat_pkg.commitments.

Covers the user-facing commitments feature surface:
  - parsing relative time expressions ("tomorrow", "in 3 days", weekdays, ...)
  - extracting commitment candidates from conversation text
  - recording commitments (CRUD)
  - recalling pending vs. due commitments (recall by date / expiry)
  - status transitions: dismiss, snooze, mark_sent
  - the auto-extract-and-store convenience wrapper
  - formatting pending commitments for display

Fixtures are local to this file. Each test gets a fresh temp SQLite DB and a
freshly created commitments table. Writes are kept synchronous (the background
writer thread is never started), so an enqueued INSERT/UPDATE is visible on the
next read in the same thread — see store._enqueue_write's sync fallback.

Run:
    pytest tests/test_commitments.py -v
"""

import os
import threading
import time
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import commitments as C
from telechat_pkg import store as _store


@pytest.fixture
def db(tmp_path):
    """Point store at a per-test DB, reset caches, create the commitments table.

    Leaves store._write_queue as None so _enqueue_write writes synchronously,
    making CRUD operations immediately visible to subsequent reads.
    """
    orig_db = _store.DB_PATH
    orig_local = _store._local
    orig_queue = _store._write_queue
    orig_thread = _store._writer_thread

    _store.DB_PATH = str(tmp_path / "commitments_test.db")
    _store._local = threading.local()
    _store._write_queue = None       # force synchronous writes
    _store._writer_thread = None

    C.init_db()
    yield

    try:
        if getattr(_store._local, "conn", None) is not None:
            _store._local.conn.close()
    except Exception:
        pass
    _store.DB_PATH = orig_db
    _store._local = orig_local
    _store._write_queue = orig_queue
    _store._writer_thread = orig_thread


# ══════════════════════════════════════════════════════════════════════════════
# 1. Parsing relative time expressions
# ══════════════════════════════════════════════════════════════════════════════


class TestParsingDueTime:
    def test_minutes(self):
        before = datetime.now() + timedelta(minutes=10)
        due = C.parse_due_time("ping me in 10 minutes")
        assert due is not None
        assert abs(due - before.timestamp()) < 5

    def test_minute_singular_abbrev(self):
        # "min" without "ute" and no plural still matches.
        due = C.parse_due_time("in 5 min")
        assert due is not None
        assert abs(due - (datetime.now() + timedelta(minutes=5)).timestamp()) < 5

    def test_hours(self):
        due = C.parse_due_time("in 3 hours")
        assert abs(due - (datetime.now() + timedelta(hours=3)).timestamp()) < 5

    def test_days(self):
        due = C.parse_due_time("in 2 days")
        assert abs(due - (datetime.now() + timedelta(days=2)).timestamp()) < 5

    def test_weeks(self):
        due = C.parse_due_time("in 2 weeks")
        assert abs(due - (datetime.now() + timedelta(weeks=2)).timestamp()) < 5

    def test_tomorrow(self):
        due = C.parse_due_time("tomorrow")
        assert abs(due - (datetime.now() + timedelta(days=1)).timestamp()) < 5

    def test_next_week(self):
        due = C.parse_due_time("next week")
        assert abs(due - (datetime.now() + timedelta(weeks=1)).timestamp()) < 5

    def test_next_month(self):
        due = C.parse_due_time("next month")
        assert abs(due - (datetime.now() + timedelta(days=30)).timestamp()) < 5

    def test_tonight_is_in_the_future(self):
        # tonight resolves to a non-negative offset (capped at 0 past 8pm).
        due = C.parse_due_time("tonight")
        assert due is not None
        assert due >= datetime.now().timestamp() - 1

    def test_this_evening(self):
        due = C.parse_due_time("this evening")
        assert due is not None

    def test_this_afternoon(self):
        due = C.parse_due_time("this afternoon")
        assert due is not None

    def test_end_of_day(self):
        due = C.parse_due_time("end of day")
        assert due is not None

    def test_end_of_the_day_variant(self):
        due = C.parse_due_time("end of the day")
        assert due is not None

    def test_weekday_monday_is_future(self):
        due = C.parse_due_time("monday")
        assert due is not None
        assert due > datetime.now().timestamp()

    @pytest.mark.parametrize(
        "word",
        ["tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    )
    def test_each_weekday_resolves_to_future(self, word):
        due = C.parse_due_time(word)
        assert due is not None
        assert due > datetime.now().timestamp()

    def test_no_time_expression_returns_none(self):
        assert C.parse_due_time("just a plain sentence about nothing") is None

    def test_first_matching_pattern_wins(self):
        # "in 1 hour" is matched before "tomorrow"; the hour delta should win.
        due = C.parse_due_time("in 1 hour or maybe tomorrow")
        assert abs(due - (datetime.now() + timedelta(hours=1)).timestamp()) < 5

    def test_case_insensitive(self):
        due = C.parse_due_time("TOMORROW")
        assert due is not None


class TestDaysUntilWeekday:
    def test_future_weekday_within_a_week(self):
        delta = C._days_until_weekday((datetime.now().weekday() + 2) % 7)
        assert 1 <= delta.days <= 7

    def test_same_weekday_rolls_to_next_week(self):
        # Asking for today's weekday means "next week", never zero/negative.
        delta = C._days_until_weekday(datetime.now().weekday())
        assert delta.days == 7


# ══════════════════════════════════════════════════════════════════════════════
# 2. Extracting commitments from conversation text
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractCommitments:
    def test_remind_me_to(self):
        out = C.extract_commitments("remind me to call mom tomorrow.")
        assert len(out) == 1
        assert out[0]["kind"] == "reminder"
        assert "call mom" in out[0]["reason"]

    def test_dont_forget(self):
        out = C.extract_commitments("don't forget to water the plants.")
        assert any(c["kind"] == "reminder" and "water the plants" in c["reason"] for c in out)

    def test_need_to_remember(self):
        out = C.extract_commitments("I need to remember to renew the passport.")
        assert any("renew the passport" in c["reason"] for c in out)

    def test_assistant_follow_up_check(self):
        out = C.extract_commitments("", "I'll check on the build status in 2 hours.")
        assert any(c["kind"] == "follow_up" for c in out)

    def test_assistant_get_back_to_you(self):
        out = C.extract_commitments("", "Let me get back to you on the pricing tomorrow.")
        assert any(c["kind"] == "follow_up" and "pricing" in c["reason"] for c in out)

    def test_deadline_keyword(self):
        out = C.extract_commitments("the deadline is friday.")
        assert any(c["kind"] == "deadline" for c in out)

    def test_due_by_keyword(self):
        out = C.extract_commitments("the report is due by monday.")
        assert any(c["kind"] == "deadline" for c in out)

    def test_too_short_reason_skipped(self):
        # Reason "go" has < 3 chars and must be dropped.
        out = C.extract_commitments("remind me to go.")
        assert out == []

    def test_too_long_reason_skipped(self):
        long_reason = "x" * 600
        out = C.extract_commitments(f"remind me to {long_reason}.")
        assert out == []

    def test_default_due_when_no_time_in_context(self):
        # No time expression near the match → defaults to ~24h out.
        out = C.extract_commitments("remind me to feed the cat please thanks")
        assert len(out) == 1
        expected = (datetime.now() + timedelta(hours=24)).timestamp()
        assert abs(out[0]["due_at"] - expected) < 30

    def test_time_in_context_is_used(self):
        out = C.extract_commitments("remind me to stretch in 2 hours")
        assert len(out) == 1
        expected = (datetime.now() + timedelta(hours=2)).timestamp()
        assert abs(out[0]["due_at"] - expected) < 30

    def test_source_text_captured(self):
        out = C.extract_commitments("remind me to buy milk.")
        assert out[0]["source_text"].lower().startswith("remind me to buy milk")

    def test_deduplicates_by_reason_prefix(self):
        # Same reason appearing twice (via different patterns/casing) collapses.
        text = "Remind me to submit the form. Don't forget to submit the form."
        out = C.extract_commitments(text)
        reasons = [c["reason"].lower()[:50] for c in out]
        assert len(reasons) == len(set(reasons))

    def test_no_commitment_text_returns_empty(self):
        assert C.extract_commitments("hello, how are you today?") == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. Recording commitments (CRUD: add / get_pending)
# ══════════════════════════════════════════════════════════════════════════════


class TestRecordingCommitments:
    def test_add_returns_pending_record(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "call dentist", time.time() + 3600)
        assert rec.id
        assert rec.platform == "tg"
        assert rec.user_id == "u1"
        assert rec.kind == "reminder"
        assert rec.status == "pending"
        assert rec.reason == "call dentist"
        assert rec.created_at > 0

    def test_added_commitment_is_persisted_and_pending(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "ship release", time.time() + 7200)
        pending = C.get_pending("tg", "u1")
        assert any(p.id == rec.id for p in pending)

    def test_get_pending_ordered_by_due_at(self, db):
        now = time.time()
        C.add_commitment("tg", "u1", "reminder", "later", now + 9000)
        C.add_commitment("tg", "u1", "reminder", "sooner", now + 100)
        pending = C.get_pending("tg", "u1")
        assert [p.reason for p in pending] == ["sooner", "later"]

    def test_get_pending_isolated_by_user(self, db):
        C.add_commitment("tg", "alice", "reminder", "alice task", time.time() + 60)
        C.add_commitment("tg", "bob", "reminder", "bob task", time.time() + 60)
        assert len(C.get_pending("tg", "alice")) == 1
        assert C.get_pending("tg", "alice")[0].reason == "alice task"

    def test_get_pending_isolated_by_platform(self, db):
        C.add_commitment("tg", "u1", "reminder", "tg task", time.time() + 60)
        C.add_commitment("slack", "u1", "reminder", "slack task", time.time() + 60)
        assert len(C.get_pending("tg", "u1")) == 1
        assert len(C.get_pending("slack", "u1")) == 1

    def test_get_pending_empty_for_unknown_user(self, db):
        assert C.get_pending("tg", "nobody") == []

    def test_source_text_round_trips(self, db):
        C.add_commitment("tg", "u1", "reminder", "thing", time.time() + 60,
                         source_text="remind me to do thing")
        rec = C.get_pending("tg", "u1")[0]
        assert rec.source_text == "remind me to do thing"

    def test_ids_are_unique(self, db):
        ids = {C.add_commitment("tg", "u1", "reminder", f"r{i}", time.time() + i).id
               for i in range(25)}
        assert len(ids) == 25


# ══════════════════════════════════════════════════════════════════════════════
# 4. Recalling by date / expiry (get_due)
# ══════════════════════════════════════════════════════════════════════════════


class TestRecallingByDateAndExpiry:
    def test_past_due_commitment_is_returned(self, db):
        C.add_commitment("tg", "u1", "reminder", "overdue thing", time.time() - 60)
        due = C.get_due("tg", "u1")
        assert any(d.reason == "overdue thing" for d in due)

    def test_future_commitment_is_not_due(self, db):
        C.add_commitment("tg", "u1", "reminder", "future thing", time.time() + 3600)
        assert C.get_due("tg", "u1") == []

    def test_exactly_now_counts_as_due(self, db):
        C.add_commitment("tg", "u1", "reminder", "right now", time.time() - 0.5)
        assert any(d.reason == "right now" for d in C.get_due("tg", "u1"))

    def test_due_ordered_by_due_at(self, db):
        now = time.time()
        C.add_commitment("tg", "u1", "reminder", "old", now - 1000)
        C.add_commitment("tg", "u1", "reminder", "older", now - 5000)
        due = C.get_due("tg", "u1")
        assert [d.reason for d in due] == ["older", "old"]

    def test_snoozed_into_future_is_not_due(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "snoozed thing", time.time() - 60)
        C.snooze(rec.id, time.time() + 3600)
        assert C.get_due("tg", "u1") == []

    def test_snooze_elapsed_becomes_due_again(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "re-due thing", time.time() - 60)
        C.snooze(rec.id, time.time() - 10)  # snooze window already passed
        assert any(d.reason == "re-due thing" for d in C.get_due("tg", "u1"))

    def test_due_excludes_other_users(self, db):
        C.add_commitment("tg", "alice", "reminder", "alice overdue", time.time() - 60)
        assert C.get_due("tg", "bob") == []


# ══════════════════════════════════════════════════════════════════════════════
# 5. Status transitions: dismiss / mark_sent / snooze
# ══════════════════════════════════════════════════════════════════════════════


class TestStatusTransitions:
    def test_dismiss_removes_from_pending(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "to dismiss", time.time() + 60)
        C.dismiss(rec.id)
        assert all(p.id != rec.id for p in C.get_pending("tg", "u1"))

    def test_dismissed_not_returned_as_due(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "dismissed overdue", time.time() - 60)
        C.dismiss(rec.id)
        assert C.get_due("tg", "u1") == []

    def test_mark_sent_removes_from_pending(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "to send", time.time() + 60)
        C.mark_sent(rec.id)
        assert all(p.id != rec.id for p in C.get_pending("tg", "u1"))

    def test_mark_sent_not_returned_as_due(self, db):
        rec = C.add_commitment("tg", "u1", "reminder", "sent overdue", time.time() - 60)
        C.mark_sent(rec.id)
        assert C.get_due("tg", "u1") == []

    def test_snooze_keeps_it_pending(self, db):
        # Snoozing does not change status; it stays visible in get_pending.
        rec = C.add_commitment("tg", "u1", "reminder", "snoozed but pending", time.time() - 60)
        C.snooze(rec.id, time.time() + 3600)
        assert any(p.id == rec.id for p in C.get_pending("tg", "u1"))


# ══════════════════════════════════════════════════════════════════════════════
# 6. auto_extract_and_store convenience wrapper
# ══════════════════════════════════════════════════════════════════════════════


class TestAutoExtractAndStore:
    def test_extracts_and_persists(self, db):
        records = C.auto_extract_and_store(
            "tg", "u1", "remind me to file taxes in 3 days"
        )
        assert len(records) == 1
        assert records[0].reason.startswith("file taxes")
        # Persisted and pending.
        assert any(p.id == records[0].id for p in C.get_pending("tg", "u1"))

    def test_no_commitments_stores_nothing(self, db):
        records = C.auto_extract_and_store("tg", "u1", "good morning!")
        assert records == []
        assert C.get_pending("tg", "u1") == []

    def test_carries_source_text_through(self, db):
        records = C.auto_extract_and_store("tg", "u1", "remind me to backup the server")
        assert records[0].source_text != ""

    def test_multiple_commitments_stored(self, db):
        records = C.auto_extract_and_store(
            "tg", "u1",
            "remind me to call alice tomorrow. don't forget to email bob next week.",
        )
        assert len(records) >= 2


# ══════════════════════════════════════════════════════════════════════════════
# 7. Formatting pending commitments for display
# ══════════════════════════════════════════════════════════════════════════════


class TestFormatPending:
    def test_empty_list_message(self):
        assert C.format_pending([]) == "📋 No pending reminders."

    def test_header_present(self):
        recs = [C.CommitmentRecord(
            id="abc", platform="tg", user_id="u1", kind="reminder", status="pending",
            reason="do the thing", due_at=time.time() + 3600, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert "Pending reminders" in out
        assert "do the thing" in out
        assert "`abc`" in out

    def test_overdue_labelled(self):
        recs = [C.CommitmentRecord(
            id="ovd", platform="tg", user_id="u1", kind="reminder", status="pending",
            reason="late thing", due_at=time.time() - 3600, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert "overdue" in out

    def test_days_away_formatting(self):
        recs = [C.CommitmentRecord(
            id="d", platform="tg", user_id="u1", kind="reminder", status="pending",
            reason="multi-day", due_at=time.time() + 3 * 86400 + 600, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert "in 3d" in out

    def test_hours_away_formatting(self):
        recs = [C.CommitmentRecord(
            id="h", platform="tg", user_id="u1", kind="reminder", status="pending",
            reason="few hours", due_at=time.time() + 2 * 3600 + 120, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert "in 2h" in out

    def test_minutes_away_formatting(self):
        recs = [C.CommitmentRecord(
            id="m", platform="tg", user_id="u1", kind="reminder", status="pending",
            reason="soon", due_at=time.time() + 5 * 60 + 30, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert "in 5m" in out

    @pytest.mark.parametrize(
        "kind,icon",
        [("reminder", "🔔"), ("follow_up", "🔄"), ("deadline", "⏳"), ("other", "📌")],
    )
    def test_kind_icons(self, kind, icon):
        recs = [C.CommitmentRecord(
            id="k", platform="tg", user_id="u1", kind=kind, status="pending",
            reason="iconned", due_at=time.time() + 3600, created_at=time.time(),
        )]
        out = C.format_pending(recs)
        assert icon in out


# ══════════════════════════════════════════════════════════════════════════════
# 8. Schema / init
# ══════════════════════════════════════════════════════════════════════════════


class TestInitDb:
    def test_init_db_is_idempotent(self, db):
        # Calling init_db twice must not raise (uses IF NOT EXISTS).
        C.init_db()
        C.init_db()
        # Table is usable afterwards.
        C.add_commitment("tg", "u1", "reminder", "after reinit", time.time() + 60)
        assert len(C.get_pending("tg", "u1")) == 1


class TestParseRow:
    def test_parse_row_without_optional_columns(self, db):
        # A row dict missing source_text / snoozed_until falls back to defaults.
        class FakeRow:
            _data = {
                "id": "x1", "platform": "tg", "user_id": "u1", "kind": "reminder",
                "status": "pending", "reason": "r", "due_at": 1.0, "created_at": 2.0,
            }

            def keys(self):
                return list(self._data.keys())

            def __getitem__(self, k):
                return self._data[k]

        rec = C._parse_row(FakeRow())
        assert rec.source_text == ""
        assert rec.snoozed_until == 0
