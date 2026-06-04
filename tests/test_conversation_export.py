"""
Behavior tests for conversation export (telechat_pkg.conversation_export).

Covers: text/markdown/html/json exporters (with and without timestamps),
HTML escaping + role classes, timestamp formatting (incl. invalid), the
EXPORTERS dispatch table + aliases, and the unknown-format error.

Run:
    pytest tests/test_conversation_export.py -v
"""

import json
import os

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import conversation_export as ce
from telechat_pkg.conversation_export import (
    export_conversation,
    export_html,
    export_json,
    export_markdown,
    export_text,
)


MESSAGES = [
    {"role": "user", "content": "Hello there", "timestamp": 1_700_000_000},
    {"role": "assistant", "content": "Hi! How can I help?", "timestamp": 1_700_000_060},
    {"role": "system", "content": "session started", "timestamp": 0},
]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Timestamp helper
# ══════════════════════════════════════════════════════════════════════════════


class TestTimestamp:
    def test_valid_timestamp(self):
        s = ce._ts_to_str(1_700_000_000)
        assert "20" in s and ":" in s

    def test_invalid_timestamp_returns_unknown(self):
        # NaN raises ValueError inside datetime.fromtimestamp → "unknown"
        assert ce._ts_to_str(float("nan")) == "unknown"

    def test_huge_timestamp_returns_unknown(self):
        # A timestamp too large for the platform C time_t raises OverflowError
        # (not ValueError) on macOS; ticket 0019 makes it degrade to "unknown"
        # instead of propagating.
        assert ce._ts_to_str(10 ** 30) == "unknown"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Text export
# ══════════════════════════════════════════════════════════════════════════════


class TestTextExport:
    def test_basic(self):
        r = export_text(MESSAGES, title="Chat")
        assert r.format == "text"
        assert r.message_count == 3
        assert "=== Chat ===" in r.content
        assert "Hello there" in r.content
        assert r.filename.endswith(".txt")

    def test_with_timestamps(self):
        r = export_text(MESSAGES, include_timestamps=True)
        assert "User:" in r.content
        # the timestamped form has bracketed prefix
        assert "[" in r.content

    def test_without_timestamps(self):
        r = export_text(MESSAGES, include_timestamps=False)
        assert "User:" in r.content
        assert "[2023" not in r.content

    def test_zero_timestamp_omits_bracket(self):
        # system message has timestamp 0 → no bracket even with include_timestamps
        r = export_text(MESSAGES, include_timestamps=True)
        assert "System:" in r.content

    def test_defaults_role_when_missing(self):
        r = export_text([{"content": "no role"}])
        assert "User:" in r.content


# ══════════════════════════════════════════════════════════════════════════════
# 3. Markdown export
# ══════════════════════════════════════════════════════════════════════════════


class TestMarkdownExport:
    def test_basic(self):
        r = export_markdown(MESSAGES, title="MD")
        assert r.format == "markdown"
        assert r.filename.endswith(".md")
        assert "# MD" in r.content
        assert "### User" in r.content

    def test_with_timestamps(self):
        r = export_markdown(MESSAGES, include_timestamps=True)
        assert "### User (" in r.content

    def test_without_timestamps(self):
        r = export_markdown(MESSAGES, include_timestamps=False)
        assert "### User" in r.content
        assert "### User (" not in r.content


# ══════════════════════════════════════════════════════════════════════════════
# 4. HTML export
# ══════════════════════════════════════════════════════════════════════════════


class TestHtmlExport:
    def test_basic_structure(self):
        r = export_html(MESSAGES, title="Web")
        assert r.format == "html"
        assert r.filename.endswith(".html")
        assert "<!DOCTYPE html>" in r.content
        assert "<title>Web</title>" in r.content

    def test_role_classes(self):
        r = export_html(MESSAGES)
        assert 'class="message user"' in r.content
        assert 'class="message assistant"' in r.content
        assert 'class="message system"' in r.content

    def test_html_escaping(self):
        r = export_html([{"role": "user", "content": "<script>alert(1)</script>"}])
        assert "&lt;script&gt;" in r.content
        assert "<script>alert(1)</script>" not in r.content

    def test_newlines_to_br(self):
        r = export_html([{"role": "user", "content": "line1\nline2"}])
        assert "line1<br>line2" in r.content

    def test_timestamp_div_present(self):
        r = export_html(MESSAGES, include_timestamps=True)
        assert 'class="timestamp"' in r.content

    def test_timestamp_div_absent_when_disabled(self):
        r = export_html(MESSAGES, include_timestamps=False)
        assert 'class="timestamp"' not in r.content


# ══════════════════════════════════════════════════════════════════════════════
# 5. JSON export
# ══════════════════════════════════════════════════════════════════════════════


class TestJsonExport:
    def test_basic(self):
        r = export_json(MESSAGES, title="J")
        assert r.format == "json"
        assert r.filename.endswith(".json")
        data = json.loads(r.content)
        assert data["title"] == "J"
        assert data["message_count"] == 3
        assert len(data["messages"]) == 3
        assert data["messages"][0]["role"] == "user"

    def test_preserves_unicode(self):
        r = export_json([{"role": "user", "content": "café résumé"}])
        assert "café" in r.content


# ══════════════════════════════════════════════════════════════════════════════
# 6. Dispatch
# ══════════════════════════════════════════════════════════════════════════════


class TestDispatch:
    @pytest.mark.parametrize("fmt,expected", [
        ("text", "text"),
        ("txt", "text"),
        ("markdown", "markdown"),
        ("md", "markdown"),
        ("html", "html"),
        ("json", "json"),
    ])
    def test_format_aliases(self, fmt, expected):
        r = export_conversation(MESSAGES, fmt=fmt)
        assert r.format == expected

    def test_case_insensitive(self):
        r = export_conversation(MESSAGES, fmt="JSON")
        assert r.format == "json"

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError) as exc:
            export_conversation(MESSAGES, fmt="pdf")
        assert "Unknown format" in str(exc.value)

    def test_kwargs_passed_through(self):
        r = export_conversation(MESSAGES, fmt="text", title="Passed")
        assert "=== Passed ===" in r.content

    def test_empty_messages(self):
        r = export_conversation([], fmt="json")
        assert r.message_count == 0
