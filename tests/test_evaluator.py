"""Behaviour tests for telechat_pkg.evaluator (LLM-as-judge).

Organised by what the judge subsystem does: decides what to sample, parses the
judge's output robustly, persists per-dimension scores, and surfaces averages
for /quality. The judge LLM call is always injected so no network is touched.
"""
from __future__ import annotations

import random
import uuid

import pytest

from telechat_pkg import evaluator, feedback
from telechat_pkg import store


def _uid() -> str:
    """Unique user id per test so rows never bleed across the shared test DB."""
    return "u-" + uuid.uuid4().hex[:12]


def _judge_fn(payload):
    """Return a claude_fn that always answers with ``payload`` (str or None)."""
    return lambda prompt, response: payload


class TestSamplingDecision:
    def test_rate_zero_never_samples(self):
        assert evaluator.should_sample(0.0) is False

    def test_rate_one_always_samples(self):
        assert evaluator.should_sample(1.0) is True

    def test_rate_fraction_uses_rng(self):
        # Seeded RNG makes the probabilistic decision deterministic.
        lo = random.Random(1)
        # random() for seed 1 first draw is ~0.134 — below 0.5 → sampled.
        assert evaluator.should_sample(0.5, rng=lo) is True

    def test_rate_fraction_rejects_high_draw(self):
        class _HighRng:
            def random(self):
                return 0.99
        assert evaluator.should_sample(0.5, rng=_HighRng()) is False

    def test_default_rate_used_when_none(self, monkeypatch):
        monkeypatch.setattr(evaluator, "JUDGE_SAMPLE_RATE", 1.0)
        assert evaluator.should_sample(None) is True


class TestParsingJudgeOutput:
    def test_parses_bare_json(self):
        s = evaluator._parse_judge_output(
            '{"helpfulness": 4, "accuracy": 5, "tone": 3, "justification": "solid"}'
        )
        assert (s.helpfulness, s.accuracy, s.tone) == (4, 5, 3)
        assert s.justification == "solid"

    def test_parses_json_wrapped_in_prose_and_fence(self):
        text = 'Sure!\n```json\n{"helpfulness": 5, "accuracy": 4, "tone": 5}\n```\n'
        s = evaluator._parse_judge_output(text)
        assert s.helpfulness == 5 and s.tone == 5

    def test_clamps_out_of_range_scores(self):
        s = evaluator._parse_judge_output(
            '{"helpfulness": 9, "accuracy": 0, "tone": -2}'
        )
        assert s.helpfulness == 5  # clamped down
        assert s.accuracy == 1     # clamped up
        assert s.tone == 1

    def test_non_numeric_field_falls_back_to_neutral(self):
        s = evaluator._parse_judge_output(
            '{"helpfulness": "great", "accuracy": 4, "tone": 4}'
        )
        assert s.helpfulness == 3  # neutral fallback, not a crash

    def test_garbage_returns_none(self):
        assert evaluator._parse_judge_output("not json at all") is None

    def test_malformed_json_in_braces_returns_none(self):
        # Reaches json.loads and trips the decode-error guard.
        assert evaluator._parse_judge_output("{not: valid, json}") is None

    def test_empty_returns_none(self):
        assert evaluator._parse_judge_output("") is None

    def test_json_without_any_dimension_returns_none(self):
        assert evaluator._parse_judge_output('{"foo": 1}') is None


class TestJudgeScore:
    def test_composite_is_normalised_mean(self):
        # all 5s → 1.0; all 1s → 0.2
        assert evaluator.JudgeScore(5, 5, 5).composite == 1.0
        assert evaluator.JudgeScore(1, 1, 1).composite == 0.2

    def test_dimension_scores_include_composite(self):
        dims = evaluator.JudgeScore(5, 5, 5).as_dimension_scores()
        assert dims["judge_helpfulness"] == 1.0
        assert "judge_composite" in dims


class TestJudgeResponse:
    def test_returns_score_from_injected_judge(self):
        score = evaluator.judge_response(
            "what is 2+2?", "4",
            claude_fn=_judge_fn('{"helpfulness":5,"accuracy":5,"tone":4}'),
        )
        assert score.accuracy == 5

    def test_returns_none_when_judge_unavailable(self):
        # claude_fn returning None models "no API key / call failed".
        assert evaluator.judge_response("q", "a", claude_fn=_judge_fn(None)) is None

    def test_returns_none_on_unparseable_judge_output(self):
        assert evaluator.judge_response("q", "a", claude_fn=_judge_fn("???")) is None

    def test_default_judge_skips_without_api_key(self, monkeypatch):
        monkeypatch.setattr(evaluator.cc, "CLAUDE_API_KEY", "")
        assert evaluator._default_judge_fn("q", "a") is None

    def test_default_judge_returns_text_with_api_key(self, monkeypatch):
        monkeypatch.setattr(evaluator.cc, "CLAUDE_API_KEY", "sk-test")
        monkeypatch.setattr(
            evaluator.cc, "ask_claude_api",
            lambda *a, **k: ('{"helpfulness":5,"accuracy":4,"tone":5}', {}),
        )
        out = evaluator._default_judge_fn("q", "a")
        assert "helpfulness" in out

    def test_default_judge_skips_on_error_marker(self, monkeypatch):
        monkeypatch.setattr(evaluator.cc, "CLAUDE_API_KEY", "sk-test")
        monkeypatch.setattr(
            evaluator.cc, "ask_claude_api", lambda *a, **k: ("[Error] boom", {})
        )
        assert evaluator._default_judge_fn("q", "a") is None

    def test_default_judge_skips_on_exception(self, monkeypatch):
        monkeypatch.setattr(evaluator.cc, "CLAUDE_API_KEY", "sk-test")
        def _boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(evaluator.cc, "ask_claude_api", _boom)
        assert evaluator._default_judge_fn("q", "a") is None

    def test_default_judge_end_to_end_via_judge_response(self, monkeypatch):
        monkeypatch.setattr(evaluator.cc, "CLAUDE_API_KEY", "sk-test")
        monkeypatch.setattr(
            evaluator.cc, "ask_claude_api",
            lambda *a, **k: ('{"helpfulness":4,"accuracy":4,"tone":4}', {}),
        )
        score = evaluator.judge_response("q", "a")  # default claude_fn
        assert score.composite == 0.8


class TestPersistenceAndAverages:
    def test_persist_writes_all_dimensions(self):
        uid = _uid()
        evaluator.persist_judge_score(
            "telegram", uid, evaluator.JudgeScore(4, 5, 3, "ok"), "preview"
        )
        store.flush_writes()
        avgs = evaluator.get_judge_averages("telegram", uid)
        assert set(avgs) == {
            "judge_helpfulness", "judge_accuracy", "judge_tone", "judge_composite",
        }
        assert avgs["judge_accuracy"] == 1.0  # 5/5

    def test_justification_persisted_on_composite_row(self):
        uid = _uid()
        evaluator.persist_judge_score(
            "telegram", uid, evaluator.JudgeScore(3, 3, 3, "needs detail"), ""
        )
        store.flush_writes()
        conn = store._get_conn()
        meta = conn.execute(
            "SELECT metadata FROM quality_scores WHERE user_id=? AND evaluator='judge_composite'",
            (uid,),
        ).fetchone()
        assert meta[0] == "needs detail"

    def test_averages_empty_when_no_samples(self):
        assert evaluator.get_judge_averages("telegram", _uid()) == {}

    def test_maybe_judge_samples_and_persists_at_rate_one(self):
        uid = _uid()
        score = evaluator.maybe_judge(
            "telegram", uid, "q", "a", rate=1.0,
            claude_fn=_judge_fn('{"helpfulness":5,"accuracy":5,"tone":5}'),
        )
        assert score is not None
        store.flush_writes()
        assert evaluator.get_judge_averages("telegram", uid)["judge_composite"] == 1.0

    def test_maybe_judge_sampled_but_judge_unavailable_returns_none(self):
        uid = _uid()
        score = evaluator.maybe_judge(
            "telegram", uid, "q", "a", rate=1.0, claude_fn=_judge_fn(None),
        )
        assert score is None
        store.flush_writes()
        assert evaluator.get_judge_averages("telegram", uid) == {}

    def test_maybe_judge_noops_when_not_sampled(self):
        uid = _uid()
        score = evaluator.maybe_judge(
            "telegram", uid, "q", "a", rate=0.0,
            claude_fn=_judge_fn('{"helpfulness":5,"accuracy":5,"tone":5}'),
        )
        assert score is None
        store.flush_writes()
        assert evaluator.get_judge_averages("telegram", uid) == {}
