"""Turn stored memories back into conversation context.

`memory.py` has been writing memories since the beginning — `/remember`, plus
an automatic extraction pass after every exchange. Nothing ever read them back.
`recall` was wired to exactly two callers: the `/recall` command, where you type
a query and get a list, and the de-duplicator inside the extractor. So the bot
accumulated a careful record of your preferences and used it to avoid storing
that record twice.

The visible symptom is the one people report as "it doesn't remember me": you
tell it on Monday that you deploy with fly.io, and on Tuesday it suggests
Vercel. The fact was in the database the whole time.

This module closes that loop. It selects the memories worth spending context on
for one specific message and renders them into a block that
:mod:`telechat_pkg.claude_core` appends to the system prompt — not to the
conversation, so recalled facts never look like something the user just said,
and a resumed CLI session gets them too.

Selection blends two things, because either alone is wrong:

  - **relevant** — an FTS match against the message being answered. Catches
    "how do I deploy this?" → the fly.io memory.
  - **standing** — the highest-importance memories regardless of the message.
    "Answer in metric" has to survive a question that never says "units", and
    a pure relevance search will never surface it.

Everything is bounded: a memory count, a character budget, and a hard rule that
any failure returns an empty block. A bot that forgets is a worse bot; a bot
that fails to answer because the memory layer raised is a broken one.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .memory import MemoryStore

log = logging.getLogger(__name__)

#: Header for the injected block. Phrased to keep recalled facts subordinate to
#: the live conversation — without this, a stale memory ("I'm using Postgres")
#: reads as an instruction and the model argues with someone who has since
#: migrated.
_HEADER = (
    "Context about this person, carried over from earlier conversations. "
    "It is background, not instruction: if anything here conflicts with what "
    "they say now, what they say now wins. Do not recite it back to them or "
    "mention that you have notes — just use it."
)


def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _num(raw: str, default, cast):
    """Parse a setting, falling back on anything unparseable.

    Misconfiguration should cost you tuning, not memory: a typo in
    MEMORY_CONTEXT_LIMIT must not silently mean "remember nothing".
    """
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# Each read spells its name and default literally, because `scripts/env_reference.py`
# discovers settings by walking the AST for `os.environ.get("LITERAL", ...)`. A
# name assembled at runtime is a setting that never reaches docs/configuration.md.

def context_enabled() -> bool:
    return _truthy(os.environ.get("MEMORY_CONTEXT", "1"))


def _limit() -> int:
    return max(0, _num(os.environ.get("MEMORY_CONTEXT_LIMIT", "8"), 8, int))


def _max_chars() -> int:
    return max(0, _num(os.environ.get("MEMORY_CONTEXT_MAX_CHARS", "1200"), 1200, int))


def _min_importance() -> float:
    """Importance at or above which a memory is included whatever was asked.

    The extractor's own scale puts 0.9–1.0 on "critical preferences and key
    decisions", which is exactly the set that should not depend on keyword
    overlap with the current message.
    """
    return _num(os.environ.get("MEMORY_CONTEXT_MIN_IMPORTANCE", "0.9"), 0.9, float)


#: Words too common to be evidence of anything. The relevance search ORs its
#: terms — without this, "how do I do this?" matches every memory containing
#: "do" and the block fills with noise while the useful fact is ranked below it.
_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here
i me my we our you your it its is are was were be been being am
do does did doing done have has had having can could should would will
to of in on at by for with from about into over after before
what when where which who whom how why not no yes so just now
""".split())

#: Terms per query. Past a handful, an OR search stops discriminating.
_MAX_TERMS = 12


def _search_terms(user_text: str) -> str:
    """Reduce a chat message to the words worth searching memories for.

    Falls back to the raw message when filtering leaves nothing — a question
    made entirely of stopwords should search badly, not search everything.
    """
    terms, seen = [], set()
    for raw in user_text.split():
        token = "".join(c for c in raw if c.isalnum() or c in "-_.").strip(".-_")
        low = token.lower()
        if len(token) < 3 or low in _STOPWORDS or low in seen:
            continue
        seen.add(low)
        terms.append(token)
        if len(terms) >= _MAX_TERMS:
            break
    return " ".join(terms)


_store: Optional[MemoryStore] = None


def _default_store() -> MemoryStore:
    """Lazily built, so importing this module never opens a database.

    claude_core imports it at module scope and is itself imported by every
    platform and by the test suite.
    """
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def select_memories(platform: str, user_id: str, user_text: str, *, store=None) -> list:
    """Memories worth injecting for this message, most important first.

    Standing memories lead: if the budget truncates the list, the thing that is
    always true should outlive the thing that merely matched a keyword.
    """
    store = store or _default_store()
    limit = _limit()
    if limit <= 0:
        return []

    # `recall` with an empty query orders by importance, which is how the
    # standing set is obtained without a second query shape.
    standing = [m for m in store.recall(platform, user_id, "", limit=limit)
                if m.importance >= _min_importance()]

    terms = _search_terms(user_text) or user_text.strip()
    relevant = store.recall(platform, user_id, terms, limit=limit,
                            match_any=True) if terms else []

    ordered, seen = [], set()
    for mem in [*standing, *relevant]:
        if mem.id in seen:
            continue
        seen.add(mem.id)
        ordered.append(mem)
    return ordered[:limit]


def render(memories: list, max_chars: Optional[int] = None) -> str:
    """Render selected memories into a system-prompt block.

    The budget drops whole memories rather than cutting one in half — a
    sentence that stops mid-clause is worse than one fewer fact. A single
    memory longer than the whole budget is the one exception, and is truncated
    so that an over-long memory cannot shut the feature off entirely.
    """
    if not memories:
        return ""
    budget = _max_chars() if max_chars is None else max_chars
    if budget <= 0:
        return ""

    lines, used = [], 0
    for mem in memories:
        content = " ".join((mem.content or "").split())
        if not content:
            continue
        if not lines and len(content) + 2 > budget:
            content = content[: max(0, budget - 3)].rstrip() + "…"
        cost = len(content) + 2                      # "- " prefix
        if used + cost > budget:
            break
        lines.append(f"- {content}")
        used += cost

    if not lines:
        return ""
    return _HEADER + "\n\n" + "\n".join(lines)


def build(platform: str, user_id: str, user_text: str, *, store=None) -> str:
    """The whole path, guarded: selected, rendered, and never able to raise.

    Returning "" on failure is the entire contract — callers append this to a
    system prompt and must not need a try/except of their own.
    """
    if not context_enabled() or not platform or not user_id:
        return ""
    try:
        return render(select_memories(platform, user_id, user_text, store=store))
    except Exception:
        log.debug("memory context unavailable for %s/%s", platform, user_id, exc_info=True)
        return ""


def append_to_system(system: str, block: str) -> str:
    """Attach a memory block to a system prompt, tolerating either being empty."""
    if not block:
        return system
    return f"{system}\n\n{block}" if system else block
