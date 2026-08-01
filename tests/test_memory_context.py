"""Tests for turning stored memories back into conversation context.

`memory.py` had been writing memories since the beginning and nothing read them
back — `recall` was wired to the `/recall` command and to the extractor's own
de-duplicator, and to nothing else. So the bot kept a careful record of your
preferences and used it exclusively to avoid storing that record twice, which
is what people were reporting as "it doesn't remember me".

What is asserted here, in rough order of how much it would hurt to get wrong:

  - **the block never breaks a reply** — a broken store, a raising `recall`, a
    missing identity all return "" rather than propagating. This is the whole
    contract: callers append the result to a system prompt with no try/except
    of their own;
  - **standing memories survive the budget** — "answer in metric" has to reach
    the model through a question that never says "units", and has to be the
    last thing dropped when the character budget bites;
  - **the block goes to the system prompt, not the conversation** — asserted at
    the claude_core seam, because injecting recalled facts as user turns is how
    a bot ends up insisting you told it something you didn't.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from telechat_pkg import memory_context as mc  # noqa: E402
from telechat_pkg.memory import MemoryStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "memctx.db"))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Each test starts from documented defaults, not the developer's shell."""
    for name in ("MEMORY_CONTEXT", "MEMORY_CONTEXT_LIMIT",
                 "MEMORY_CONTEXT_MAX_CHARS", "MEMORY_CONTEXT_MIN_IMPORTANCE"):
        monkeypatch.delenv(name, raising=False)


class Boom:
    """A store whose every read raises, standing in for a corrupt database."""

    def recall(self, *a, **k):
        raise RuntimeError("database is locked")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Selection
# ══════════════════════════════════════════════════════════════════════════════


class TestSelection:
    def test_a_memory_matching_the_message_is_selected(self, store):
        store.remember("tg", "u1", "Deploys with fly.io, never Vercel", importance=0.6)
        store.remember("tg", "u1", "Has a cat named Widget", importance=0.6)
        picked = mc.select_memories("tg", "u1", "how do I deploy this?", store=store)
        assert any("fly.io" in m.content for m in picked)

    def test_a_critical_memory_is_included_even_when_nothing_matches(self, store):
        # The reason relevance alone is not enough: a standing preference has no
        # keyword overlap with most of the questions it ought to shape.
        store.remember("tg", "u1", "Always answer in metric units", importance=0.95)
        picked = mc.select_memories("tg", "u1", "what time is the train", store=store)
        assert [m.content for m in picked] == ["Always answer in metric units"]

    def test_an_ordinary_memory_is_not_included_when_nothing_matches(self, store):
        store.remember("tg", "u1", "Has a cat named Widget", importance=0.6)
        assert mc.select_memories("tg", "u1", "kubernetes ingress", store=store) == []

    def test_standing_memories_come_first(self, store):
        store.remember("tg", "u1", "Always answer in metric units", importance=0.95)
        store.remember("tg", "u1", "Prefers kubernetes over nomad", importance=0.6)
        picked = mc.select_memories("tg", "u1", "kubernetes", store=store)
        assert "metric" in picked[0].content

    def test_a_memory_that_is_both_standing_and_relevant_appears_once(self, store):
        store.remember("tg", "u1", "Always deploy with fly.io", importance=0.95)
        picked = mc.select_memories("tg", "u1", "deploy fly.io", store=store)
        assert len(picked) == 1

    def test_selection_is_scoped_to_one_user(self, store):
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        assert mc.select_memories("tg", "u2", "deploy", store=store) == []

    def test_selection_is_scoped_to_one_platform(self, store):
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        assert mc.select_memories("slack", "u1", "deploy", store=store) == []

    def test_the_count_is_capped(self, store, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT_LIMIT", "3")
        for i in range(10):
            store.remember("tg", "u1", f"Critical preference number {i}", importance=0.95)
        assert len(mc.select_memories("tg", "u1", "preference", store=store)) == 3

    def test_a_zero_limit_selects_nothing(self, store, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT_LIMIT", "0")
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        assert mc.select_memories("tg", "u1", "deploy", store=store) == []

    def test_an_empty_message_still_gets_standing_memories(self, store):
        # Attachments and voice notes can reach the model with no text at all.
        store.remember("tg", "u1", "Always answer in metric units", importance=0.95)
        assert len(mc.select_memories("tg", "u1", "", store=store)) == 1


class TestSearchTerms:
    """A chat message is not a search query, and treating it as one matched nothing.

    `recall` joins its tokens with FTS5's implicit AND, so "how do I deploy
    this?" required a single memory to contain every one of those words. The
    fix is two-sided: `match_any` on the store, and dropping the words that
    make an OR search meaningless here.
    """

    def test_stopwords_are_dropped(self):
        assert mc._search_terms("how do I deploy this?") == "deploy"

    def test_punctuation_is_stripped_from_terms(self):
        assert mc._search_terms("deploy, please!") == "deploy please"

    def test_short_tokens_are_dropped(self):
        assert mc._search_terms("is it ok to go") == ""

    def test_repeated_words_are_searched_once(self):
        assert mc._search_terms("deploy deploy Deploy") == "deploy"

    def test_identifiers_keep_their_punctuation(self):
        # fly.io and snake_case names are exactly the terms worth matching.
        assert mc._search_terms("about fly.io and my_project") == "fly.io my_project"

    def test_the_term_count_is_capped(self):
        assert len(mc._search_terms(" ".join(f"word{i}" for i in range(40))).split()) == 12

    def test_a_message_of_only_stopwords_does_not_search_for_everything(self, store):
        # The fallback passes the raw text through, which finds nothing —
        # correct. Matching every memory would be the bad outcome.
        store.remember("tg", "u1", "Has a cat named Widget", importance=0.6)
        assert mc.select_memories("tg", "u1", "how do you do?", store=store) == []

    def test_a_natural_question_finds_the_relevant_memory(self, store):
        # The end-to-end regression: this returned nothing before match_any.
        store.remember("tg", "u1", "Deploys with fly.io, never Vercel", importance=0.6)
        picked = mc.select_memories("tg", "u1", "how do I deploy this?", store=store)
        assert any("fly.io" in m.content for m in picked)

    def test_only_one_word_of_the_question_needs_to_match(self, store):
        store.remember("tg", "u1", "Prefers pytest over unittest", importance=0.6)
        picked = mc.select_memories("tg", "u1", "should I write pytest cases here", store=store)
        assert any("pytest" in m.content for m in picked)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Rendering
# ══════════════════════════════════════════════════════════════════════════════


class TestRendering:
    def test_no_memories_render_to_nothing_at_all(self):
        # Not a header with an empty list — an empty string, so the system
        # prompt is untouched for someone who has never stored a memory.
        assert mc.render([]) == ""

    def test_each_memory_becomes_a_bullet(self, store):
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        block = mc.render(mc.select_memories("tg", "u1", "deploy", store=store))
        assert "- Deploys with fly.io" in block

    def test_the_header_subordinates_memories_to_the_live_conversation(self, store):
        # Without this framing a stale memory reads as an instruction, and the
        # model argues with someone who has since changed their setup.
        store.remember("tg", "u1", "Uses Postgres", importance=0.95)
        block = mc.render(mc.select_memories("tg", "u1", "postgres", store=store))
        assert "what they say now wins" in block

    def test_newlines_inside_a_memory_cannot_break_the_bullet_list(self, store):
        store.remember("tg", "u1", "line one\nline two", importance=0.95)
        block = mc.render(mc.select_memories("tg", "u1", "line", store=store))
        assert "- line one line two" in block

    def test_the_budget_drops_whole_memories_rather_than_halves(self):
        mems = [_mem("a" * 40), _mem("b" * 40), _mem("c" * 40)]
        block = mc.render(mems, max_chars=90)
        assert "a" * 40 in block
        assert "c" * 40 not in block
        assert "cccc" not in block          # no partial third bullet

    def test_a_single_memory_larger_than_the_budget_is_truncated_not_dropped(self):
        # Otherwise one runaway memory silently switches the feature off.
        block = mc.render([_mem("x" * 500)], max_chars=100)
        assert "…" in block
        assert "x" in block

    def test_a_zero_budget_renders_nothing(self):
        assert mc.render([_mem("anything")], max_chars=0) == ""

    def test_an_empty_memory_is_skipped(self):
        assert mc.render([_mem("   ")]) == ""


def _mem(content: str):
    from telechat_pkg.memory import Memory
    return Memory(id=content[:8], platform="tg", user_id="u1", content=content, importance=0.9)


# ══════════════════════════════════════════════════════════════════════════════
# 3. build() — the guarded path callers actually use
# ══════════════════════════════════════════════════════════════════════════════


class TestBuild:
    def test_it_returns_a_block_end_to_end(self, store):
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        assert "fly.io" in mc.build("tg", "u1", "deploy", store=store)

    def test_a_broken_store_costs_memory_not_the_reply(self, store):
        # The contract the callers depend on: they append this to a system
        # prompt with no error handling of their own.
        assert mc.build("tg", "u1", "deploy", store=Boom()) == ""

    def test_it_is_off_when_disabled(self, store, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT", "0")
        store.remember("tg", "u1", "Deploys with fly.io", importance=0.95)
        assert mc.build("tg", "u1", "deploy", store=store) == ""

    def test_an_anonymous_caller_gets_nothing(self, store):
        # A missing platform or user id would otherwise select across the whole
        # table, handing one person's memories to another.
        store.remember("", "", "Deploys with fly.io", importance=0.95)
        assert mc.build("", "", "deploy", store=store) == ""
        assert mc.build("tg", "", "deploy", store=store) == ""

    def test_a_user_with_no_memories_gets_nothing(self, store):
        assert mc.build("tg", "nobody", "deploy", store=store) == ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. Configuration
# ══════════════════════════════════════════════════════════════════════════════


class TestConfig:
    def test_memory_context_is_on_by_default(self):
        assert mc.context_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", ""])
    def test_it_can_be_switched_off(self, monkeypatch, value):
        monkeypatch.setenv("MEMORY_CONTEXT", value)
        assert mc.context_enabled() is False

    def test_a_nonsense_limit_falls_back_to_the_default(self, monkeypatch):
        # Misconfiguration should cost tuning, not memory.
        monkeypatch.setenv("MEMORY_CONTEXT_LIMIT", "lots")
        assert mc._limit() == 8

    def test_a_nonsense_budget_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT_MAX_CHARS", "big")
        assert mc._max_chars() == 1200

    def test_a_negative_limit_is_clamped_rather_than_inverted(self, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT_LIMIT", "-5")
        assert mc._limit() == 0

    def test_the_importance_threshold_is_configurable(self, monkeypatch, store):
        monkeypatch.setenv("MEMORY_CONTEXT_MIN_IMPORTANCE", "0.5")
        store.remember("tg", "u1", "Has a cat named Widget", importance=0.6)
        assert mc.select_memories("tg", "u1", "unrelated question", store=store)


class TestAppendToSystem:
    def test_a_block_is_appended_below_the_system_prompt(self):
        assert mc.append_to_system("Be terse.", "- likes fly.io") == (
            "Be terse.\n\n- likes fly.io")

    def test_an_empty_block_leaves_the_prompt_untouched(self):
        assert mc.append_to_system("Be terse.", "") == "Be terse."

    def test_an_empty_prompt_yields_the_block_alone(self):
        assert mc.append_to_system("", "- likes fly.io") == "- likes fly.io"


# ══════════════════════════════════════════════════════════════════════════════
# 5. The claude_core seam
# ══════════════════════════════════════════════════════════════════════════════


class TestClaudeCoreWiring:
    """Where the block is attached matters as much as what it contains."""

    def test_the_cli_gets_memories_as_a_system_prompt_not_as_conversation(self, monkeypatch):
        from telechat_pkg import claude_core as cc

        monkeypatch.setattr(cc.memory_context, "build",
                            lambda *a, **k: "- Deploys with fly.io")
        captured = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            raise OSError("stop here — the argv is the assertion")

        monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
        import asyncio
        asyncio.run(cc.ask_claude_async("deploy?", [], platform="tg", user_id="u1"))

        cmd = captured["cmd"]
        assert "--append-system-prompt" in cmd
        assert cmd[cmd.index("--append-system-prompt") + 1] == "- Deploys with fly.io"
        # The user's own message must remain exactly what they typed.
        assert cmd[cmd.index("-p") + 1] == "deploy?"

    def test_no_memories_means_no_extra_flag(self, monkeypatch):
        from telechat_pkg import claude_core as cc

        monkeypatch.setattr(cc.memory_context, "build", lambda *a, **k: "")
        captured = {}

        async def fake_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            raise OSError("stop here")

        monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
        import asyncio
        asyncio.run(cc.ask_claude_async("hi", [], platform="tg", user_id="u1"))
        assert "--append-system-prompt" not in captured["cmd"]
