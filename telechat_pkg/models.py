"""Claude model IDs and pricing — the single place either one is written down.

Two problems this solves (items 3 and 16 of docs/improvements.md):

**Model IDs were hardcoded across five modules.** `claude_core.py`,
`smart_router.py`, `two_agent.py`, `memory.py`, and `evaluator.py` each carried
their own default, so a model bump meant five edits and the tree drifted: the
API-mode defaults still pointed at `claude-sonnet-4-20250514` and
`claude-opus-4-20250514`, both of which Anthropic scheduled for retirement on
2026-06-15 — a date that has passed. A fresh `CLAUDE_MODE=api` install was one
deprecation sweep away from 404-ing on every message.

**API mode recorded every turn as costing $0.** Only the CLI path returns a cost
(the stream's ``total_cost_usd``); the API path returns token counts and nothing
else. `cost_tracking.cost_usd` therefore accumulated 0.0 forever, `/budget`
reported `$0.00` no matter the spend, and daily and monthly caps never tripped —
a spend guard that silently enforces nothing. :func:`estimate_cost` computes the
figure from tokens when the backend doesn't supply one.

Every tier is env-overridable, so a model bump needs no code change at all;
:data:`PRICING` is keyed by the same IDs and needs updating alongside them.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


# ─── Model tiers ────────────────────────────────────────────────────────────────

#: Current model IDs by capability tier. These are complete as written — the
#: current families take no date suffix (only Haiku 4.5 still has one, and its
#: bare alias resolves to the same model).
#:
#: Each is overridable by env so a future bump is a config change, not a release.
HAIKU  = os.getenv("MODEL_HAIKU",  "claude-haiku-4-5")
SONNET = os.getenv("MODEL_SONNET", "claude-sonnet-5")
OPUS   = os.getenv("MODEL_OPUS",   "claude-opus-5")

#: The default for API mode. Sonnet is the balance of cost and capability that
#: suits a personal bot answering chat messages; set CLAUDE_API_MODEL (or
#: MODEL_SONNET) to change it.
DEFAULT_API_MODEL = SONNET

TIERS = {"haiku": HAIKU, "sonnet": SONNET, "opus": OPUS}


# ─── Pricing ────────────────────────────────────────────────────────────────────

#: USD per *million* tokens: (input, output, cache_read).
#:
#: First-party Anthropic API rates. Bedrock and Vertex are partner-operated and
#: priced separately — a deployment through either will read low here.
#:
#: Keys are matched by longest prefix (see :func:`_rates_for`), so a dated
#: snapshot like ``claude-haiku-4-5-20251001`` resolves via its family entry
#: without needing a row of its own.
PRICING: dict[str, tuple[float, float, float]] = {
    "claude-fable-5":   (10.00, 50.00, 1.00),
    "claude-mythos-5":  (10.00, 50.00, 1.00),
    "claude-opus-5":    (5.00,  25.00, 0.50),
    "claude-opus-4-8":  (5.00,  25.00, 0.50),
    "claude-opus-4-7":  (5.00,  25.00, 0.50),
    "claude-opus-4-6":  (5.00,  25.00, 0.50),
    "claude-opus-4":    (15.00, 75.00, 1.50),   # 4.5 and the retiring 4.0/4.1
    "claude-sonnet-5":  (3.00,  15.00, 0.30),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-sonnet-4":  (3.00,  15.00, 0.30),   # 4.0 / 4.5
    "claude-haiku-4-5": (1.00,  5.00,  0.10),
    "claude-3-haiku":   (0.25,  1.25,  0.03),
}

#: Charged when the *unknown* model is priced, so an unrecognised ID produces a
#: conservative over-estimate rather than a free turn. Silently returning 0.0 is
#: what made the budget guard useless in the first place; a figure that is too
#: high trips a cap early, which is the safe direction for a spend guard.
_FALLBACK_RATES = PRICING["claude-sonnet-5"]

_warned_unknown: set[str] = set()


def _rates_for(model: str) -> tuple[float, float, float]:
    """Return (input, output, cache_read) USD-per-Mtok rates for ``model``."""
    if not model:
        return _FALLBACK_RATES
    # Longest prefix wins, so "claude-opus-4-8" beats "claude-opus-4" for an
    # ID that both would match.
    best = ""
    for key in PRICING:
        if model.startswith(key) and len(key) > len(best):
            best = key
    if best:
        return PRICING[best]
    if model not in _warned_unknown:
        _warned_unknown.add(model)
        log.warning(
            "no pricing entry for model %r; estimating at %s rates. Add it to "
            "telechat_pkg/models.py PRICING so /usage and /budget stay accurate.",
            model, "claude-sonnet-5",
        )
    return _FALLBACK_RATES


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate a turn's cost in USD from its token counts.

    Used for backends that report tokens but no cost — every API-mode turn. The
    result is an *estimate*: callers that surface it to the user should label it
    as one, because it can't account for tier discounts, batch pricing, or a
    partner platform's own rates.

    Cache *writes* are not modelled. Nothing in this tree sets `cache_control`,
    so there are none to bill; add a term here if that changes.
    """
    in_rate, out_rate, cache_rate = _rates_for(model)
    return (
        max(0, input_tokens) * in_rate
        + max(0, output_tokens) * out_rate
        + max(0, cache_read_tokens) * cache_rate
    ) / 1_000_000
