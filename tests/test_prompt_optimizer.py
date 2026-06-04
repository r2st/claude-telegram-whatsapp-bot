"""Behaviour tests for telechat_pkg.prompt_optimizer (A/B prompt optimization).

Organised by what the optimizer does: seed a baseline, route traffic by weight,
keep a conversation on one variant, aggregate scores, and promote/demote on the
evidence. RNG and time are injected so routing and promotion are deterministic.
"""
from __future__ import annotations

import random
import uuid

import pytest

from telechat_pkg import prompt_optimizer as po


def _key() -> str:
    return "conv-" + uuid.uuid4().hex[:10]


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test starts with empty optimizer tables — ensure_baseline is global,
    so without this the first test's baseline would leak into the rest."""
    po._ensure_schema()
    conn = po._store._get_conn()
    for t in ("prompt_variants", "prompt_assignments", "prompt_variant_scores"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    yield


class TestBaselineSeeding:
    def test_ensure_baseline_creates_one(self):
        v = po.ensure_baseline("base system prompt")
        assert v.status == po.STATUS_BASELINE
        assert v.text == "base system prompt"

    def test_ensure_baseline_idempotent(self):
        first = po.ensure_baseline("base A")
        second = po.ensure_baseline("base B")  # already exists → unchanged
        assert first.id == second.id
        assert second.text == "base A"


class TestRouting:
    def test_assignment_is_sticky_per_conversation(self):
        po.ensure_baseline("base")
        po.add_variant("alt", weight=1.0)
        key = _key()
        first = po.assign_variant(key, rng=random.Random(1))
        # A different RNG must not change an already-assigned conversation.
        again = po.assign_variant(key, rng=random.Random(999))
        assert first.id == again.id

    def test_weighted_choice_favours_heavier_variant(self):
        # Build an isolated pair and sample assignments across many conversations.
        heavy = po.add_variant("heavy", weight=9.0)
        light = po.add_variant("light", weight=1.0)
        variants = [po.get_variant(heavy), po.get_variant(light)]
        rng = random.Random(42)
        picks = [po._weighted_choice(variants, rng).id for _ in range(200)]
        assert picks.count(heavy) > picks.count(light)

    def test_weighted_choice_all_zero_weights_falls_back(self):
        a = po.add_variant("a", weight=0.0)
        b = po.add_variant("b", weight=0.0)
        chosen = po._weighted_choice([po.get_variant(a), po.get_variant(b)], random.Random(0))
        assert chosen.id in (a, b)  # no crash, returns one of them

    def test_assign_raises_when_no_variants_and_no_seed(self):
        with pytest.raises(RuntimeError):
            po.assign_variant(_key(), rng=random.Random(0))

    def test_seeds_baseline_when_empty(self):
        # Fresh conversation with no variants yet but baseline_text provided.
        # (Other tests may have seeded a baseline already; assignment still works.)
        key = _key()
        v = po.assign_variant(key, baseline_text="seeded base", rng=random.Random(0))
        assert v is not None

    def test_reassigns_when_assigned_variant_deleted(self):
        base = po.ensure_baseline("base")
        key = _key()
        po.assign_variant(key, rng=random.Random(1))
        # Simulate the assigned variant vanishing.
        conn = po._store._get_conn()
        conn.execute("UPDATE prompt_assignments SET variant_id='gone' WHERE conversation_key=?", (key,))
        conn.commit()
        v = po.assign_variant(key, rng=random.Random(1))
        assert v.id != "gone"


class TestScoring:
    def test_record_and_aggregate(self):
        vid = po.add_variant("v")
        po.record_score(vid, 0.8)
        po.record_score(vid, 0.6)
        stats = po.variant_stats(vid)
        assert stats["count"] == 2
        assert stats["avg"] == 0.7

    def test_stats_empty_variant(self):
        vid = po.add_variant("v")
        assert po.variant_stats(vid) == {"count": 0, "avg": 0.0}


class TestPromotion:
    def test_winner_weight_bumped(self):
        base = po.ensure_baseline("base")
        for _ in range(20):
            po.record_score(base.id, 0.5)
        winner = po.add_variant("winner", weight=1.0)
        for _ in range(20):
            po.record_score(winner, 0.9)  # clearly beats baseline
        before = po.get_variant(winner).traffic_weight
        result = po.maybe_promote(min_samples=20, margin=0.05)
        assert winner in result["promoted"]
        assert po.get_variant(winner).traffic_weight > before

    def test_loser_retired(self):
        base = po.ensure_baseline("base")
        for _ in range(20):
            po.record_score(base.id, 0.8)
        loser = po.add_variant("loser", weight=1.0)
        for _ in range(20):
            po.record_score(loser, 0.3)
        result = po.maybe_promote(min_samples=20, margin=0.05)
        assert loser in result["demoted"]
        assert po.get_variant(loser).status == po.STATUS_RETIRED

    def test_insufficient_samples_no_change(self):
        base = po.ensure_baseline("base")
        for _ in range(20):
            po.record_score(base.id, 0.5)
        candidate = po.add_variant("cand", weight=1.0)
        po.record_score(candidate, 0.99)  # only one sample
        result = po.maybe_promote(min_samples=20, margin=0.05)
        assert candidate not in result["promoted"]
        assert po.get_variant(candidate).traffic_weight == 1.0

    def test_no_baseline_is_noop(self, monkeypatch):
        # With no baseline among variants, promotion does nothing.
        monkeypatch.setattr(po, "list_variants", lambda include_retired=True: [])
        assert po.maybe_promote() == {"promoted": [], "demoted": []}


class TestForcePromote:
    def test_force_promote_swaps_baseline(self):
        old = po.ensure_baseline("old base")
        challenger = po.add_variant("challenger", weight=1.0)
        assert po.force_promote(challenger) is True
        assert po.get_variant(challenger).status == po.STATUS_BASELINE
        assert po.get_variant(old.id).status == po.STATUS_ACTIVE

    def test_force_promote_unknown_returns_false(self):
        assert po.force_promote("does-not-exist") is False
