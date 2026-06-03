"""
Context compaction — summarize long conversations to stay within token limits.

When conversation history grows too long, older messages are compressed into
a concise summary while keeping recent messages verbatim. This preserves
context without hitting token limits.

"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Rough token estimation: ~4 chars per token for English text
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 100_000
DEFAULT_KEEP_RECENT = 10


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_history_tokens(history: list[dict]) -> int:
    """Estimate total tokens across all messages in history."""
    return sum(estimate_tokens(m.get("content", "")) for m in history)


def needs_compaction(history: list[dict], max_tokens: int = DEFAULT_MAX_TOKENS) -> bool:
    """Check if conversation history needs compaction."""
    if len(history) <= DEFAULT_KEEP_RECENT:
        return False
    return estimate_history_tokens(history) > max_tokens


@dataclass
class CompactionResult:
    history: list[dict]
    tokens_before: int
    tokens_after: int
    messages_before: int
    messages_after: int
    messages_compacted: int
    summary_tokens: int


def _extractive_summary(messages: list[dict], max_sentences: int = 30) -> str:
    """Simple extractive summary: keep first sentence of each message."""
    parts: list[str] = []
    for m in messages:
        content = m.get("content", "").strip()
        if not content:
            continue
        role = m.get("role", "user")
        # Take first sentence or first 200 chars
        first_sentence = content.split(". ")[0].split(".\n")[0]
        if len(first_sentence) > 200:
            first_sentence = first_sentence[:200] + "…"
        parts.append(f"[{role}] {first_sentence}")
        if len(parts) >= max_sentences:
            break
    return "\n".join(parts)


def build_summary_prompt(messages: list[dict]) -> str:
    """Build a prompt asking Claude to summarize conversation history."""
    formatted = []
    for m in messages:
        role = m.get("role", "user").capitalize()
        content = m.get("content", "")
        # Truncate very long messages for the summary prompt
        if len(content) > 1000:
            content = content[:1000] + "…[truncated]"
        formatted.append(f"**{role}:** {content}")

    conversation_text = "\n\n".join(formatted)

    return f"""Summarize this conversation concisely. Capture:
1. Key topics discussed
2. Important facts, decisions, and preferences shared
3. Any pending tasks or follow-ups
4. The user's current goal or question

Keep it under 500 words. Use bullet points for clarity.

---
{conversation_text}
---

Summary:"""


def format_summary(summary: str, messages_compacted: int) -> dict:
    """Format a summary as a system-style message."""
    return {
        "role": "system",
        "content": (
            f"[Conversation summary — {messages_compacted} earlier messages compacted]\n\n"
            f"{summary}"
        ),
    }


async def compact_history(
    history: list[dict],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    claude_fn: Optional[Callable] = None,
) -> CompactionResult:
    """Compact conversation history by summarizing older messages.

    Args:
        history: Full conversation history (list of role/content dicts).
        max_tokens: Target maximum token count after compaction.
        keep_recent: Number of recent messages to keep verbatim.
        claude_fn: Optional async callable(prompt) -> str for AI-powered summary.
                   If None, uses simple extractive summary.

    Returns:
        CompactionResult with the compacted history and stats.
    """
    tokens_before = estimate_history_tokens(history)
    messages_before = len(history)

    if not needs_compaction(history, max_tokens):
        return CompactionResult(
            history=history,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            messages_before=messages_before,
            messages_after=messages_before,
            messages_compacted=0,
            summary_tokens=0,
        )

    # Split into old (to summarize) and recent (to keep)
    split_point = max(0, len(history) - keep_recent)
    old_messages = history[:split_point]
    recent_messages = history[split_point:]

    log.info(
        "Compacting %d messages (keeping %d recent, summarizing %d older)",
        len(history), len(recent_messages), len(old_messages),
    )

    # Generate summary
    if claude_fn and old_messages:
        try:
            prompt = build_summary_prompt(old_messages)
            summary = await claude_fn(prompt)
            if not summary or len(summary.strip()) < 20:
                log.warning("Claude summary too short, falling back to extractive")
                summary = _extractive_summary(old_messages)
        except Exception as e:
            log.warning("Claude summary failed (%s), falling back to extractive", e)
            summary = _extractive_summary(old_messages)
    else:
        summary = _extractive_summary(old_messages)

    # Build compacted history
    summary_msg = format_summary(summary, len(old_messages))
    compacted = [summary_msg] + recent_messages

    tokens_after = estimate_history_tokens(compacted)
    summary_tokens = estimate_tokens(summary)

    log.info(
        "Compaction complete: %d→%d messages, %d→%d estimated tokens",
        messages_before, len(compacted), tokens_before, tokens_after,
    )

    return CompactionResult(
        history=compacted,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        messages_before=messages_before,
        messages_after=len(compacted),
        messages_compacted=len(old_messages),
        summary_tokens=summary_tokens,
    )


def compact_history_sync(
    history: list[dict],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> CompactionResult:
    """Synchronous version of compact_history (extractive summary only)."""
    tokens_before = estimate_history_tokens(history)
    messages_before = len(history)

    if not needs_compaction(history, max_tokens):
        return CompactionResult(
            history=history,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            messages_before=messages_before,
            messages_after=messages_before,
            messages_compacted=0,
            summary_tokens=0,
        )

    split_point = max(0, len(history) - keep_recent)
    old_messages = history[:split_point]
    recent_messages = history[split_point:]

    summary = _extractive_summary(old_messages)
    summary_msg = format_summary(summary, len(old_messages))
    compacted = [summary_msg] + recent_messages

    tokens_after = estimate_history_tokens(compacted)

    return CompactionResult(
        history=compacted,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        messages_before=messages_before,
        messages_after=len(compacted),
        messages_compacted=len(old_messages),
        summary_tokens=estimate_tokens(summary),
    )
