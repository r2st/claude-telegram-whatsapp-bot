"""
Smart Model Routing (Feature 3) — auto-select haiku/sonnet/opus based on query complexity.

Inspired by the Customer Support Agent quickstart which supports multiple Claude models
with automatic switching. Uses heuristic classification to route simple queries to the
fastest model and complex ones to the most capable.

Usage:
    from telechat_pkg.smart_router import route_model, classify_complexity
    model = route_model("What time is it?")         # → "haiku"
    model = route_model("Refactor this codebase...")  # → "sonnet"
    model = route_model("Design a distributed system with fault tolerance...")  # → "opus"
"""
from __future__ import annotations

import logging
import os
import re

from . import models

log = logging.getLogger(__name__)

# Complexity thresholds (adjustable via env)
HAIKU_MAX_TOKENS = int(os.getenv("SMART_ROUTE_HAIKU_MAX", "50"))
OPUS_MIN_TOKENS = int(os.getenv("SMART_ROUTE_OPUS_MIN", "200"))

# Patterns that suggest higher complexity
_COMPLEX_PATTERNS = [
    r"\b(refactor|architect|design|implement|build|create|develop|optimize)\b",
    r"\b(system|framework|pipeline|infrastructure|distributed)\b",
    r"\b(analyze|compare|evaluate|trade-?offs?|pros?\s*(?:and|&)\s*cons?)\b",
    r"\b(explain\s+(?:in\s+detail|thoroughly|step\s+by\s+step))\b",
    r"\b(write\s+(?:a\s+)?(?:full|complete|comprehensive|detailed))\b",
    r"\b(debug|troubleshoot|diagnose)\b",
    r"\b(security|vulnerability|audit|compliance)\b",
    r"```",  # code blocks indicate technical tasks
]

_SIMPLE_PATTERNS = [
    r"^(?:hi|hello|hey|thanks|thank you|ok|yes|no|sure)\b",
    r"^(?:what\s+(?:is|are|was|were)\b)",
    r"^(?:who\s+(?:is|are|was|were)\b)",
    r"^(?:when\s+(?:is|was|did|does|will)\b)",
    r"^(?:where\s+(?:is|are|was|were)\b)",
    r"^(?:how\s+(?:many|much|old|long|far|tall)\b)",
    r"\b(?:translate|convert|calculate|define)\b",
    r"^(?:list|name|give\s+me)\b",
]

_OPUS_PATTERNS = [
    r"\b(multi-?step|chain[- ]of[- ]thought|reason(?:ing)?)\b",
    r"\b(mathematical\s+proof|theorem|formal\s+verification)\b",
    r"\b(creative\s+writing|novel|story|poem|essay)\b.*\b(long|detailed|full)\b",
    r"\b(research\s+paper|white\s*paper|literature\s+review)\b",
    r"\b(complex|advanced|sophisticated|nuanced)\b.*\b(analysis|discussion)\b",
]

_compiled_complex = [re.compile(p, re.IGNORECASE) for p in _COMPLEX_PATTERNS]
_compiled_simple = [re.compile(p, re.IGNORECASE) for p in _SIMPLE_PATTERNS]
_compiled_opus = [re.compile(p, re.IGNORECASE) for p in _OPUS_PATTERNS]


def classify_complexity(text: str) -> str:
    """Classify query complexity: 'simple', 'moderate', or 'complex'."""
    text = text.strip()
    words = text.split()
    word_count = len(words)

    # Very short queries are simple
    if word_count <= 5:
        return "simple"

    # Check for opus-level complexity
    opus_score = sum(1 for p in _compiled_opus if p.search(text))
    if opus_score >= 2 or (opus_score >= 1 and word_count > OPUS_MIN_TOKENS):
        return "complex"

    # Check for explicit simplicity
    simple_score = sum(1 for p in _compiled_simple if p.search(text))
    if simple_score >= 1 and word_count <= HAIKU_MAX_TOKENS:
        return "simple"

    # Check for moderate/complex patterns
    complex_score = sum(1 for p in _compiled_complex if p.search(text))
    if complex_score >= 3:
        return "complex"
    if complex_score >= 1 or word_count > HAIKU_MAX_TOKENS:
        return "moderate"

    # Short-ish factual queries
    if word_count <= HAIKU_MAX_TOKENS:
        return "simple"

    return "moderate"  # pragma: no cover — unreachable; kept as safety fallback


def route_model(text: str) -> str:
    """Return the best model name for the given query text."""
    complexity = classify_complexity(text)
    model_map = {
        "simple": "haiku",
        "moderate": "sonnet",
        "complex": "opus",
    }
    model = model_map.get(complexity, "sonnet")
    log.debug("Smart routing: %s → %s (%d words)", complexity, model, len(text.split()))
    return model


def route_model_api(text: str) -> str:
    """Return the best API model identifier for the given query text."""
    model = route_model(text)
    api_models = {
        "haiku": os.getenv("SMART_ROUTE_HAIKU_API", models.HAIKU),
        "sonnet": os.getenv("SMART_ROUTE_SONNET_API", models.SONNET),
        "opus": os.getenv("SMART_ROUTE_OPUS_API", models.OPUS),
    }
    return api_models.get(model, api_models["sonnet"])
