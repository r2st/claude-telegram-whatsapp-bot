"""
Behavior-organized tests for telechat_pkg.context_compaction.

Context compaction is the seam where token budgets meet conversation history.
These tests verify the observable invariants:

  - Recent messages within the keep_recent window are NEVER compacted.
  - With no LLM (claude_fn=None) the extractive fallback still produces a
    real, non-empty summary.
  - Compaction fires only when history exceeds the token budget, not below.
  - The CompactionResult shape is consistent on both the compacted and
    no-op paths, and through the claude_fn / fallback branches.

Run:
    pytest tests/test_context_compaction.py -v
"""

import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg.context_compaction import (
    CHARS_PER_TOKEN,
    DEFAULT_KEEP_RECENT,
    CompactionResult,
    build_summary_prompt,
    compact_history,
    compact_history_sync,
    estimate_history_tokens,
    estimate_tokens,
    format_summary,
    needs_compaction,
    _extractive_summary,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _make_history(n: int, *, chars: int = 100, prefix: str = "msg") -> list[dict]:
    """Build n messages, each `chars` characters of content."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        body = f"{prefix}-{i} " + ("x" * chars)
        out.append(_msg(role, body[:chars] if chars else body))
    return out


def _big_history(n: int = 40, *, chars: int = 4000) -> list[dict]:
    """Build a history that is guaranteed to exceed a small token budget.

    Each message ~chars chars => ~chars/4 tokens; n of them well over a
    small max_tokens value.
    """
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(_msg(role, f"Message number {i}. " + ("word " * (chars // 5))))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 1. keep_recent invariant — recent messages are never compacted
# ──────────────────────────────────────────────────────────────────────────────


class TestKeepRecentInvariant:
    @pytest.mark.asyncio
    async def test_recent_messages_preserved_verbatim(self):
        history = _big_history(40)
        keep = 5
        result = await compact_history(history, max_tokens=10, keep_recent=keep)
        # The tail of the original history must appear verbatim at the tail
        # of the compacted history.
        assert result.history[-keep:] == history[-keep:]

    @pytest.mark.asyncio
    async def test_summary_is_prepended_not_replacing_recent(self):
        history = _big_history(40)
        keep = 5
        result = await compact_history(history, max_tokens=10, keep_recent=keep)
        # First message is the summary; everything after is the recent window.
        assert result.history[0]["role"] == "system"
        assert "summary" in result.history[0]["content"].lower()
        assert len(result.history) == keep + 1

    @pytest.mark.asyncio
    async def test_messages_compacted_equals_old_count(self):
        history = _big_history(40)
        keep = 8
        result = await compact_history(history, max_tokens=10, keep_recent=keep)
        assert result.messages_compacted == len(history) - keep

    @pytest.mark.asyncio
    async def test_keep_recent_larger_than_history_compacts_nothing_old(self):
        # keep_recent exceeds compactable messages: split_point clamps to 0,
        # so no old messages exist and the summary covers zero messages.
        history = _big_history(15)
        result = await compact_history(history, max_tokens=10, keep_recent=100)
        assert result.messages_compacted == 0
        # Recent window is the whole history, prefixed by an (empty) summary.
        assert result.history[1:] == history

    def test_sync_recent_messages_preserved(self):
        history = _big_history(30)
        keep = 6
        result = compact_history_sync(history, max_tokens=10, keep_recent=keep)
        assert result.history[-keep:] == history[-keep:]


# ──────────────────────────────────────────────────────────────────────────────
# 2. Extractive fallback — real summary when no LLM is available
# ──────────────────────────────────────────────────────────────────────────────


class TestExtractiveFallback:
    @pytest.mark.asyncio
    async def test_claude_fn_none_produces_nonempty_summary(self):
        history = _big_history(30)
        result = await compact_history(history, max_tokens=10, keep_recent=5, claude_fn=None)
        summary_block = result.history[0]["content"]
        # The summary message contains the header plus the extractive body;
        # the body must carry real content from the compacted messages.
        assert "Message number 0" in summary_block
        assert result.summary_tokens > 0

    def test_extractive_summary_keeps_first_sentence_per_message(self):
        messages = [
            _msg("user", "First sentence. Second sentence."),
            _msg("assistant", "Reply one. Reply two."),
        ]
        summary = _extractive_summary(messages)
        assert "[user] First sentence" in summary
        assert "[assistant] Reply one" in summary
        # Trailing sentences are dropped.
        assert "Second sentence" not in summary
        assert "Reply two" not in summary

    def test_extractive_summary_skips_empty_messages(self):
        messages = [
            _msg("user", "   "),
            _msg("user", ""),
            _msg("assistant", "Actual content here."),
        ]
        summary = _extractive_summary(messages)
        # Split only happens on ". " / ".\n"; a lone trailing period stays.
        assert summary == "[assistant] Actual content here."

    def test_extractive_summary_truncates_long_first_sentence(self):
        long = "a" * 500
        summary = _extractive_summary([_msg("user", long)])
        assert summary.endswith("…")
        # 200 chars + ellipsis + "[user] " prefix.
        assert "a" * 200 in summary
        assert "a" * 201 not in summary

    def test_extractive_summary_respects_max_sentences(self):
        messages = [_msg("user", f"Line {i}.") for i in range(50)]
        summary = _extractive_summary(messages, max_sentences=5)
        assert len(summary.splitlines()) == 5

    def test_extractive_summary_defaults_missing_role_to_user(self):
        summary = _extractive_summary([{"content": "No role provided."}])
        assert summary == "[user] No role provided."

    def test_extractive_summary_empty_input(self):
        assert _extractive_summary([]) == ""

    def test_sync_uses_extractive_summary(self):
        history = _big_history(30)
        result = compact_history_sync(history, max_tokens=10, keep_recent=5)
        assert "Message number 0" in result.history[0]["content"]
        assert result.summary_tokens > 0


# ──────────────────────────────────────────────────────────────────────────────
# 3. Budget threshold — compaction fires above max_tokens, not below
# ──────────────────────────────────────────────────────────────────────────────


class TestBudgetThreshold:
    def test_short_history_never_needs_compaction(self):
        # At or below keep-recent count, never compacts regardless of size.
        history = _make_history(DEFAULT_KEEP_RECENT, chars=100_000)
        assert needs_compaction(history) is False

    def test_below_budget_does_not_compact(self):
        # Many messages, but tiny content => total tokens under budget.
        history = _make_history(20, chars=4)
        assert needs_compaction(history, max_tokens=1_000_000) is False

    def test_above_budget_needs_compaction(self):
        history = _big_history(40)
        assert needs_compaction(history, max_tokens=10) is True

    def test_threshold_is_strict_greater_than(self):
        # Construct history whose estimated tokens equal exactly the budget.
        history = _make_history(DEFAULT_KEEP_RECENT + 1, chars=CHARS_PER_TOKEN)
        total = estimate_history_tokens(history)
        # Equal to budget => not over => no compaction (strict >).
        assert needs_compaction(history, max_tokens=total) is False
        # One token below budget => over by one => compaction.
        assert needs_compaction(history, max_tokens=total - 1) is True

    @pytest.mark.asyncio
    async def test_no_compaction_returns_history_unchanged(self):
        history = _make_history(20, chars=4)
        result = await compact_history(history, max_tokens=1_000_000)
        assert result.history is history
        assert result.messages_compacted == 0
        assert result.summary_tokens == 0
        assert result.tokens_after == result.tokens_before
        assert result.messages_after == result.messages_before

    def test_sync_no_compaction_returns_history_unchanged(self):
        history = _make_history(20, chars=4)
        result = compact_history_sync(history, max_tokens=1_000_000)
        assert result.history is history
        assert result.messages_compacted == 0
        assert result.summary_tokens == 0

    def test_estimate_tokens_floor_is_one(self):
        assert estimate_tokens("") == 1
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 40) == 10

    def test_estimate_history_tokens_sums_messages(self):
        history = [_msg("user", "a" * 40), _msg("assistant", "b" * 80)]
        assert estimate_history_tokens(history) == 10 + 20


# ──────────────────────────────────────────────────────────────────────────────
# 4. claude_fn path — AI summary used, with fallback on failure / short output
# ──────────────────────────────────────────────────────────────────────────────


class TestClaudeFnPath:
    @pytest.mark.asyncio
    async def test_claude_fn_summary_used_when_valid(self):
        captured = {}

        async def fake_claude(prompt):
            captured["prompt"] = prompt
            return "This is a sufficiently long AI-generated summary of the chat."

        history = _big_history(40)
        result = await compact_history(history, max_tokens=10, keep_recent=5, claude_fn=fake_claude)
        assert "AI-generated summary" in result.history[0]["content"]
        # The prompt fed to claude_fn was the structured summary prompt.
        assert "Summarize this conversation" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_claude_fn_short_output_falls_back_to_extractive(self):
        async def short_claude(prompt):
            return "too short"  # < 20 chars after strip

        history = _big_history(40)
        result = await compact_history(history, max_tokens=10, keep_recent=5, claude_fn=short_claude)
        # Falls back to extractive => contains content drawn from messages.
        assert "Message number 0" in result.history[0]["content"]
        assert "too short" not in result.history[0]["content"]

    @pytest.mark.asyncio
    async def test_claude_fn_empty_output_falls_back(self):
        async def empty_claude(prompt):
            return ""

        history = _big_history(40)
        result = await compact_history(history, max_tokens=10, keep_recent=5, claude_fn=empty_claude)
        assert "Message number 0" in result.history[0]["content"]

    @pytest.mark.asyncio
    async def test_claude_fn_exception_falls_back_to_extractive(self):
        async def boom_claude(prompt):
            raise RuntimeError("API down")

        history = _big_history(40)
        result = await compact_history(history, max_tokens=10, keep_recent=5, claude_fn=boom_claude)
        # Exception is swallowed; extractive summary used instead.
        assert "Message number 0" in result.history[0]["content"]
        assert result.summary_tokens > 0

    @pytest.mark.asyncio
    async def test_claude_fn_not_called_when_no_old_messages(self):
        # keep_recent >= history => old_messages empty => claude_fn skipped,
        # extractive branch taken (which produces an empty summary).
        called = {"n": 0}

        async def fake_claude(prompt):
            called["n"] += 1
            return "should not be used"

        history = _big_history(15)
        result = await compact_history(history, max_tokens=10, keep_recent=100, claude_fn=fake_claude)
        assert called["n"] == 0
        assert result.messages_compacted == 0


# ──────────────────────────────────────────────────────────────────────────────
# 5. Result shape and helpers
# ──────────────────────────────────────────────────────────────────────────────


class TestResultShape:
    @pytest.mark.asyncio
    async def test_compacted_result_fields(self):
        history = _big_history(40)
        result = await compact_history(history, max_tokens=10, keep_recent=5)
        assert isinstance(result, CompactionResult)
        assert isinstance(result.history, list)
        assert all(isinstance(m, dict) for m in result.history)
        assert result.summary_tokens > 0
        assert result.messages_before == 40
        assert result.messages_after == len(result.history)
        assert result.tokens_after < result.tokens_before

    def test_sync_compacted_result_fields(self):
        history = _big_history(40)
        result = compact_history_sync(history, max_tokens=10, keep_recent=5)
        assert isinstance(result, CompactionResult)
        assert isinstance(result.history, list)
        assert result.summary_tokens > 0
        assert result.tokens_after < result.tokens_before
        assert result.messages_after == len(result.history)

    def test_format_summary_shape(self):
        msg = format_summary("a recap", 7)
        assert msg["role"] == "system"
        assert "7 earlier messages compacted" in msg["content"]
        assert "a recap" in msg["content"]

    def test_build_summary_prompt_includes_roles_and_content(self):
        messages = [_msg("user", "hello there"), _msg("assistant", "hi back")]
        prompt = build_summary_prompt(messages)
        assert "**User:** hello there" in prompt
        assert "**Assistant:** hi back" in prompt
        assert "Summarize this conversation" in prompt

    def test_build_summary_prompt_truncates_long_messages(self):
        long = "z" * 2000
        prompt = build_summary_prompt([_msg("user", long)])
        assert "…[truncated]" in prompt
        assert "z" * 1000 in prompt
        assert "z" * 1001 not in prompt

    def test_build_summary_prompt_defaults_missing_role(self):
        prompt = build_summary_prompt([{"content": "no role"}])
        assert "**User:** no role" in prompt
