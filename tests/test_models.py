"""Tests for the model/pricing registry (docs/improvements.md items 3 and 16).

Two behaviors matter here and neither was covered before:

  - the shipped default model IDs are current, not ones Anthropic has already
    scheduled for retirement (item 3);
  - a turn's cost can be computed from token counts, because in API mode the
    backend reports tokens and no cost — which is why every API-mode turn used
    to be recorded at $0.00 and `/budget` enforced nothing (item 16).
"""

from __future__ import annotations

import importlib

import pytest

from telechat_pkg import models


# ══════════════════════════════════════════════════════════════════════════════
# 1. Model tiers
# ══════════════════════════════════════════════════════════════════════════════


class TestModelTiers:
    #: IDs that Anthropic scheduled for retirement on 2026-06-15 — a date that has
    #: passed. A default pointing at one of these means a fresh API-mode install is
    #: one deprecation sweep away from 404-ing on every message.
    RETIRED = {
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-3-5-sonnet-20241022",
    }

    def test_no_tier_ships_a_retired_model(self):
        for tier, model_id in models.TIERS.items():
            assert model_id not in self.RETIRED, f"{tier} still defaults to a retired model"
        assert models.DEFAULT_API_MODEL not in self.RETIRED

    def test_every_tier_is_priced(self):
        # An unpriced tier silently falls back to Sonnet rates, which is exactly
        # the kind of quiet wrongness the budget guard can't survive.
        for tier, model_id in models.TIERS.items():
            assert models._rates_for(model_id) is not models._FALLBACK_RATES or (
                model_id.startswith("claude-sonnet-5")
            ), f"{tier} ({model_id}) has no pricing entry"

    def test_tiers_are_distinct(self):
        assert len({models.HAIKU, models.SONNET, models.OPUS}) == 3

    def test_env_overrides_a_tier(self, monkeypatch):
        # The point of the registry: a model bump is a config change, not a release.
        monkeypatch.setenv("MODEL_SONNET", "claude-sonnet-9")
        reloaded = importlib.reload(models)
        try:
            assert reloaded.SONNET == "claude-sonnet-9"
            assert reloaded.DEFAULT_API_MODEL == "claude-sonnet-9"
        finally:
            monkeypatch.delenv("MODEL_SONNET")
            importlib.reload(models)

    def test_consumers_read_from_the_registry(self):
        # Item 3's structural half: the IDs used to be hardcoded in five modules,
        # so a bump meant five edits and they drifted apart.
        from telechat_pkg import evaluator, smart_router, two_agent

        assert two_agent.PLANNER_MODEL == models.HAIKU
        assert two_agent.EXECUTOR_MODEL == models.SONNET
        assert evaluator.JUDGE_MODEL == models.HAIKU
        assert smart_router.route_model_api("hi") in set(models.TIERS.values())


# ══════════════════════════════════════════════════════════════════════════════
# 2. Cost estimation
# ══════════════════════════════════════════════════════════════════════════════


class TestEstimateCost:
    def test_priced_from_input_and_output_rates(self):
        # Sonnet 5: $3/Mtok in, $15/Mtok out.
        cost = models.estimate_cost("claude-sonnet-5", input_tokens=1_000_000)
        assert cost == pytest.approx(3.00)
        cost = models.estimate_cost("claude-sonnet-5", output_tokens=1_000_000)
        assert cost == pytest.approx(15.00)
        cost = models.estimate_cost("claude-sonnet-5", 500_000, 100_000)
        assert cost == pytest.approx(1.50 + 1.50)

    def test_cache_reads_are_cheaper_than_fresh_input(self):
        fresh = models.estimate_cost("claude-opus-5", input_tokens=100_000)
        cached = models.estimate_cost("claude-opus-5", cache_read_tokens=100_000)
        assert 0 < cached < fresh

    def test_realistic_turn_is_a_small_nonzero_amount(self):
        # The bug this fixes: this exact turn used to be recorded as $0.00.
        cost = models.estimate_cost("claude-sonnet-5", input_tokens=2_000, output_tokens=500)
        assert cost > 0
        assert cost < 0.05

    def test_dated_snapshot_resolves_via_its_family(self):
        assert models.estimate_cost("claude-haiku-4-5-20251001", 1_000_000) == pytest.approx(
            models.estimate_cost("claude-haiku-4-5", 1_000_000)
        )

    def test_longest_prefix_wins(self):
        # "claude-opus-4-8" and "claude-opus-4" both prefix-match this ID; the
        # more specific entry must win, or Opus 4.8 gets billed at Opus 4 rates
        # (3x too high).
        assert models.estimate_cost("claude-opus-4-8", 1_000_000) == pytest.approx(5.00)
        assert models.estimate_cost("claude-opus-4-5", 1_000_000) == pytest.approx(15.00)

    def test_unknown_model_estimates_rather_than_returning_zero(self, caplog):
        # A silent 0.0 is what broke the budget guard. An unknown model must
        # still produce a chargeable figure — erring high is the safe direction.
        models._warned_unknown.discard("some-future-model")
        with caplog.at_level("WARNING"):
            cost = models.estimate_cost("some-future-model", 1_000_000, 1_000_000)
        assert cost > 0
        assert "no pricing entry" in caplog.text

    def test_unknown_model_warns_once(self):
        models._warned_unknown.discard("noisy-model")
        models.estimate_cost("noisy-model", 10)
        assert "noisy-model" in models._warned_unknown

    def test_zero_and_negative_tokens_are_safe(self):
        assert models.estimate_cost("claude-sonnet-5") == 0.0
        assert models.estimate_cost("claude-sonnet-5", -100, -100) == 0.0

    def test_empty_model_does_not_raise(self):
        assert models.estimate_cost("", 1_000) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. The API-mode stats path
# ══════════════════════════════════════════════════════════════════════════════


class _Usage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class TestApiStatsCost:
    def test_stats_carry_a_cost_and_are_marked_estimated(self):
        from telechat_pkg import claude_core as cc

        stats: dict = {}
        cc._add_estimated_cost(stats, "claude-sonnet-5", _Usage(2_000, 500))
        assert stats["cost_usd"] > 0
        assert stats["cost_estimated"] is True

    def test_cache_reads_are_recorded_when_present(self):
        from telechat_pkg import claude_core as cc

        stats: dict = {}
        cc._add_estimated_cost(stats, "claude-sonnet-5", _Usage(100, 50, 4_000))
        assert stats["cache_read_tokens"] == 4_000

        stats = {}
        cc._add_estimated_cost(stats, "claude-sonnet-5", _Usage(100, 50))
        assert "cache_read_tokens" not in stats

    def test_usage_object_missing_fields_does_not_raise(self):
        from telechat_pkg import claude_core as cc

        class _Bare:
            pass

        stats: dict = {}
        cc._add_estimated_cost(stats, "claude-sonnet-5", _Bare())
        assert stats["cost_usd"] == 0.0

    def test_none_valued_usage_fields_are_treated_as_zero(self):
        from telechat_pkg import claude_core as cc

        stats: dict = {}
        cc._add_estimated_cost(
            stats, "claude-sonnet-5", _Usage(None, None, None)  # type: ignore[arg-type]
        )
        assert stats["cost_usd"] == 0.0
