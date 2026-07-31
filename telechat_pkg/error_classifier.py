"""
Error classification and fingerprinting — ported from auto-agent/fastcoder.

Classifies errors into categories (syntax, type, import, logic, env, flaky,
integration, architectural) and generates stable fingerprints so the coding
agent can detect oscillation (same error recurring) and apply smarter
recovery strategies.

Used by the coding agent's fix loop to decide whether to retry, enrich
context, or escalate to the user.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    SYNTAX_ERROR = "syntax"
    TYPE_ERROR = "type"
    IMPORT_ERROR = "import"
    LOGIC_ERROR = "logic"
    ENVIRONMENT_ERROR = "environment"
    FLAKY_ERROR = "flaky"
    INTEGRATION_ERROR = "integration"
    ARCHITECTURAL_ERROR = "architectural"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    DIRECT_FIX = "direct_fix"           # Just fix the error
    INCLUDE_TYPES = "include_types"     # Add type context for retries
    INCLUDE_BROAD_CONTEXT = "broad"     # Read more files, look at patterns
    RERUN = "rerun"                     # Transient — just try again
    REPLAN = "replan"                   # Scrap approach, plan differently
    ESCALATE = "escalate"               # Ask the user for help


@dataclass
class ErrorClassification:
    category: ErrorCategory
    strategy: RecoveryStrategy
    fingerprint: str
    confidence: float = 0.75
    typical_fix_attempts: int = 2


@dataclass
class ConvergenceResult:
    """Result of checking whether the coding agent is making progress."""
    status: str   # "progressing" | "oscillating" | "stuck" | "diverging"
    reason: str
    action: str   # "continue" | "enrich_context" | "replan" | "escalate"


# ─── Pattern-based classifier ─────────────────────────────────────────────────

_PATTERN_MAP: dict[ErrorCategory, list[str]] = {
    ErrorCategory.SYNTAX_ERROR: [
        r"SyntaxError", r"IndentationError", r"invalid syntax",
        r"unexpected token", r"expected.*received", r"Missing closing",
        r"Unterminated", r"Parse error",
    ],
    ErrorCategory.TYPE_ERROR: [
        r"TypeError", r"TS2\d{3}", r"is not compatible with",
        r"Argument of type", r"Property.*does not exist",
        r"Cannot assign", r"Type.*is not assignable",
    ],
    ErrorCategory.IMPORT_ERROR: [
        r"ImportError", r"ModuleNotFoundError", r"Cannot find module",
        r"No module named", r"cannot find name", r"import.*not found",
    ],
    ErrorCategory.LOGIC_ERROR: [
        r"AssertionError", r"AssertionError", r"Expected.*but got",
        r"assertion failed", r"test failed", r"expected value",
        r"FAIL", r"FAILED",
    ],
    ErrorCategory.ENVIRONMENT_ERROR: [
        r"ENOENT", r"EACCES", r"EADDRINUSE",
        r"FileNotFoundError", r"PermissionError", r"OSError",
        r"Environment variable",
    ],
    ErrorCategory.FLAKY_ERROR: [
        r"timeout", r"network", r"connection refused",
        r"temporary failure", r"rate limited", r"ETIMEDOUT",
    ],
    ErrorCategory.INTEGRATION_ERROR: [
        r"connection error", r"database error", r"API error",
        r"service unavailable", r"health check failed",
    ],
    ErrorCategory.ARCHITECTURAL_ERROR: [
        r"circular dependency", r"incompatible design",
        r"breaking change", r"cannot modify",
    ],
}

_STRATEGY_MAP: dict[ErrorCategory, RecoveryStrategy] = {
    ErrorCategory.SYNTAX_ERROR: RecoveryStrategy.DIRECT_FIX,
    ErrorCategory.TYPE_ERROR: RecoveryStrategy.INCLUDE_TYPES,
    ErrorCategory.IMPORT_ERROR: RecoveryStrategy.DIRECT_FIX,
    ErrorCategory.LOGIC_ERROR: RecoveryStrategy.INCLUDE_BROAD_CONTEXT,
    ErrorCategory.ENVIRONMENT_ERROR: RecoveryStrategy.DIRECT_FIX,
    ErrorCategory.FLAKY_ERROR: RecoveryStrategy.RERUN,
    ErrorCategory.INTEGRATION_ERROR: RecoveryStrategy.INCLUDE_BROAD_CONTEXT,
    ErrorCategory.ARCHITECTURAL_ERROR: RecoveryStrategy.REPLAN,
    ErrorCategory.UNKNOWN: RecoveryStrategy.DIRECT_FIX,
}

_FIX_ATTEMPTS: dict[ErrorCategory, int] = {
    ErrorCategory.SYNTAX_ERROR: 1,
    ErrorCategory.TYPE_ERROR: 2,
    ErrorCategory.IMPORT_ERROR: 2,
    ErrorCategory.LOGIC_ERROR: 3,
    ErrorCategory.ENVIRONMENT_ERROR: 2,
    ErrorCategory.FLAKY_ERROR: 3,
    ErrorCategory.INTEGRATION_ERROR: 2,
    ErrorCategory.ARCHITECTURAL_ERROR: 1,
    ErrorCategory.UNKNOWN: 2,
}


def classify_error(error_text: str) -> ErrorClassification:
    """Classify an error string into a category with recovery strategy.

    Args:
        error_text: Raw error output (stderr, test failure, etc.)

    Returns:
        ErrorClassification with category, strategy, and fingerprint.
    """
    # Match against patterns
    category = ErrorCategory.UNKNOWN
    for cat, patterns in _PATTERN_MAP.items():
        if any(re.search(p, error_text, re.IGNORECASE) for p in patterns):
            category = cat
            break

    strategy = _STRATEGY_MAP[category]
    fingerprint = _fingerprint(error_text)
    confidence = 0.95 if category != ErrorCategory.UNKNOWN else 0.3

    return ErrorClassification(
        category=category,
        strategy=strategy,
        fingerprint=fingerprint,
        confidence=confidence,
        typical_fix_attempts=_FIX_ATTEMPTS[category],
    )


def _fingerprint(error_text: str) -> str:
    """Generate a stable fingerprint for an error (strips dynamic values)."""
    normalized = error_text
    # Strip line numbers
    normalized = re.sub(r"(?:line|at|:)\s*\d+", "", normalized)
    # Strip file paths
    normalized = re.sub(r"(/[^\s]+)+", "<path>", normalized)
    # Strip UUIDs / hashes
    normalized = re.sub(r"[a-f0-9]{8,}", "<hash>", normalized)
    # Strip URLs
    normalized = re.sub(r"https?://[^\s]+", "<url>", normalized)
    # Strip comparison values
    normalized = re.sub(r"got\s+['\"]?[^'\"\s]+['\"]?", "got <value>", normalized)

    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def format_classification(cls: ErrorClassification) -> str:
    """Human-readable one-liner for a classification."""
    emoji = {
        ErrorCategory.SYNTAX_ERROR: "🔤",
        ErrorCategory.TYPE_ERROR: "🏷️",
        ErrorCategory.IMPORT_ERROR: "📦",
        ErrorCategory.LOGIC_ERROR: "🧩",
        ErrorCategory.ENVIRONMENT_ERROR: "🖥️",
        ErrorCategory.FLAKY_ERROR: "🎲",
        ErrorCategory.INTEGRATION_ERROR: "🔗",
        ErrorCategory.ARCHITECTURAL_ERROR: "🏗️",
        ErrorCategory.UNKNOWN: "❓",
    }.get(cls.category, "❓")
    return f"{emoji} {cls.category.value} error — strategy: {cls.strategy.value}"


# ─── Convergence detection (ported from fastcoder) ───────────────────────────

class ConvergenceDetector:
    """Detects when a coding agent is stuck, oscillating, or diverging.

    Tracks error fingerprints across iterations and recommends escalation
    when no progress is being made.
    """

    def __init__(self, window_size: int = 4, stuck_threshold: int = 3):
        self.window_size = window_size
        self.stuck_threshold = stuck_threshold
        self._history: list[str] = []  # recent error fingerprints ("" = success)

    def record(self, fingerprint: str = "") -> None:
        """Record an iteration result. Empty fingerprint = success."""
        self._history.append(fingerprint)
        # Keep bounded
        if len(self._history) > 20:
            self._history = self._history[-20:]

    def check(self) -> ConvergenceResult:
        """Check convergence status of recent iterations."""
        if not self._history:
            return ConvergenceResult("progressing", "No iterations yet", "continue")

        recent = self._history[-self.window_size:]
        error_fps = [fp for fp in recent if fp]

        # Check oscillation: same error appearing 2+ times
        if len(error_fps) >= 2:
            counts: dict[str, int] = {}
            for fp in error_fps:
                counts[fp] = counts.get(fp, 0) + 1
            for fp, count in counts.items():
                if count >= 2:
                    return ConvergenceResult(
                        "oscillating",
                        f"Same error repeated {count} times (fingerprint {fp[:8]}…)",
                        "enrich_context",
                    )

        # Check stuck: all recent iterations had errors
        recent_errors = [1 if fp else 0 for fp in recent]
        if len(recent_errors) >= self.stuck_threshold and all(recent_errors[-self.stuck_threshold:]):
            return ConvergenceResult(
                "stuck",
                f"No progress for {self.stuck_threshold} consecutive iterations",
                "replan",
            )

        # Check diverging: error count increasing
        if len(recent_errors) >= 4:
            mid = len(recent_errors) // 2
            first_half = sum(recent_errors[:mid])
            second_half = sum(recent_errors[mid:])
            if second_half > first_half:
                return ConvergenceResult(
                    "diverging",
                    f"Error count increased from {first_half} to {second_half}",
                    "escalate",
                )

        return ConvergenceResult("progressing", "Making progress", "continue")

    def reset(self) -> None:
        self._history.clear()
