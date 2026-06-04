"""
Prompt self-optimization — A/B test system prompts and promote the winner.

The "self-improving" claim is only real if the prompts themselves improve. This
module maintains N candidate system prompts, routes a fraction of traffic to
each (weighted), records a quality score per variant (judge scores from
``evaluator`` and/or user ratings), and promotes the variant that beats the
baseline by a margin over a minimum sample size — demoting the losers.

Routing is locked per conversation: once a conversation is assigned a variant it
stays on it, so a single conversation never switches voice mid-thread. Tables are
owned here (created lazily) — same pattern as ``commitments.py``/``preferences.py``.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import store as _store

log = logging.getLogger(__name__)

# Statuses a variant can hold.
STATUS_BASELINE = "baseline"   # the reference variant promotions are measured against
STATUS_ACTIVE = "active"       # a candidate receiving traffic
STATUS_RETIRED = "retired"     # demoted out of rotation

# Promotion needs at least this many scored samples and this average-score margin
# over baseline before a candidate's weight is increased.
PROMOTE_MIN_SAMPLES = int(os.getenv("PROMPT_PROMOTE_MIN_SAMPLES", "20"))
PROMOTE_MARGIN = float(os.getenv("PROMPT_PROMOTE_MARGIN", "0.05"))

_schema_ready = False


@dataclass
class Variant:
    id: str
    text: str
    traffic_weight: float
    status: str
    created_at: float


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = _store._get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_variants (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            traffic_weight REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_assignments (
            conversation_key TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            assigned_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_variant_scores (
            variant_id TEXT NOT NULL,
            score REAL NOT NULL,
            ts REAL NOT NULL
        )
        """
    )
    conn.commit()
    _schema_ready = True


def _row_to_variant(row) -> Variant:
    return Variant(id=row[0], text=row[1], traffic_weight=row[2], status=row[3], created_at=row[4])


def add_variant(
    text: str,
    *,
    weight: float = 1.0,
    status: str = STATUS_ACTIVE,
    now: Optional[float] = None,
) -> str:
    """Insert a new prompt variant and return its id."""
    _ensure_schema()
    now = time.time() if now is None else now
    vid = uuid.uuid4().hex[:12]
    conn = _store._get_conn()
    conn.execute(
        "INSERT INTO prompt_variants (id, text, traffic_weight, status, created_at) VALUES (?,?,?,?,?)",
        (vid, text, weight, status, now),
    )
    conn.commit()
    return vid


def list_variants(include_retired: bool = True) -> list[Variant]:
    _ensure_schema()
    conn = _store._get_conn()
    rows = conn.execute(
        "SELECT id, text, traffic_weight, status, created_at FROM prompt_variants ORDER BY created_at"
    ).fetchall()
    out = [_row_to_variant(r) for r in rows]
    if not include_retired:
        out = [v for v in out if v.status != STATUS_RETIRED]
    return out


def ensure_baseline(text: str, *, now: Optional[float] = None) -> Variant:
    """Guarantee a baseline variant exists, seeding it from ``text`` the first
    time. Idempotent — returns the existing baseline if there already is one."""
    _ensure_schema()
    for v in list_variants():
        if v.status == STATUS_BASELINE:
            return v
    vid = add_variant(text, weight=1.0, status=STATUS_BASELINE, now=now)
    return get_variant(vid)


def get_variant(variant_id: str) -> Optional[Variant]:
    _ensure_schema()
    conn = _store._get_conn()
    row = conn.execute(
        "SELECT id, text, traffic_weight, status, created_at FROM prompt_variants WHERE id=?",
        (variant_id,),
    ).fetchone()
    return _row_to_variant(row) if row else None


def _weighted_choice(variants: list[Variant], rng: random.Random) -> Variant:
    """Pick a variant proportional to traffic_weight (ignores non-positive weights)."""
    pool = [v for v in variants if v.traffic_weight > 0]
    if not pool:
        pool = variants
    total = sum(v.traffic_weight for v in pool)
    if total <= 0:
        return pool[0]
    r = rng.random() * total
    upto = 0.0
    for v in pool:
        upto += v.traffic_weight
        if r <= upto:
            return v
    return pool[-1]  # pragma: no cover - float rounding guard


def assign_variant(
    conversation_key: str,
    *,
    baseline_text: Optional[str] = None,
    rng: Optional[random.Random] = None,
    now: Optional[float] = None,
) -> Variant:
    """Return the variant this conversation should use, assigning one if needed.

    The assignment is sticky: the same ``conversation_key`` always maps to the
    same variant (coherent voice within a thread). When no variants exist yet and
    ``baseline_text`` is given, a baseline is seeded so routing always succeeds.
    """
    _ensure_schema()
    now = time.time() if now is None else now
    conn = _store._get_conn()
    row = conn.execute(
        "SELECT variant_id FROM prompt_assignments WHERE conversation_key=?",
        (conversation_key,),
    ).fetchone()
    if row:
        existing = get_variant(row[0])
        if existing:
            return existing
        # Assigned variant was deleted — fall through and reassign.

    candidates = [v for v in list_variants() if v.status in (STATUS_BASELINE, STATUS_ACTIVE)]
    if not candidates:
        if baseline_text is None:
            raise RuntimeError("no prompt variants available and no baseline_text to seed")
        candidates = [ensure_baseline(baseline_text, now=now)]

    chosen = _weighted_choice(candidates, rng or random.Random())
    conn.execute(
        """INSERT INTO prompt_assignments (conversation_key, variant_id, assigned_at)
           VALUES (?,?,?)
           ON CONFLICT(conversation_key) DO UPDATE SET
               variant_id=excluded.variant_id, assigned_at=excluded.assigned_at""",
        (conversation_key, chosen.id, now),
    )
    conn.commit()
    return chosen


def record_score(variant_id: str, score: float, *, now: Optional[float] = None) -> None:
    """Attribute a quality score (0..1) to a variant for later aggregation."""
    _ensure_schema()
    now = time.time() if now is None else now
    conn = _store._get_conn()
    conn.execute(
        "INSERT INTO prompt_variant_scores (variant_id, score, ts) VALUES (?,?,?)",
        (variant_id, score, now),
    )
    conn.commit()


def variant_stats(variant_id: str) -> dict:
    """Return ``{"count": int, "avg": float}`` over a variant's recorded scores."""
    _ensure_schema()
    conn = _store._get_conn()
    row = conn.execute(
        "SELECT COUNT(*), AVG(score) FROM prompt_variant_scores WHERE variant_id=?",
        (variant_id,),
    ).fetchone()
    count = row[0] or 0
    return {"count": count, "avg": round(row[1], 3) if row[1] is not None else 0.0}


def _set_weight(variant_id: str, weight: float, status: Optional[str] = None) -> None:
    conn = _store._get_conn()
    if status is None:
        conn.execute("UPDATE prompt_variants SET traffic_weight=? WHERE id=?", (weight, variant_id))
    else:
        conn.execute(
            "UPDATE prompt_variants SET traffic_weight=?, status=? WHERE id=?",
            (weight, status, variant_id),
        )
    conn.commit()


def maybe_promote(*, min_samples: Optional[int] = None, margin: Optional[float] = None) -> dict:
    """Compare each active candidate against the baseline and adjust weights.

    A candidate with at least ``min_samples`` scores whose average beats the
    baseline average by at least ``margin`` gets its weight bumped; a candidate
    that loses by the same margin (with enough samples) is retired. Returns a
    summary ``{"promoted": [...], "demoted": [...]}`` for the caller to report.
    """
    min_samples = PROMOTE_MIN_SAMPLES if min_samples is None else min_samples
    margin = PROMOTE_MARGIN if margin is None else margin
    variants = list_variants(include_retired=False)
    baseline = next((v for v in variants if v.status == STATUS_BASELINE), None)
    if baseline is None:
        return {"promoted": [], "demoted": []}
    base_avg = variant_stats(baseline.id)["avg"]

    promoted, demoted = [], []
    for v in variants:
        if v.status != STATUS_ACTIVE:
            continue
        stats = variant_stats(v.id)
        if stats["count"] < min_samples:
            continue
        if stats["avg"] >= base_avg + margin:
            _set_weight(v.id, min(10.0, v.traffic_weight * 2))
            promoted.append(v.id)
        elif stats["avg"] <= base_avg - margin:
            _set_weight(v.id, 0.0, status=STATUS_RETIRED)
            demoted.append(v.id)
    return {"promoted": promoted, "demoted": demoted}


def force_promote(variant_id: str) -> bool:
    """Admin override: make ``variant_id`` the baseline and demote the old one."""
    _ensure_schema()
    target = get_variant(variant_id)
    if target is None:
        return False
    for v in list_variants():
        if v.status == STATUS_BASELINE and v.id != variant_id:
            _set_weight(v.id, 1.0, status=STATUS_ACTIVE)
    _set_weight(variant_id, max(1.0, target.traffic_weight), status=STATUS_BASELINE)
    return True
