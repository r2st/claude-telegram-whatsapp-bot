"""
Chat-based coding agent for telechat.

telechat already proxies messages to Claude CLI, which is itself a complete
agentic coding tool (it reads/writes files, runs shell, and tests). This
module adds the missing piece: a disciplined end-to-end development workflow
(distilled from the auto-agent/fastcoder methodology — Explore → Plan →
Implement → Test → Review → Report) plus a per-user project directory.

No server, auth, admin, or web code from fastcoder is used — only the
methodology, expressed as a system prompt and run through telechat's existing
Claude CLI plumbing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from . import claude_core as cc
from .error_classifier import (
    ConvergenceDetector,
    classify_error,
    format_classification,
)

log = logging.getLogger(__name__)

# ─── Per-user project directory store ────────────────────────────────────────
# Maps "<platform>:<user_id>" → absolute project path. Stored as JSON next to
# the bot database so it survives restarts and is shared across adapters.

_PROJECTS_PATH = Path(cc.DB_PATH).parent / "coder_projects.json"
_lock = threading.Lock()


def _load() -> dict[str, str]:
    try:
        return json.loads(_PROJECTS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, str]) -> None:
    with _lock:
        try:
            _PROJECTS_PATH.write_text(json.dumps(data, indent=2))
        except OSError:
            pass


def get_project(platform: str, user_id: str) -> str | None:
    """Return the user's configured project directory, or None."""
    return _load().get(f"{platform}:{user_id}")


def set_project(platform: str, user_id: str, path: str) -> tuple[bool, str]:
    """Set the user's project directory.

    Returns (ok, message). The path must exist and be a directory.
    """
    expanded = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(expanded):
        return False, f"Not a directory: {expanded}"
    data = _load()
    data[f"{platform}:{user_id}"] = expanded
    _save(data)
    return True, expanded


def clear_project(platform: str, user_id: str) -> None:
    data = _load()
    data.pop(f"{platform}:{user_id}", None)
    _save(data)


# ─── Coding-agent system prompt (distilled fastcoder methodology) ────────────

CODER_SYSTEM = """You are a senior software engineer operating as an autonomous \
coding agent inside a real project working directory. You have full file and \
shell access. Deliver complete, working changes end to end — do not stop at \
suggestions.

Follow this disciplined workflow on every coding task:

1. EXPLORE — Before writing anything, inspect the relevant parts of the
   codebase. Learn its conventions, structure, test framework, and the
   specific files involved. Never assume; verify by reading.

2. PLAN — State a short, ordered plan: the files you will change and why.
   Keep changes minimal and focused on exactly what was asked. No unrelated
   refactors, no speculative abstractions, no scope creep.

3. IMPLEMENT — Make the edits directly in the working directory. Match the
   project's existing style and patterns. Write complete, production-quality
   code with real error handling for genuine edge cases — not defensive
   noise. Prefer editing existing files over creating new ones.

4. TEST — Run the project's tests/build/linters for what you changed. If
   there is no test for new behavior, add a focused one. Actually execute
   them; do not claim success without running them.

5. FIX LOOP — If tests or the build fail, diagnose the root cause and fix it.
   Re-run. Iterate until green. Escalate context as needed (read more files,
   check types, look at similar patterns). Bound yourself to reasonable
   attempts; if genuinely blocked, stop and report precisely what's blocking.

6. REVIEW — Re-read your own diff with fresh eyes for correctness, security
   (injection, auth, secrets), performance, and convention fit. Fix issues
   you find before reporting done.

7. REPORT — Finish with a concise summary: what changed (files + one line
   each), how you verified it (commands run + result), and anything the user
   should know or decide next. Keep it short — the user can read the diff.

Rules:
- Stay strictly within the working directory. Do not touch unrelated repos.
- Never run destructive or irreversible commands (force push, history
  rewrite, mass delete, dropping data) without the user explicitly asking.
- Do not commit or push unless the user asked you to.
- Never invent file paths, APIs, or test results. If you didn't run it, say so.
- If the request is ambiguous in a way that materially changes the
  implementation, ask one focused clarifying question before a large change;
  otherwise proceed with the most reasonable interpretation and note the
  assumption in your report.
- Be terse in chat. The user is on a messenger app — lead with the outcome.
"""


def build_task_prompt(task: str, project_dir: str) -> str:
    """Wrap a user's coding request with working-directory context."""
    return (
        f"Project working directory: {project_dir}\n\n"
        f"Coding task:\n{task.strip()}\n\n"
        f"Work through the EXPLORE → PLAN → IMPLEMENT → TEST → FIX → REVIEW → "
        f"REPORT workflow now. Make the changes directly in the working "
        f"directory and verify them by running the project's tests/build."
    )


# ─── Pipeline stage tracking (ported from auto-agent) ────────────────────────
#
# The coding agent's tool usage is mapped to high-level pipeline stages so the
# Telegram progress indicator can show where in the workflow we are, not just
# what tool is running.

class PipelineStage:
    """Named stages with emoji, mapped from tool activity."""

    EXPLORING = ("exploring", "🔍 Exploring")
    PLANNING  = ("planning",  "📋 Planning")
    CODING    = ("coding",    "⚙️ Implementing")
    TESTING   = ("testing",   "🧪 Testing")
    REVIEWING = ("reviewing", "👀 Reviewing")
    FIXING    = ("fixing",    "🔧 Fixing")
    DEPLOYING = ("deploying", "🚀 Deploying")
    DONE      = ("done",      "✅ Done")
    FAILED    = ("failed",    "❌ Failed")


# Map tool names → pipeline stage (heuristic)
_TOOL_STAGE_MAP = {
    "Read":       PipelineStage.EXPLORING,
    "Grep":       PipelineStage.EXPLORING,
    "ListDir":    PipelineStage.EXPLORING,
    "WebSearch":  PipelineStage.EXPLORING,
    "WebFetch":   PipelineStage.EXPLORING,
    "TodoWrite":  PipelineStage.PLANNING,
    "TodoRead":   PipelineStage.PLANNING,
    "Write":      PipelineStage.CODING,
    "Edit":       PipelineStage.CODING,
    "Bash":       None,  # ambiguous — inferred from command content
    "Agent":      PipelineStage.CODING,
}

# Bash command patterns → stage
_BASH_STAGE_PATTERNS = [
    (["test", "pytest", "jest", "vitest", "mocha", "cargo test", "go test",
      "npm test", "yarn test", "bun test", "make test"],
     PipelineStage.TESTING),
    (["lint", "eslint", "ruff", "flake8", "mypy", "tsc", "clippy"],
     PipelineStage.REVIEWING),
    (["git commit", "git push", "deploy", "docker build"],
     PipelineStage.DEPLOYING),
    (["npm install", "pip install", "yarn add", "cargo add"],
     PipelineStage.CODING),
]


class PipelineTracker:
    """Tracks the current pipeline stage based on tool activity.

    Also integrates convergence detection — if the coding agent is stuck
    in a fix loop, this produces an actionable status message.
    """

    def __init__(self):
        self.current_stage = PipelineStage.EXPLORING
        self.stage_history: list[tuple[str, str, float]] = []  # (stage_id, label, timestamp)
        self.convergence = ConvergenceDetector()
        self._fix_count = 0
        self._test_count = 0
        self._last_error: str = ""

    def on_tool(self, tool_name: str, detail: str = "") -> tuple[str, str]:
        """Update stage based on tool use. Returns (stage_id, stage_label)."""
        stage = _TOOL_STAGE_MAP.get(tool_name)

        # Bash: infer from command content
        if tool_name == "Bash" and detail:
            detail_lower = detail.lower()
            for patterns, bash_stage in _BASH_STAGE_PATTERNS:
                if any(p in detail_lower for p in patterns):
                    stage = bash_stage
                    break
            if stage is None:
                # Default: if we've been coding, bash is probably building/running
                if self.current_stage == PipelineStage.CODING:
                    stage = PipelineStage.TESTING
                else:
                    stage = self.current_stage  # stay in current

        if stage is None:
            stage = self.current_stage

        # Track fix loop iterations
        if stage == PipelineStage.TESTING:
            self._test_count += 1
        if (self.current_stage == PipelineStage.TESTING
                and stage == PipelineStage.CODING
                and self._test_count > 0):
            self._fix_count += 1
            stage = PipelineStage.FIXING

        self.current_stage = stage
        sid, label = stage
        self.stage_history.append((sid, label, time.time()))
        return sid, label

    def on_error(self, error_text: str) -> str:
        """Record an error from stderr/test output. Returns classification summary."""
        cls = classify_error(error_text)
        self.convergence.record(cls.fingerprint)
        self._last_error = format_classification(cls)
        return self._last_error

    def on_success(self) -> None:
        """Record a successful iteration (tests passed)."""
        self.convergence.record("")

    def get_convergence_warning(self) -> str | None:
        """Check convergence and return a warning if stuck, or None."""
        result = self.convergence.check()
        if result.status == "progressing":
            return None
        emoji = {
            "oscillating": "🔄",
            "stuck": "🚧",
            "diverging": "📈",
        }.get(result.status, "⚠️")
        return f"{emoji} *{result.status.title()}*: {result.reason}\n_Suggestion: {result.action}_"

    def stage_summary(self) -> str:
        """One-line stage summary for the progress indicator."""
        _, label = self.current_stage
        parts = [label]
        if self._fix_count > 0:
            parts.append(f"(fix attempt {self._fix_count})")
        if self._last_error:
            parts.append(f"\n{self._last_error}")
        return " ".join(parts)

    def pipeline_bar(self) -> str:
        """Visual pipeline progress bar showing completed stages."""
        stages = [
            PipelineStage.EXPLORING,
            PipelineStage.PLANNING,
            PipelineStage.CODING,
            PipelineStage.TESTING,
            PipelineStage.REVIEWING,
        ]
        visited = {sid for sid, _, _ in self.stage_history}
        current_sid, _ = self.current_stage
        parts = []
        for sid, label in stages:
            emoji = label.split()[0]
            if sid == current_sid:
                parts.append(f"▶{emoji}")
            elif sid in visited:
                parts.append(f"✓{emoji}")
            else:
                parts.append(f"·{emoji}")
        return " ".join(parts)
