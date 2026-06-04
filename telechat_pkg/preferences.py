"""
Per-user style preference learning.

Users who repeatedly ask for "shorter answers" or "with code blocks" shouldn't
have to keep asking. This module tracks a small set of style dimensions per
user, each with a confidence that grows on confirming signals, shrinks on
contradicting ones, and decays over time so stale preferences don't ossify.
``prompt_hint`` turns the confident preferences into a one-line style
instruction the chat layer injects into the system prompt.

Signals arrive from three places (wired in the chat layer):
- an explicit ``/prefer <dimension> <value>`` command (strong signal),
- free-text feedback that mentions a style ("be shorter", "use code"),
- a ``/rate`` rating combined with the shape of the response that was rated.

The DB table is owned here (created lazily on first use) to avoid coupling to
``store.init_db`` — same pattern as ``commitments.py``.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from . import store as _store

log = logging.getLogger(__name__)

# Allowed dimensions and their permitted values. Kept deliberately small.
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "length": ("short", "medium", "long"),
    "format": ("plain", "markdown", "code-heavy"),
    "tone": ("formal", "casual"),
}

# A confirming signal moves confidence by this much; contradicting moves down.
_STEP = 0.25
# Confidence decays with this half-life (days) since the last update.
_HALF_LIFE_DAYS = 30.0
# Below this *effective* (decayed) confidence a preference is ignored.
_MIN_CONFIDENCE = 0.3

_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = _store._get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_preferences (
            uid TEXT NOT NULL,
            dimension TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (uid, dimension)
        )
        """
    )
    conn.commit()
    _schema_ready = True


def _decay(confidence: float, updated_at: float, now: Optional[float] = None) -> float:
    """Exponentially decay a stored confidence by its age."""
    now = time.time() if now is None else now
    age_days = max(0.0, (now - updated_at) / 86400.0)
    return confidence * math.pow(0.5, age_days / _HALF_LIFE_DAYS)


def record_signal(
    uid: str,
    dimension: str,
    value: str,
    *,
    weight: float = 1.0,
    now: Optional[float] = None,
) -> None:
    """Fold one observation into a user's preference for ``dimension``.

    Confirming the stored value raises confidence; a different value lowers it,
    and once confidence is exhausted the dimension flips to the new value. Stored
    confidence is the *pre-decay* value; ``get_user_prefs`` applies decay on read.
    Unknown dimensions/values are ignored (defensive against bad callers).
    """
    if dimension not in DIMENSIONS or value not in DIMENSIONS[dimension]:
        log.debug("ignoring unknown preference signal: %s=%s", dimension, value)
        return
    _ensure_schema()
    now = time.time() if now is None else now
    conn = _store._get_conn()
    row = conn.execute(
        "SELECT value, confidence, updated_at FROM user_preferences WHERE uid=? AND dimension=?",
        (uid, dimension),
    ).fetchone()
    step = _STEP * weight
    if row is None:
        new_value, new_conf = value, step
    else:
        cur_value, cur_conf, cur_updated = row[0], row[1], row[2]
        cur_conf = _decay(cur_conf, cur_updated, now)  # age the prior belief first
        if cur_value == value:
            new_value, new_conf = value, min(1.0, cur_conf + step)
        else:
            remaining = cur_conf - step
            if remaining <= 0:
                # Evidence overcame the old belief — switch to the new value.
                new_value, new_conf = value, min(1.0, -remaining)
            else:
                new_value, new_conf = cur_value, remaining
    conn.execute(
        """INSERT INTO user_preferences (uid, dimension, value, confidence, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(uid, dimension) DO UPDATE SET
               value=excluded.value, confidence=excluded.confidence, updated_at=excluded.updated_at""",
        (uid, dimension, new_value, new_conf, now),
    )
    conn.commit()


def get_user_prefs(uid: str, now: Optional[float] = None) -> dict[str, dict]:
    """Return confident preferences for a user, decayed to the present.

    Shape: ``{dimension: {"value": str, "confidence": float}}``. Only dimensions
    whose decayed confidence is at least ``_MIN_CONFIDENCE`` are included, so a
    single stray signal never drives behaviour.
    """
    _ensure_schema()
    now = time.time() if now is None else now
    conn = _store._get_conn()
    rows = conn.execute(
        "SELECT dimension, value, confidence, updated_at FROM user_preferences WHERE uid=?",
        (uid,),
    ).fetchall()
    out: dict[str, dict] = {}
    for dimension, value, confidence, updated_at in rows:
        eff = _decay(confidence, updated_at, now)
        if eff >= _MIN_CONFIDENCE:
            out[dimension] = {"value": value, "confidence": round(eff, 3)}
    return out


def prompt_hint(uid: str, now: Optional[float] = None) -> str:
    """One-line style instruction for system-prompt injection, or "" if unknown."""
    prefs = get_user_prefs(uid, now)
    if not prefs:
        return ""
    clauses = []
    if "length" in prefs:
        clauses.append({"short": "keep answers brief",
                        "medium": "use moderate length",
                        "long": "give thorough, detailed answers"}[prefs["length"]["value"]])
    if "format" in prefs:
        clauses.append({"plain": "prefer plain prose without heavy formatting",
                        "markdown": "use markdown formatting",
                        "code-heavy": "lead with code examples"}[prefs["format"]["value"]])
    if "tone" in prefs:
        clauses.append({"formal": "keep a formal tone",
                        "casual": "keep a casual, friendly tone"}[prefs["tone"]["value"]])
    return "User style preferences: " + "; ".join(clauses) + "."


def parse_prefer_command(arg: str) -> Optional[tuple[str, str]]:
    """Parse the argument of ``/prefer <dimension> <value>``.

    Returns ``(dimension, value)`` if valid, else None. Accepts a leading
    "/prefer" if the caller passed the raw command text.
    """
    if not arg:
        return None
    parts = arg.replace("/prefer", "", 1).split()
    if len(parts) < 2:
        return None
    dimension, value = parts[0].lower(), parts[1].lower()
    if dimension in DIMENSIONS and value in DIMENSIONS[dimension]:
        return dimension, value
    return None


# Phrase → (dimension, value) signals mined from free-text feedback. Ordered so
# the most specific phrases win; first match per dimension is taken.
_TEXT_SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("shorter", "length", "short"),
    ("too long", "length", "short"),
    ("be brief", "length", "short"),
    ("more detail", "length", "long"),
    ("longer", "length", "long"),
    ("more concise", "length", "short"),
    ("code block", "format", "code-heavy"),
    ("show code", "format", "code-heavy"),
    ("with code", "format", "code-heavy"),
    ("no markdown", "format", "plain"),
    ("plain text", "format", "plain"),
    ("more formal", "tone", "formal"),
    ("less formal", "tone", "casual"),
    ("more casual", "tone", "casual"),
)


def infer_signals_from_text(text: str) -> list[tuple[str, str]]:
    """Extract style signals from free-text feedback. One signal per dimension."""
    if not text:
        return []
    low = text.lower()
    seen: dict[str, str] = {}
    for phrase, dimension, value in _TEXT_SIGNALS:
        if phrase in low and dimension not in seen:
            seen[dimension] = value
    return list(seen.items())


def record_text_feedback(uid: str, text: str, *, weight: float = 1.0) -> list[tuple[str, str]]:
    """Mine free-text feedback for style signals and record each. Returns what
    it recorded so callers can acknowledge ("noted: shorter answers")."""
    signals = infer_signals_from_text(text)
    for dimension, value in signals:
        record_signal(uid, dimension, value, weight=weight)
    return signals
