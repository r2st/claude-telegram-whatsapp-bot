"""Behaviour tests for telechat_pkg.preferences (user style learning).

Organised by what the subsystem does: capture signals, age them, aggregate into
confident preferences, and inject a style hint. Time is injected via ``now`` so
decay is deterministic without sleeping.
"""
from __future__ import annotations

import uuid

import pytest

from telechat_pkg import preferences as prefs


def _uid() -> str:
    return "p-" + uuid.uuid4().hex[:12]


DAY = 86400.0


class TestRecordingSignals:
    def test_first_signal_creates_belief(self):
        uid = _uid()
        prefs.record_signal(uid, "length", "short", weight=2.0, now=1000.0)
        got = prefs.get_user_prefs(uid, now=1000.0)
        assert got["length"]["value"] == "short"

    def test_confirming_signal_raises_confidence(self):
        uid = _uid()
        prefs.record_signal(uid, "tone", "casual", now=1000.0)
        prefs.record_signal(uid, "tone", "casual", now=1000.0)
        c2 = prefs.get_user_prefs(uid, now=1000.0)["tone"]["confidence"]
        prefs.record_signal(uid, "tone", "casual", now=1000.0)
        c3 = prefs.get_user_prefs(uid, now=1000.0)["tone"]["confidence"]
        assert c3 > c2

    def test_contradicting_signal_lowers_then_flips(self):
        uid = _uid()
        # Build a confident "long" belief, then hammer it with "short".
        prefs.record_signal(uid, "length", "long", weight=3.0, now=1000.0)   # 0.75
        prefs.record_signal(uid, "length", "short", weight=1.0, now=1000.0)  # 0.5 left, still long
        assert prefs.get_user_prefs(uid, now=1000.0)["length"]["value"] == "long"
        prefs.record_signal(uid, "length", "short", weight=2.0, now=1000.0)  # overcome → flip
        prefs.record_signal(uid, "length", "short", weight=2.0, now=1000.0)  # rebuild short
        assert prefs.get_user_prefs(uid, now=1000.0)["length"]["value"] == "short"

    def test_unknown_dimension_ignored(self):
        uid = _uid()
        prefs.record_signal(uid, "verbosity", "loud", weight=5.0, now=1000.0)
        assert prefs.get_user_prefs(uid, now=1000.0) == {}

    def test_unknown_value_ignored(self):
        uid = _uid()
        prefs.record_signal(uid, "tone", "sarcastic", weight=5.0, now=1000.0)
        assert prefs.get_user_prefs(uid, now=1000.0) == {}


class TestConfidenceThreshold:
    def test_single_weak_signal_below_threshold_is_hidden(self):
        uid = _uid()
        prefs.record_signal(uid, "format", "markdown", weight=1.0, now=1000.0)  # 0.25 < 0.3
        assert "format" not in prefs.get_user_prefs(uid, now=1000.0)

    def test_two_signals_clear_threshold(self):
        uid = _uid()
        prefs.record_signal(uid, "format", "markdown", now=1000.0)
        prefs.record_signal(uid, "format", "markdown", now=1000.0)  # 0.5 >= 0.3
        assert prefs.get_user_prefs(uid, now=1000.0)["format"]["value"] == "markdown"


class TestDecay:
    def test_confidence_decays_over_time(self):
        uid = _uid()
        # Build confidence to ~0.75 so it stays above the floor after one half-life.
        prefs.record_signal(uid, "tone", "formal", weight=1.5, now=0.0)
        prefs.record_signal(uid, "tone", "formal", weight=1.5, now=0.0)  # ~0.75
        fresh = prefs.get_user_prefs(uid, now=0.0)["tone"]["confidence"]
        one_half_life = prefs.get_user_prefs(uid, now=30 * DAY)["tone"]["confidence"]
        assert one_half_life == pytest.approx(fresh / 2, abs=0.02)

    def test_stale_preference_drops_below_threshold(self):
        uid = _uid()
        prefs.record_signal(uid, "tone", "formal", weight=2.0, now=0.0)  # 0.5
        # After several half-lives the decayed confidence falls under the floor.
        assert prefs.get_user_prefs(uid, now=120 * DAY) == {}


class TestPromptHint:
    def test_empty_when_no_confident_prefs(self):
        assert prefs.prompt_hint(_uid(), now=1000.0) == ""

    def test_hint_mentions_each_confident_dimension(self):
        uid = _uid()
        for _ in range(2):
            prefs.record_signal(uid, "length", "short", now=1000.0)
            prefs.record_signal(uid, "format", "code-heavy", now=1000.0)
            prefs.record_signal(uid, "tone", "casual", now=1000.0)
        hint = prefs.prompt_hint(uid, now=1000.0)
        assert "brief" in hint and "code" in hint and "casual" in hint
        assert hint.startswith("User style preferences:")


class TestParsePreferCommand:
    def test_valid(self):
        assert prefs.parse_prefer_command("length short") == ("length", "short")

    def test_with_command_prefix(self):
        assert prefs.parse_prefer_command("/prefer tone formal") == ("tone", "formal")

    def test_invalid_value_rejected(self):
        assert prefs.parse_prefer_command("length enormous") is None

    def test_too_few_tokens(self):
        assert prefs.parse_prefer_command("length") is None

    def test_empty(self):
        assert prefs.parse_prefer_command("") is None


class TestTextSignalMining:
    def test_detects_shorter(self):
        assert ("length", "short") in prefs.infer_signals_from_text("please be shorter")

    def test_detects_code_request(self):
        assert ("format", "code-heavy") in prefs.infer_signals_from_text("show code please")

    def test_one_signal_per_dimension(self):
        # "shorter" and "more detail" both map to length — only the first wins.
        sigs = prefs.infer_signals_from_text("shorter but also more detail")
        lengths = [v for d, v in sigs if d == "length"]
        assert len(lengths) == 1

    def test_no_signal_in_neutral_text(self):
        assert prefs.infer_signals_from_text("thanks, that was great") == []

    def test_empty_text(self):
        assert prefs.infer_signals_from_text("") == []

    def test_record_text_feedback_persists_and_returns(self):
        uid = _uid()
        recorded = prefs.record_text_feedback(uid, "use code blocks and be more casual", weight=2.0)
        assert ("format", "code-heavy") in recorded
        assert ("tone", "casual") in recorded
        got = prefs.get_user_prefs(uid)
        assert got["format"]["value"] == "code-heavy"
