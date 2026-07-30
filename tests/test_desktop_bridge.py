"""Tests for the Claude Desktop bridge (docs/improvements.md item 17).

`grep -rl desktop_bridge tests/` used to return nothing: 1,817 lines implementing
the project's differentiator — hook handling, approval decisions, transcript
parsing, `claude --resume` injection, Telegram transport — had no test at all,
measured at 19% coverage by the rest of the suite touching it incidentally.
Meanwhile all five queued tickets (0023–0027) list `tests/test_desktop_bridge.py`
in their `touches:` frontmatter, so whoever claimed the first one inherited the
whole harness as unscoped work.

This file is that harness. Four fakes, all in the ``bridge`` fixture:

  - **a fake ``~/.claude/projects`` tree** — `desktop_bridge.HOME` is a
    module-level Path read at import, so pointing it at a tmpdir gives real
    transcript files to parse without touching the developer's own sessions;
  - **a stubbed ``_tg_call``** — records outbound Telegram calls instead of
    making them, and is what lets card structure and button payloads be asserted;
  - **a fake ``claude`` binary** — a shell script, so the resume and digest paths
    exercise real ``subprocess.run`` plumbing (argv, cwd, env) rather than a mock
    of it;
  - **an isolated bridge DB** — a temp `store` DB with the bridge tables created,
    so approvals, session cards, and follow state persist as they do in
    production.

Covered here: transcript parsing, digest formatting and its no-summarizer
fallback, the approve/deny hook round-trip, short-session-id resolution, and text
routing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from telechat_pkg import desktop_bridge as db
from telechat_pkg import store


# ══════════════════════════════════════════════════════════════════════════════
# Harness
# ══════════════════════════════════════════════════════════════════════════════


class FakeTelegram:
    """Records what the bridge would have sent, and answers like Telegram does."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_message_id = 1000
        self.fail_all = False

    def __call__(self, method: str, **params):
        self.calls.append((method, params))
        if self.fail_all:
            return None
        self._next_message_id += 1
        return {"ok": True, "result": {"message_id": self._next_message_id}}

    # ── assertions helpers ──

    @property
    def sent(self) -> list[dict]:
        """Params of every sendMessage call, in order."""
        return [p for m, p in self.calls if m == "sendMessage"]

    @property
    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.sent]

    def buttons(self, index: int = -1) -> list[dict]:
        """Flattened inline-keyboard buttons of the index-th sendMessage."""
        markup = self.sent[index].get("reply_markup")
        if not markup:
            return []
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [b for row in markup["inline_keyboard"] for b in row]

    def callback_data(self, index: int = -1) -> list[str]:
        return [b["callback_data"] for b in self.buttons(index)]


class BridgeHarness:
    """Fake home + fake claude + fake Telegram + isolated DB."""

    def __init__(self, home: Path, tg: FakeTelegram, claude_bin: Path):
        self.home = home
        self.tg = tg
        self.claude_bin = claude_bin
        self.projects = home / ".claude" / "projects"

    # ── project directories ──

    def make_cwd(self, name: str = "proj") -> str:
        """Create a real directory to stand in for a session's working directory.

        `claude --resume` is spawned with ``cwd=`` set, so a path that doesn't
        exist makes subprocess raise before the binary runs — tests that assert
        on the resume need a directory that is actually there.
        """
        path = self.home / "projects" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    # ── transcripts ──

    def write_transcript(self, sid: str, cwd: str, entries: list[dict]) -> Path:
        """Write a Claude Code JSONL transcript for ``sid`` under ``cwd``'s project dir.

        Mirrors the real layout: ~/.claude/projects/<cwd-with-slashes-as-dashes>/<sid>.jsonl
        """
        proj = self.projects / cwd.replace("/", "-")
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / f"{sid}.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        return path

    def assistant_turn(self, text: str = "", tools: list[str] | None = None, cwd: str = "/tmp/proj") -> dict:
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for name in tools or []:
            content.append({"type": "tool_use", "name": name})
        return {"type": "assistant", "cwd": cwd, "message": {"content": content}}

    def user_turn(self, text: str, cwd: str = "/tmp/proj") -> dict:
        return {"type": "user", "cwd": cwd, "message": {"content": [{"type": "text", "text": text}]}}

    # ── the fake claude binary ──

    def set_claude_output(self, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Rewrite the fake `claude` so the next invocation produces this output.

        It also appends its argv, cwd, and a couple of env vars to `invocations`,
        so tests can assert on how the bridge spawned it — that `--resume` gets
        the resolved cwd, that the digest is guarded with
        TELECHAT_BRIDGE_INTERNAL, and so on.
        """
        log = self.home / "claude-invocations.log"
        script = f"""#!/bin/sh
{{
  printf 'argv:'
  for a in "$@"; do printf ' %s' "$a"; done
  printf '\\ncwd: %s\\n' "$(pwd)"
  printf 'internal: %s\\n' "${{TELECHAT_BRIDGE_INTERNAL:-}}"
}} >> {log}
cat <<'CLAUDE_STDOUT_EOF'
{stdout}
CLAUDE_STDOUT_EOF
cat <<'CLAUDE_STDERR_EOF' >&2
{stderr}
CLAUDE_STDERR_EOF
exit {exit_code}
"""
        self.claude_bin.write_text(script)
        self.claude_bin.chmod(self.claude_bin.stat().st_mode | stat.S_IEXEC)

    @property
    def invocations(self) -> list[str]:
        log = self.home / "claude-invocations.log"
        if not log.exists():
            return []
        return [b.strip() for b in log.read_text().split("argv:") if b.strip()]

    def wait_for_invocation(self, count: int = 1, timeout: float = 10.0) -> bool:
        """The resume path runs on a daemon thread — wait for it rather than sleeping."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.invocations) >= count:
                return True
            time.sleep(0.02)
        return False

    def wait_for_send(self, count: int = 1, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.tg.sent) >= count:
                return True
            time.sleep(0.02)
        return False

    def wait_for_text(self, needle: str, timeout: float = 10.0) -> bool:
        """Wait for a sent message containing ``needle``.

        Prefer this over :meth:`wait_for_send` when asserting on content produced
        by the resume path. `_run_resume_background` spawns a daemon thread that
        resolves ``_tg_call`` at call time, so a thread left over from an earlier
        test posts into *this* test's fake — which can satisfy a bare count check
        before the message under test has actually arrived.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in t for t in self.tg.texts):
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".telechat").mkdir(parents=True)

    # A .env the bridge can read a chat id and token out of, so _tg_send doesn't
    # bail before reaching the stubbed transport.
    (home / ".telechat" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=424242\n"
    )

    monkeypatch.setattr(db, "HOME", home)
    monkeypatch.setattr(db, "TELECHAT_HOME", home / ".telechat")

    claude_bin = home / "claude"
    claude_bin.write_text("#!/bin/sh\nexit 0\n")
    claude_bin.chmod(0o755)
    monkeypatch.setattr(db, "_find_claude_bin", lambda: str(claude_bin))

    tg = FakeTelegram()
    monkeypatch.setattr(db, "_tg_call", tg)

    # Isolated DB with the bridge tables. _SCHEMA_READY is a module global that
    # would otherwise skip creation against this fresh database.
    store.shutdown_writer(timeout=2.0)
    orig_db_path, orig_local = store.DB_PATH, store._local
    store.DB_PATH = str(tmp_path / "bridge_test.db")
    store._local = threading.local()
    store.init_db()
    monkeypatch.setattr(db, "_SCHEMA_READY", False)
    db.init_bridge_schema(store._get_conn())
    store._get_conn().commit()

    harness = BridgeHarness(home, tg, claude_bin)
    yield harness

    store.shutdown_writer(timeout=2.0)
    store._reset_conn_state()
    store.DB_PATH = orig_db_path
    store._local = orig_local


@pytest.fixture
def no_summarizer(monkeypatch):
    """Force the digest path's fallback — as when the model is unavailable."""
    monkeypatch.setattr(db, "_summarize", lambda raw: None)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Transcript parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestTranscriptParsing:
    def test_finds_transcript_by_explicit_path(self, bridge):
        path = bridge.write_transcript("sid-a", "/tmp/proj", [bridge.assistant_turn("hi")])
        assert db._find_transcript({"transcript_path": str(path)}) == path

    def test_finds_transcript_from_session_and_cwd(self, bridge):
        path = bridge.write_transcript("sid-b", "/tmp/proj", [bridge.assistant_turn("hi")])
        found = db._find_transcript({"session_id": "sid-b", "cwd": "/tmp/proj"})
        assert found == path

    def test_finds_transcript_by_sid_when_cwd_is_wrong(self, bridge):
        # A hook can fire with a stale subdirectory as cwd; the sid still locates it.
        path = bridge.write_transcript("sid-c", "/tmp/proj", [bridge.assistant_turn("hi")])
        found = db._find_transcript({"session_id": "sid-c", "cwd": "/somewhere/else"})
        assert found == path

    def test_missing_transcript_path_falls_through_rather_than_returning_it(self, bridge):
        assert db._find_transcript({"transcript_path": "/no/such/file.jsonl"}) is None

    def test_last_assistant_text_reads_the_newest_turn(self, bridge):
        path = bridge.write_transcript("sid-d", "/tmp/proj", [
            bridge.assistant_turn("first answer"),
            bridge.user_turn("follow-up"),
            bridge.assistant_turn("second answer"),
        ])
        assert db._last_assistant_text(path) == "second answer"

    def test_last_assistant_text_skips_a_tool_only_turn_for_a_text_one(self, bridge):
        path = bridge.write_transcript("sid-e", "/tmp/proj", [
            bridge.assistant_turn("the actual answer"),
            bridge.assistant_turn(tools=["Read", "Grep"]),
        ])
        # The newest turn has no text, so its tool names are reported.
        assert db._last_assistant_text(path) == "(ran tools: Read, Grep)"

    def test_last_assistant_text_truncates_at_max_chars(self, bridge):
        path = bridge.write_transcript("sid-f", "/tmp/proj", [bridge.assistant_turn("x" * 500)])
        out = db._last_assistant_text(path, max_chars=50)
        assert len(out) == 51  # 50 chars + the ellipsis
        assert out.endswith("…")

    def test_malformed_lines_are_skipped_not_fatal(self, bridge):
        # Transcripts are appended to live; a torn final line is normal.
        proj = bridge.projects / "-tmp-proj"
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / "sid-g.jsonl"
        path.write_text(
            json.dumps(bridge.assistant_turn("good turn")) + "\n"
            + '{"type": "assistant", "message": {"content": [{"type": "te\n'
        )
        assert db._last_assistant_text(path) == "good turn"

    def test_no_transcript_yields_none_rather_than_raising(self, bridge):
        assert db._last_assistant_text(None) is None
        assert db._last_assistant_text(Path("/no/such/file.jsonl")) is None

    def test_transcript_with_no_assistant_turn_yields_none(self, bridge):
        path = bridge.write_transcript("sid-h", "/tmp/proj", [bridge.user_turn("only me")])
        assert db._last_assistant_text(path) is None

    def test_resolve_session_cwd_reads_the_true_cwd_from_the_transcript(self, bridge):
        # This is what stops `claude --resume` failing with "No conversation found":
        # the payload cwd can be a subdirectory, the transcript records the real one.
        bridge.write_transcript("sid-i", "/tmp/proj", [
            bridge.assistant_turn("hi", cwd="/tmp/proj"),
        ])
        assert db._resolve_session_cwd("sid-i", fallback="/tmp/proj/sub") == "/tmp/proj"

    def test_resolve_session_cwd_falls_back_when_there_is_no_transcript(self, bridge):
        assert db._resolve_session_cwd("unknown-sid", fallback="/tmp/x") == "/tmp/x"
        assert db._resolve_session_cwd("unknown-sid") is None

    def test_tail_reader_agrees_with_the_full_reader(self, bridge):
        path = bridge.write_transcript("sid-j", "/tmp/proj", [
            bridge.assistant_turn("a short answer"),
        ])
        assert db._tail_last_assistant(path, max_chars=70) == "a short answer"

    def test_tail_reader_handles_a_file_larger_than_its_window(self, bridge):
        # It seeks to the last 64KiB, which can land mid-line — the partial line
        # must be skipped rather than crashing the read.
        entries = [bridge.assistant_turn("filler " * 200) for _ in range(80)]
        entries.append(bridge.assistant_turn("the newest answer"))
        path = bridge.write_transcript("sid-k", "/tmp/proj", entries)
        assert path.stat().st_size > 65536
        assert db._tail_last_assistant(path, max_chars=70) == "the newest answer"

    def test_session_status_reports_busy_for_a_fresh_transcript(self, bridge):
        bridge.write_transcript("sid-l", "/tmp/proj", [bridge.assistant_turn("working")])
        status = db._session_status("sid-l")
        assert status["busy"] is True          # just written, so mid-turn
        assert status["last"] == "working"

    def test_session_status_reports_idle_for_an_old_transcript(self, bridge):
        path = bridge.write_transcript("sid-m", "/tmp/proj", [bridge.assistant_turn("done")])
        old = time.time() - (db._BUSY_WINDOW_SECS + 60)
        os.utime(path, (old, old))
        assert db._session_status("sid-m")["busy"] is False

    def test_session_status_of_an_unknown_session_is_not_busy(self, bridge):
        assert db._session_status("nope") == {"busy": False, "last": None}


# ══════════════════════════════════════════════════════════════════════════════
# 2. Digest formatting and the no-summarizer fallback
# ══════════════════════════════════════════════════════════════════════════════


class TestDigestFormatting:
    def test_status_word_becomes_an_icon_and_the_rest_becomes_the_body(self):
        text, has_decision = db._format_digest("DONE\nRefactored the parser and tests pass.")
        assert text.startswith(db._STATUS_ICONS["DONE"])
        assert "*DONE*" in text
        assert "Refactored the parser" in text
        assert has_decision is False

    def test_decision_line_is_pulled_out_and_flagged(self):
        text, has_decision = db._format_digest(
            "NEEDS DECISION\nTwo migration paths are possible.\n"
            "DECISION: Should I use the in-place migration or a copy?"
        )
        assert has_decision is True
        assert "NEEDS YOU" in text
        assert "in-place migration" in text

    def test_a_status_of_needs_decision_counts_even_without_a_decision_line(self):
        _, has_decision = db._format_digest("NEEDS DECISION\nWaiting on you.")
        assert has_decision is True

    def test_unrecognised_first_line_defaults_to_update_and_keeps_the_line(self):
        text, has_decision = db._format_digest("Some prose with no status word.")
        assert "*UPDATE*" in text
        assert "Some prose" in text
        assert has_decision is False

    def test_empty_digest_does_not_raise(self):
        text, has_decision = db._format_digest("")
        assert has_decision is False
        assert isinstance(text, str)

    def test_markdown_metacharacters_are_neutralised(self):
        # The cards are sent with parse_mode=Markdown, so an unbalanced * or _
        # from model output would make Telegram reject the whole message.
        text, _ = db._format_digest("DONE\nEdited *a_file* with `backticks` and [brackets]")
        body = text.split("\n", 1)[1]
        for ch in ("`", "*", "_", "[", "]"):
            assert ch not in body

    def test_meta_response_is_recognised_as_the_model_talking_back(self):
        assert db._looks_like_meta_response("I don't have any content to summarize.")
        assert db._looks_like_meta_response("Please provide the text you'd like summarized")
        assert not db._looks_like_meta_response("DONE\nRefactored the parser.")

    def test_summarize_declines_input_below_the_minimum(self, bridge):
        assert db._summarize("tiny") is None
        assert bridge.invocations == []  # and doesn't spawn claude for it

    def test_summarize_returns_the_models_text(self, bridge):
        bridge.set_claude_output(stdout="DONE\nAll set.")
        out = db._summarize("x" * (db._DIGEST_MIN_CHARS + 50))
        assert out == "DONE\nAll set."

    def test_summarize_guards_its_own_hooks_from_re_notifying(self, bridge):
        bridge.set_claude_output(stdout="DONE\nAll set.")
        db._summarize("x" * (db._DIGEST_MIN_CHARS + 50))
        # Without TELECHAT_BRIDGE_INTERNAL the digest's own Stop hook would fire
        # a notification about the digest — a feedback loop.
        assert "internal: 1" in bridge.invocations[0]

    def test_summarize_rejects_a_meta_response_so_the_caller_falls_back(self, bridge):
        bridge.set_claude_output(stdout="I don't have any content to summarize.")
        assert db._summarize("x" * (db._DIGEST_MIN_CHARS + 50)) is None

    def test_summarize_survives_a_failing_binary(self, bridge):
        bridge.set_claude_output(stderr="boom", exit_code=1)
        assert db._summarize("x" * (db._DIGEST_MIN_CHARS + 50)) is None


class TestDigestCard:
    def test_card_carries_status_use_session_and_full_output_buttons(self, bridge):
        bridge.set_claude_output(stdout="DONE\nFinished the refactor.")
        r = db._digest_card("✅ *Stop*", "x" * 200, session_short="abcd1234")
        assert r and r["ok"]
        data = bridge.tg.callback_data()
        assert "bridge:act:abcd1234:status" in data
        assert "bridge:use:abcd1234" in data
        assert any(d.startswith("bridge:full:") for d in data)

    def test_proceed_button_appears_only_for_a_decision(self, bridge):
        bridge.set_claude_output(stdout="NEEDS DECISION\nPick one.\nDECISION: A or B?")
        db._digest_card("🔔 *Notification*", "x" * 200, session_short="abcd1234")
        assert "bridge:act:abcd1234:proceed" in bridge.tg.callback_data()

        bridge.tg.calls.clear()
        bridge.set_claude_output(stdout="DONE\nNothing needed.")
        db._digest_card("✅ *Stop*", "x" * 200, session_short="abcd1234")
        assert "bridge:act:abcd1234:proceed" not in bridge.tg.callback_data()

    def test_without_a_session_only_the_full_output_button_is_offered(self, bridge):
        bridge.set_claude_output(stdout="DONE\nFinished.")
        db._digest_card("✅ *Stop*", "x" * 200, session_short=None)
        data = bridge.tg.callback_data()
        assert len(data) == 1 and data[0].startswith("bridge:full:")

    def test_full_output_button_resolves_to_the_original_text(self, bridge):
        bridge.set_claude_output(stdout="DONE\nSummarised.")
        raw = "the complete untruncated output " * 40
        db._digest_card("✅ *Stop*", raw, session_short="abcd1234")
        token = next(d for d in bridge.tg.callback_data() if d.startswith("bridge:full:"))
        assert db._get_full_output(token.split(":")[-1]) == raw

    def test_no_summarizer_falls_back_to_the_full_text(self, bridge, no_summarizer):
        raw = "line one\nline two\nline three"
        assert db._digest_card("✅ *Stop*", raw, session_short="abcd1234") is None
        # The fallback must still deliver the content — a failed digest cannot be
        # allowed to swallow the assistant's actual output.
        assert any("line two" in t for t in bridge.tg.texts)

    def test_unknown_full_output_token_yields_none(self, bridge):
        assert db._get_full_output("no-such-token") is None

    def test_full_output_table_is_bounded(self, bridge):
        for i in range(205):
            db._store_full_output(f"content {i}")
        conn = store._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM bridge_full_outputs").fetchone()[0]
        assert count <= 200


# ══════════════════════════════════════════════════════════════════════════════
# 3. Short-session-id resolution
# ══════════════════════════════════════════════════════════════════════════════


class TestShortSessionResolution:
    def _record_card(self, sid: str, cwd: str, message_id: int = 1) -> None:
        conn = store._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO bridge_session_messages(message_id,session_id,cwd,created_at)"
            " VALUES(?,?,?,?)",
            (message_id, sid, cwd, "2026-07-30T00:00:00"),
        )
        conn.commit()

    def test_resolves_a_short_id_from_a_notified_session(self, bridge):
        self._record_card("abcd1234-full-session-id", "/tmp/proj")
        assert db.resolve_short_session("abcd1234") == ("abcd1234-full-session-id", "/tmp/proj")

    def test_resolution_is_case_insensitive_and_trims(self, bridge):
        self._record_card("abcd1234-full-session-id", "/tmp/proj")
        assert db.resolve_short_session("  ABCD1234 ") is not None

    def test_newest_card_wins_when_a_prefix_matches_several(self, bridge):
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO bridge_session_messages(message_id,session_id,cwd,created_at) VALUES(?,?,?,?)",
            (1, "abcd1234-older", "/tmp/old", "2026-07-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO bridge_session_messages(message_id,session_id,cwd,created_at) VALUES(?,?,?,?)",
            (2, "abcd1234-newer", "/tmp/new", "2026-07-30T00:00:00"),
        )
        conn.commit()
        assert db.resolve_short_session("abcd1234")[0] == "abcd1234-newer"

    def test_falls_back_to_the_transcript_tree_for_a_session_never_notified(self, bridge):
        # The point of the on-disk fallback: any existing session can be resumed,
        # not only ones the bridge has already sent a card for.
        bridge.write_transcript("ffff9999-never-notified", "/tmp/other", [
            bridge.assistant_turn("hello", cwd="/tmp/other"),
        ])
        assert db.resolve_short_session("ffff9999") == (
            "ffff9999-never-notified", "/tmp/other",
        )

    def test_empty_or_unknown_short_id_yields_none(self, bridge):
        assert db.resolve_short_session("") is None
        assert db.resolve_short_session("   ") is None
        assert db.resolve_short_session("deadbeef") is None


class TestCurrentSession:
    def test_set_get_and_clear_round_trip(self, bridge):
        assert db.get_current_session() == (None, None)
        db.set_current_session("sid-1", "/tmp/proj")
        assert db.get_current_session() == ("sid-1", "/tmp/proj")
        db.set_current_session(None, None)
        assert db.get_current_session() == (None, None)

    def test_a_partial_pin_clears_rather_than_half_setting(self, bridge):
        db.set_current_session("sid-1", "/tmp/proj")
        db.set_current_session("sid-2", None)
        assert db.get_current_session() == (None, None)


# ══════════════════════════════════════════════════════════════════════════════
# 4. The approve/deny hook round-trip
# ══════════════════════════════════════════════════════════════════════════════


class TestApproveHook:
    def _decide(self, req_id: str, decision: str) -> None:
        conn = store._get_conn()
        conn.execute(
            "UPDATE bridge_approvals SET decision=?, decided_at=? WHERE request_id=?",
            (decision, "2026-07-30T00:00:00", req_id),
        )
        conn.commit()

    def _answer_when_asked(self, decision: str) -> None:
        """Decide as soon as the hook records its request, from another thread."""
        def wait_and_decide():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                conn = sqlite3.connect(store.DB_PATH)
                try:
                    row = conn.execute(
                        "SELECT request_id FROM bridge_approvals WHERE decision IS NULL"
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE bridge_approvals SET decision=?, decided_at=? WHERE request_id=?",
                            (decision, "2026-07-30T00:00:00", row[0]),
                        )
                        conn.commit()
                        return
                finally:
                    conn.close()
                time.sleep(0.02)
        threading.Thread(target=wait_and_decide, daemon=True).start()

    def test_passes_through_when_approve_mode_is_off(self, bridge):
        result = db.hook_approve({"cwd": "/tmp/proj", "session_id": "sid", "tool_name": "Bash"})
        assert result is None
        assert bridge.tg.sent == []  # and doesn't bother the user

    def test_passes_through_when_there_is_no_cwd(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        assert db.hook_approve({"session_id": "sid", "tool_name": "Bash"}) is None

    def test_approval_returns_an_allow_decision(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        self._answer_when_asked("y")
        result = db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "rm -rf build"},
        })
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_denial_returns_a_deny_decision_with_a_reason(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        self._answer_when_asked("n")
        result = db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Write", "tool_input": {"file_path": "/etc/hosts"},
        })
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert out["permissionDecisionReason"]

    def test_the_card_offers_approve_and_deny_for_the_same_request(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        self._answer_when_asked("y")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "ls"},
        })
        data = bridge.tg.callback_data(0)
        assert len(data) == 2
        req_ids = {d.split(":")[2] for d in data}
        assert len(req_ids) == 1                       # both buttons, one request
        assert {d.split(":")[3] for d in data} == {"y", "n"}

    def test_the_prompt_shows_the_command_for_bash(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        self._answer_when_asked("y")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /important"},
        })
        assert "rm -rf /important" in bridge.tg.texts[0]

    def test_the_prompt_shows_the_path_for_a_write(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        self._answer_when_asked("y")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Edit", "tool_input": {"file_path": "/tmp/proj/main.py"},
        })
        assert "/tmp/proj/main.py" in bridge.tg.texts[0]

    def test_the_global_toggle_overrides_per_project_settings(self, bridge):
        db._state_set("approve_all", "1")
        assert db.approve_mode_on("/never/configured") is True

    def test_per_project_mode_can_be_turned_back_off(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        assert db.approve_mode_on("/tmp/proj") is True
        db.set_approve_mode("/tmp/proj", False)
        assert db.approve_mode_on("/tmp/proj") is False

    def test_a_timed_out_request_falls_through_to_the_normal_flow(self, bridge, monkeypatch):
        # The README documents fail-open after five minutes. Nobody decides here,
        # so the hook must return None rather than blocking or denying.
        db.set_approve_mode("/tmp/proj", True)
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
        monkeypatch.setattr(db.time, "time", lambda: next(clock, 10_000.0))
        result = db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "ls"},
        })
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. Text routing
# ══════════════════════════════════════════════════════════════════════════════


class FakeMessage:
    def __init__(self, text: str, message_id: int = 1, reply_to=None):
        self.text = text
        self.message_id = message_id
        self.reply_to_message = reply_to
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=self.message_id + 1000)


def _update(text: str, reply_to=None) -> SimpleNamespace:
    return SimpleNamespace(message=FakeMessage(text, reply_to=reply_to))


class TestTextRouting:
    @pytest.mark.asyncio
    async def test_ignores_commands_and_empty_text(self, bridge):
        db.set_current_session("sid-1", "/tmp/proj")
        assert await db.try_handle_text_message(_update("/desktop"), None) is False
        assert await db.try_handle_text_message(_update("   "), None) is False

    @pytest.mark.asyncio
    async def test_no_target_means_the_bot_answers_normally(self, bridge):
        # The bridge must not consume a plain message when nothing is targeted,
        # or the bot stops working as an assistant.
        assert await db.try_handle_text_message(_update("what is 2+2?"), None) is False

    @pytest.mark.asyncio
    async def test_does_not_hijack_when_a_session_merely_happens_to_be_running(
        self, bridge, monkeypatch
    ):
        # Regression: routing used to fall back to "the only running session", so
        # every plain message was injected into whatever Claude session was open.
        monkeypatch.setattr(
            db, "list_running_sessions",
            lambda: [{"sid": "abcd1234-x", "cwd": "/tmp/proj", "pid": "1"}],
        )
        assert await db.try_handle_text_message(_update("hello bot"), None) is False
        assert bridge.invocations == []

    @pytest.mark.asyncio
    async def test_routes_to_the_pinned_session(self, bridge):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, [
            bridge.assistant_turn("ready", cwd=cwd),
        ])
        bridge.set_claude_output(stdout="Done — applied the change.")
        db.set_current_session("abcd1234-x", cwd)

        upd = _update("please refactor the parser")
        assert await db.try_handle_text_message(upd, None) is True
        assert "proj" in upd.message.replies[0]
        assert bridge.wait_for_invocation()

        inv = bridge.invocations[0]
        assert "--resume abcd1234-x" in inv
        assert "please refactor the parser" in inv

    @pytest.mark.asyncio
    async def test_routes_a_reply_to_the_session_of_the_card_it_replies_to(self, bridge):
        conn = store._get_conn()
        carded = bridge.make_cwd("carded")
        conn.execute(
            "INSERT INTO bridge_session_messages(message_id,session_id,cwd,created_at)"
            " VALUES(?,?,?,?)",
            (555, "card-session-id", carded, "2026-07-30T00:00:00"),
        )
        conn.commit()
        bridge.write_transcript("card-session-id", carded, [
            bridge.assistant_turn("ready", cwd=carded),
        ])
        bridge.set_claude_output(stdout="ack")
        # A pinned session must lose to an explicit reply target.
        db.set_current_session("other-session", "/tmp/other")

        upd = _update("yes go ahead", reply_to=SimpleNamespace(message_id=555))
        assert await db.try_handle_text_message(upd, None) is True
        assert bridge.wait_for_invocation()
        assert "--resume card-session-id" in bridge.invocations[0]

    @pytest.mark.asyncio
    async def test_a_reply_to_an_unrelated_message_is_not_routed(self, bridge):
        upd = _update("hello", reply_to=SimpleNamespace(message_id=999))
        assert await db.try_handle_text_message(upd, None) is False

    @pytest.mark.asyncio
    async def test_resume_runs_in_the_sessions_true_cwd(self, bridge):
        # `claude --resume` is scoped by cwd; resuming from a stale subdirectory
        # fails with "No conversation found".
        real_cwd = bridge.home / "realproj"
        real_cwd.mkdir()
        bridge.write_transcript("abcd1234-x", str(real_cwd), [
            bridge.assistant_turn("ready", cwd=str(real_cwd)),
        ])
        bridge.set_claude_output(stdout="ok")
        db.set_current_session("abcd1234-x", str(real_cwd / "subdir"))

        assert await db.try_handle_text_message(_update("hi"), None) is True
        assert bridge.wait_for_invocation()
        assert f"cwd: {real_cwd}" in bridge.invocations[0]

    @pytest.mark.asyncio
    async def test_a_busy_session_warns_but_still_sends(self, bridge):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, [
            bridge.assistant_turn("mid-turn", cwd=cwd),
        ])
        bridge.set_claude_output(stdout="ok")
        db.set_current_session("abcd1234-x", cwd)

        upd = _update("another message")
        assert await db.try_handle_text_message(upd, None) is True
        assert "queue" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_the_reply_lands_as_a_digest_card(self, bridge):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, [
            bridge.assistant_turn("ready", cwd=cwd),
        ])
        bridge.set_claude_output(stdout="DONE\nApplied the refactor.")
        db.set_current_session("abcd1234-x", cwd)

        await db.try_handle_text_message(_update("do it"), None)
        assert bridge.wait_for_text("Reply from")


class TestResumeRunner:
    def test_stderr_is_reported_when_there_is_no_stdout(self, bridge, no_summarizer):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, [
            bridge.assistant_turn("ready", cwd=cwd),
        ])
        bridge.set_claude_output(stderr="No conversation found", exit_code=1)
        db._run_resume_background("abcd1234-x", cwd, "hello")
        assert bridge.wait_for_text("No conversation found")

    def test_the_resume_is_guarded_against_its_own_stop_hook(self, bridge, no_summarizer):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, [
            bridge.assistant_turn("ready", cwd=cwd),
        ])
        bridge.set_claude_output(stdout="fine")
        db._run_resume_background("abcd1234-x", cwd, "hello")
        assert bridge.wait_for_invocation()
        # Otherwise the resumed session's Stop hook posts a second, redundant card.
        assert "internal: 1" in bridge.invocations[0]


# ══════════════════════════════════════════════════════════════════════════════
# 6. hook_notify — the card the whole feature hangs off
# ══════════════════════════════════════════════════════════════════════════════


class TestNotifyHook:
    def test_a_stop_card_includes_the_last_assistant_message(self, bridge, no_summarizer):
        bridge.write_transcript("abcd1234-session", "/tmp/proj", [
            bridge.assistant_turn("Finished the migration.", cwd="/tmp/proj"),
        ])
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        assert any("Finished the migration." in t for t in bridge.tg.texts)

    def test_the_card_is_recorded_so_replies_can_be_routed_back(self, bridge, no_summarizer):
        bridge.write_transcript("abcd1234-session", "/tmp/proj", [
            bridge.assistant_turn("done", cwd="/tmp/proj"),
        ])
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        conn = store._get_conn()
        row = conn.execute(
            "SELECT session_id, cwd FROM bridge_session_messages"
        ).fetchone()
        assert tuple(row) == ("abcd1234-session", "/tmp/proj")

    def test_a_failed_send_records_nothing(self, bridge, no_summarizer):
        bridge.write_transcript("abcd1234-session", "/tmp/proj", [
            bridge.assistant_turn("done", cwd="/tmp/proj"),
        ])
        bridge.tg.fail_all = True
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        conn = store._get_conn()
        # A row whose message_id doesn't exist would route replies nowhere.
        assert conn.execute("SELECT COUNT(*) FROM bridge_session_messages").fetchone()[0] == 0

    def test_a_notification_event_carries_its_message(self, bridge, no_summarizer):
        db.hook_notify("Notification", {
            "session_id": "abcd1234-session", "cwd": "/tmp/proj",
            "message": "Claude needs your permission to use Bash",
        })
        assert any("permission to use Bash" in t for t in bridge.tg.texts)

    def test_a_short_body_gets_a_use_session_button(self, bridge, no_summarizer):
        db.hook_notify("Notification", {
            "session_id": "abcd1234-session", "cwd": "/tmp/proj", "message": "brief",
        })
        assert "bridge:use:abcd1234" in bridge.tg.callback_data()

    def test_the_project_name_and_short_id_head_the_card(self, bridge, no_summarizer):
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/myproject"})
        assert any("myproject" in t and "abcd1234" in t for t in bridge.tg.texts)

    def test_a_session_with_no_cwd_gets_no_session_buttons(self, bridge, no_summarizer):
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": ""})
        # Nothing to resume into, so offering "Use this session" would dead-end.
        assert bridge.tg.callback_data() == []
