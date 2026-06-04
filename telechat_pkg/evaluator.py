"""
LLM-as-judge evaluator for the self-improving loop.

The binary evaluators in ``feedback.py`` catch trivial failures (empty,
truncated, error-marked responses) but can't distinguish a mediocre answer
from a good one. This module samples a fraction of responses and scores them
with a cheap LLM ("judge") on a small rubric — helpfulness, accuracy, tone —
then persists the scores into the existing ``quality_scores`` table so
``/quality`` reflects judged samples alongside the binary metrics.

The judge call is injectable (``claude_fn``) so the sampling/parsing/persistence
logic is testable without network access. The default judge uses Haiku via the
Anthropic API and degrades to a no-op when no API key is configured (the CLI
deployment has no key) rather than raising.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Callable, Optional

from . import claude_core as cc
from . import feedback

log = logging.getLogger(__name__)

# Fraction of responses to judge. Kept low because each judge call costs tokens.
JUDGE_SAMPLE_RATE = float(os.getenv("JUDGE_SAMPLE_RATE", "0.1"))
# Haiku keeps the judge cheap. Override via env if a newer Haiku ships.
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-haiku-4-5")

DIMENSIONS = ("helpfulness", "accuracy", "tone")

_JUDGE_SYSTEM = (
    "You are a strict evaluator of AI assistant responses. Score the response on "
    "three dimensions, each an integer 1-5 (1=poor, 5=excellent):\n"
    "- helpfulness: does it actually address what the user asked?\n"
    "- accuracy: is it correct and free of fabrication?\n"
    "- tone: is the register appropriate and respectful?\n"
    "Respond with ONLY a JSON object, no prose, of the form:\n"
    '{"helpfulness": <int>, "accuracy": <int>, "tone": <int>, '
    '"justification": "<one short sentence>"}'
)


@dataclass
class JudgeScore:
    helpfulness: int
    accuracy: int
    tone: int
    justification: str = ""

    @property
    def composite(self) -> float:
        """Mean of the three dimensions normalised to 0..1 (1-5 → 0.2..1.0)."""
        return round((self.helpfulness + self.accuracy + self.tone) / 3 / 5, 3)

    def as_dimension_scores(self) -> dict[str, float]:
        """Per-dimension scores normalised to 0..1, plus the composite."""
        return {
            "judge_helpfulness": round(self.helpfulness / 5, 3),
            "judge_accuracy": round(self.accuracy / 5, 3),
            "judge_tone": round(self.tone / 5, 3),
            "judge_composite": self.composite,
        }


def should_sample(rate: Optional[float] = None, rng: Optional[random.Random] = None) -> bool:
    """Decide whether this response is in the judged sample.

    ``rate`` defaults to ``JUDGE_SAMPLE_RATE``. A rate <= 0 never samples and a
    rate >= 1 always samples — callers rely on those edges for testing and for
    an operator who wants every response judged.
    """
    r = JUDGE_SAMPLE_RATE if rate is None else rate
    if r <= 0:
        return False
    if r >= 1:
        return True
    return (rng or random).random() < r


def _clamp_score(value) -> int:
    """Coerce a judge-supplied dimension to an int in [1, 5]."""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return 3  # neutral fallback when the judge returns garbage for a field
    return max(1, min(5, v))


def _parse_judge_output(text: str) -> Optional[JudgeScore]:
    """Extract a JudgeScore from the judge's raw text, or None if unparseable."""
    if not text:
        return None
    # The judge is asked for bare JSON but may wrap it in prose or a code fence.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):  # pragma: no cover - regex guarantees an object
        return None
    if not any(d in data for d in DIMENSIONS):
        return None
    return JudgeScore(
        helpfulness=_clamp_score(data.get("helpfulness")),
        accuracy=_clamp_score(data.get("accuracy")),
        tone=_clamp_score(data.get("tone")),
        justification=str(data.get("justification", ""))[:300],
    )


def _default_judge_fn(prompt: str, response: str) -> Optional[str]:
    """Call Haiku to judge a response. Returns raw judge text, or None to skip.

    Degrades to None (skip) when the API is unavailable so a CLI-mode bot with
    no API key simply doesn't judge rather than erroring on every sampled turn.
    """
    if not cc.CLAUDE_API_KEY:
        return None
    user = (
        f"User prompt:\n{prompt[:4000]}\n\n"
        f"Assistant response:\n{response[:4000]}\n\n"
        "Score the assistant response now."
    )
    try:
        text, _stats = cc.ask_claude_api(
            user, [], model=JUDGE_MODEL, system=_JUDGE_SYSTEM, max_tokens=256
        )
    except Exception as exc:  # network/SDK failure shouldn't break the chat path
        log.warning("judge call failed: %s", exc)
        return None
    if text.startswith("[Error]"):
        return None
    return text


def judge_response(
    prompt: str,
    response: str,
    *,
    claude_fn: Optional[Callable[[str, str], Optional[str]]] = None,
) -> Optional[JudgeScore]:
    """Score a single response with the judge. Returns None if the judge is
    unavailable or its output couldn't be parsed."""
    fn = claude_fn or _default_judge_fn
    raw = fn(prompt, response)
    if raw is None:
        return None
    return _parse_judge_output(raw)


def persist_judge_score(
    platform: str, user_id: str, score: JudgeScore, response_preview: str = ""
) -> None:
    """Write each judged dimension (and the composite) into ``quality_scores``.

    The justification is logged in the composite row's ``metadata`` column so a
    later audit can read why the judge scored the way it did.
    """
    dims = score.as_dimension_scores()
    for evaluator, value in dims.items():
        meta = score.justification if evaluator == "judge_composite" else ""
        feedback.save_quality_score(
            platform, user_id, evaluator, value, response_preview, meta
        )


def maybe_judge(
    platform: str,
    user_id: str,
    prompt: str,
    response: str,
    *,
    rate: Optional[float] = None,
    claude_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[JudgeScore]:
    """Sampling entry point for the response path.

    Returns the JudgeScore when this turn was sampled AND judged successfully,
    persisting it as a side effect; otherwise returns None. Safe to call on
    every turn — it no-ops when not sampled.
    """
    if not should_sample(rate, rng):
        return None
    score = judge_response(prompt, response, claude_fn=claude_fn)
    if score is None:
        return None
    persist_judge_score(platform, user_id, score, response[:200])
    return score


def get_judge_averages(platform: str, user_id: str, limit: int = 50) -> dict[str, float]:
    """Average of recent judge scores per dimension for ``/quality``.

    Returns an empty dict when no judged samples exist yet, so callers can show
    "no judged samples" rather than a misleading zero.
    """
    out: dict[str, float] = {}
    for evaluator in ("judge_helpfulness", "judge_accuracy", "judge_tone", "judge_composite"):
        trend = feedback.get_quality_trend(platform, user_id, evaluator, limit)
        if trend:
            out[evaluator] = round(sum(trend) / len(trend), 3)
    return out
