"""What a Claude Code turn actually *did* — files, tests, errors.

A triage card used to carry only what the assistant *said*: "Done — fixed the
token refresh race." On a phone that is unverifiable. The question a card has to
answer is "do I need to get to my laptop?", and prose cannot answer it, because
the same sentence is written whether the change touched one file or thirty and
whether the tests went green or were never run.

This module reads the answer out of the transcript instead of asking a model for
it. Every fact here is recorded as structured data by Claude Code — the tool
calls it made, the patches they produced, the exit status of each command — so
extraction is exact and free. Summarising a diff through an LLM would be both
slower and less true; the digest model keeps the prose, this keeps the facts.

Three kinds of evidence:

  - **files** — Edit / Write / MultiEdit / NotebookEdit calls, with per-file
    added/removed line counts taken from the patch Claude Code already computed;
  - **tests** — pytest / jest / vitest / cargo / go summary lines recognised in
    command output, so "128 passed" or "3 failed" shows on the card;
  - **errors** — the tail of whatever failed, trimmed to the lines that say why.

Scope is the **last turn**: everything since the most recent human message. A
card about the turn that just ended should not list every file touched since
morning.

Usage:
    from telechat_pkg.bridge_evidence import collect_evidence
    ev = collect_evidence(transcript_path, cwd="/Users/me/proj")
    if not ev.is_empty:
        card += "\\n\\n" + ev.render()
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

#: Only the tail of a transcript is read. A long session's JSONL runs to
#: megabytes and the last turn is at the end of it; reading the whole file on
#: every Stop hook would put that cost in the path of every notification.
TAIL_BYTES = 512 * 1024

#: Card budget. A notification is glanceable or it is not read at all.
MAX_FILES_SHOWN = 5
MAX_ERROR_LINES = 6
MAX_ERROR_LINE_CHARS = 120

# Tools that change files, mapped to the input key naming the target.
_EDIT_TOOLS = {
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}


# ───────────────────────── data ─────────────────────────


@dataclass
class FileChange:
    """One file touched by a turn, with the size of the change."""

    path: str
    added: int = 0
    removed: int = 0
    created: bool = False

    def merge(self, other: "FileChange") -> None:
        """Fold a second edit of the same file into this one."""
        self.added += other.added
        self.removed += other.removed
        self.created = self.created or other.created


@dataclass
class TestResult:
    """A recognised test-runner summary line."""

    framework: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    ok: bool = True
    raw: str = ""


@dataclass
class ErrorSnippet:
    """A failed command and the part of its output that says why."""

    command: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    files: list[FileChange] = field(default_factory=list)
    tests: list[TestResult] = field(default_factory=list)
    errors: list[ErrorSnippet] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.files or self.tests or self.errors)

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def failing(self) -> bool:
        """True if anything in this turn went wrong — drives the card's icon."""
        return bool(self.errors) or any(not t.ok for t in self.tests)

    def render(self) -> str:
        """Telegram-Markdown block for a triage card. Empty string if nothing to show."""
        return _render(self)


# ───────────────────────── collection ─────────────────────────


def collect_evidence(
    transcript_path: Optional[Path], cwd: Optional[str] = None
) -> Evidence:
    """Evidence from the last turn of a Claude Code transcript.

    Never raises: a card missing its evidence block is a smaller failure than a
    Stop hook that crashes, so every parse problem degrades to less evidence.
    """
    entries = _read_tail_entries(transcript_path)
    if not entries:
        return Evidence()
    try:
        return _collect(_last_turn(entries), cwd)
    except Exception:
        log.debug("evidence extraction failed", exc_info=True)
        return Evidence()


def _read_tail_entries(path: Optional[Path]) -> list[dict]:
    if not path:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(-TAIL_BYTES, 2)
                fh.readline()  # drop the partial line the seek landed inside
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        log.debug("could not read transcript tail %s", path, exc_info=True)
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _is_human_turn(entry: dict) -> bool:
    """True for a message a person typed, as opposed to a tool result.

    Claude Code records tool results as ``type: "user"`` entries too — they are
    the transcript's way of feeding output back to the model. Treating those as
    turn boundaries would cut the window at the last tool call and lose every
    file the turn touched.
    """
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return not any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        )
    return False


def _last_turn(entries: list[dict]) -> list[dict]:
    """The entries after the most recent human message."""
    for i in range(len(entries) - 1, -1, -1):
        if _is_human_turn(entries[i]):
            return entries[i + 1 :]
    return entries


def _collect(entries: list[dict], cwd: Optional[str]) -> Evidence:
    results = _index_results(entries)
    ev = Evidence()
    by_path: dict[str, FileChange] = {}

    for entry in entries:
        for block in _content_blocks(entry):
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if not isinstance(inp, dict):
                continue
            outcome = results.get(block.get("id") or "")

            if name in _EDIT_TOOLS:
                change = _file_change(name, inp, outcome, cwd)
                if change:
                    existing = by_path.get(change.path)
                    if existing:
                        existing.merge(change)
                    else:
                        by_path[change.path] = change
                        ev.files.append(change)
            elif name == "Bash":
                _collect_command(ev, str(inp.get("command") or ""), outcome)

    return ev


def _content_blocks(entry: dict) -> Iterable[dict]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _index_results(entries: list[dict]) -> dict[str, dict]:
    """Map tool_use id → {"result": <toolUseResult>, "is_error": bool, "text": str}.

    Claude Code splits a tool's outcome across two places: the ``tool_result``
    block carries the error flag and the text the model saw, while the sibling
    ``toolUseResult`` key on the same entry carries the structured form (stdout,
    stderr, the computed patch). Both are wanted, so both are indexed together.
    """
    out: dict[str, dict] = {}
    for entry in entries:
        structured = entry.get("toolUseResult")
        for block in _content_blocks(entry):
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            out[tid] = {
                "result": structured if isinstance(structured, dict) else None,
                "is_error": bool(block.get("is_error")),
                "text": _as_text(block.get("content")),
            }
    return out


def _as_text(content: Any) -> str:
    """Flatten a tool_result's content, which is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


# ───────────────────────── files ─────────────────────────


def _file_change(
    tool: str, inp: dict, outcome: Optional[dict], cwd: Optional[str]
) -> Optional[FileChange]:
    raw_path = inp.get(_EDIT_TOOLS[tool])
    if not raw_path:
        return None
    # A tool call that failed did not change the file — listing it as changed
    # would send someone to their laptop to look at an edit that never landed.
    if outcome and outcome.get("is_error"):
        return None

    path = _rel_path(str(raw_path), cwd)
    patch = (outcome or {}).get("result") or {}
    added, removed = _patch_counts(patch)
    created = str(patch.get("type") or "").lower() == "create"

    if added or removed:
        return FileChange(path, added, removed, created)

    # No patch recorded (older transcripts, or a tool whose result was not
    # captured) — count from the strings the call itself carried. Approximate by
    # construction, and still far better than showing a bare filename.
    if tool == "Write":
        return FileChange(path, _line_count(inp.get("content")), 0, created=True)
    if tool == "MultiEdit":
        edits = inp.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    added += _line_count(edit.get("new_string"))
                    removed += _line_count(edit.get("old_string"))
    else:
        added = _line_count(inp.get("new_string") or inp.get("new_source"))
        removed = _line_count(inp.get("old_string") or inp.get("old_source"))
    return FileChange(path, added, removed, created)


def _patch_counts(patch: dict) -> tuple[int, int]:
    """Added/removed line counts from Claude Code's ``structuredPatch``."""
    hunks = patch.get("structuredPatch")
    if not isinstance(hunks, list):
        return 0, 0
    added = removed = 0
    for hunk in hunks:
        if not isinstance(hunk, dict):
            continue
        for line in hunk.get("lines") or []:
            if not isinstance(line, str) or not line:
                continue
            if line[0] == "+":
                added += 1
            elif line[0] == "-":
                removed += 1
    return added, removed


def _line_count(text: Any) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return len(text.splitlines())


def _rel_path(path: str, cwd: Optional[str]) -> str:
    """Shorten an absolute path against the session's working directory."""
    if cwd:
        try:
            return str(Path(path).relative_to(cwd))
        except ValueError:
            pass
    home = str(Path.home())
    if path.startswith(home + "/"):
        return "~/" + path[len(home) + 1 :]
    return path


# ───────────────────────── tests + errors ─────────────────────────


def _collect_command(ev: Evidence, command: str, outcome: Optional[dict]) -> None:
    if outcome is None:
        return
    result = outcome.get("result") or {}
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    text = "\n".join(p for p in (stdout, stderr) if p) or outcome.get("text") or ""

    for line in text.splitlines():
        parsed = _parse_test_line(line)
        if parsed:
            ev.tests.append(parsed)

    if outcome.get("is_error"):
        snippet = _error_snippet(command, text)
        if snippet:
            ev.errors.append(snippet)


# Counts appearing in test summaries, e.g. "12 passed", "3 failed".
_COUNT_RE = re.compile(r"(\d+)\s+([a-z]+)", re.I)
# A pytest summary is *only* count phrases, optionally with a duration.
_PYTEST_TAIL_RE = re.compile(r"\s+in\s+[\d.:]+\s*s(?:econds)?\b.*$", re.I)
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")
_CARGO_RE = re.compile(r"^test result:\s*(ok|FAILED)\b", re.I)
_JEST_RE = re.compile(r"^\s*Tests[:\s]\s*\d", re.I)
_GO_RE = re.compile(r"^(ok|FAIL)\s+\S+")

_PYTEST_WORDS = {
    "passed", "failed", "error", "errors", "skipped", "xfailed",
    "xpassed", "warning", "warnings", "deselected",
}


def _parse_test_line(line: str) -> Optional[TestResult]:
    """Recognise a test-runner summary line, or return None.

    Deliberately strict. A card that claims "3 failed" because a log line
    happened to contain those words is worse than a card with no test line at
    all, so each parser requires the shape of a real summary, not just keywords.
    """
    stripped = line.strip()
    if not stripped:
        return None
    for parser in (_parse_pytest, _parse_jest, _parse_cargo, _parse_go):
        result = parser(stripped)
        if result:
            return result
    return None


def _parse_pytest(line: str) -> Optional[TestResult]:
    core = line.strip("= \t")
    core = _PYTEST_TAIL_RE.sub("", core)
    core = _PAREN_TAIL_RE.sub("", core).strip()
    if not core:
        return None
    counts: dict[str, int] = {}
    for part in core.split(","):
        match = re.fullmatch(r"(\d+)\s+([a-z]+)", part.strip(), re.I)
        # Every comma-separated part must be a count phrase; one stray word and
        # this is prose that merely mentions "passed".
        if not match or match.group(2).lower() not in _PYTEST_WORDS:
            return None
        counts[match.group(2).lower()] = int(match.group(1))
    if not counts or not (counts.keys() & {"passed", "failed", "error", "errors"}):
        return None
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    return TestResult(
        framework="pytest",
        passed=counts.get("passed", 0),
        failed=failed,
        skipped=counts.get("skipped", 0),
        ok=failed == 0,
        raw=line.strip("= \t"),
    )


def _parse_jest(line: str) -> Optional[TestResult]:
    if not _JEST_RE.match(line):
        return None
    counts = {
        word.lower(): int(num)
        for num, word in _COUNT_RE.findall(line)
        if word.lower() in {"passed", "failed", "skipped", "todo", "total"}
    }
    if not counts:
        return None
    failed = counts.get("failed", 0)
    return TestResult(
        framework="jest",
        passed=counts.get("passed", 0),
        failed=failed,
        skipped=counts.get("skipped", 0) + counts.get("todo", 0),
        ok=failed == 0,
        raw=line,
    )


def _parse_cargo(line: str) -> Optional[TestResult]:
    match = _CARGO_RE.match(line)
    if not match:
        return None
    counts = {
        word.lower(): int(num)
        for num, word in _COUNT_RE.findall(line)
        if word.lower() in {"passed", "failed", "ignored"}
    }
    return TestResult(
        framework="cargo",
        passed=counts.get("passed", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("ignored", 0),
        ok=match.group(1).lower() == "ok",
        raw=line,
    )


def _parse_go(line: str) -> Optional[TestResult]:
    match = _GO_RE.match(line)
    if not match:
        return None
    # `go test` reports per package and gives no counts, so this carries only a
    # verdict — which is the part that decides whether you get up.
    return TestResult(framework="go", ok=match.group(1) == "ok", raw=line)


# Lines worth showing from a failure. Ordered by how directly they name a cause.
_ERROR_MARKERS = (
    "error", "failed", "fail:", "exception", "traceback", "assert",
    "cannot find", "not found", "no such", "undefined", "unexpected",
    "syntaxerror", "typeerror", "valueerror", "panic:", "fatal",
    "refused", "timed out", "permission denied",
)


def _error_snippet(command: str, text: str) -> Optional[ErrorSnippet]:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    # Prefer the lines that name a cause; fall back to the tail, which is where
    # a command that failed quietly puts whatever it did say.
    marked = [l for l in lines if any(m in l.lower() for m in _ERROR_MARKERS)]
    chosen = (marked or lines)[-MAX_ERROR_LINES:]
    return ErrorSnippet(
        command=_shorten(command.strip().splitlines()[0] if command.strip() else "", 80),
        lines=[_shorten(l, MAX_ERROR_LINE_CHARS) for l in chosen],
    )


def _shorten(text: str, limit: int) -> str:
    text = text.replace("\t", "    ")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ───────────────────────── rendering ─────────────────────────


def _code(text: str) -> str:
    """Wrap in a Telegram code span.

    Paths go in backticks rather than through the bridge's ``_md`` scrubber:
    that replaces ``_`` with a space to keep Markdown from italicising, which
    turns ``test_auth.py`` into ``test auth.py`` — a filename nobody can search
    for. Inside a code span Telegram takes the text literally, so the only
    character that has to go is the backtick itself.
    """
    return "`" + (text or "").replace("`", "'") + "`"


def _signed(added: int, removed: int) -> str:
    parts = []
    if added:
        parts.append(f"+{added}")
    if removed:
        parts.append(f"−{removed}")
    return " ".join(parts)


def _render(ev: Evidence) -> str:
    if ev.is_empty:
        return ""
    blocks = []

    if ev.files:
        total = _signed(ev.added, ev.removed)
        noun = "file" if len(ev.files) == 1 else "files"
        head = f"📝 *{len(ev.files)} {noun}*"
        if total:
            head += f"  {total}"
        lines = [head]
        for change in ev.files[:MAX_FILES_SHOWN]:
            counts = _signed(change.added, change.removed)
            mark = " *new*" if change.created else ""
            lines.append(f"  {_code(change.path)}{mark}" + (f"  {counts}" if counts else ""))
        hidden = len(ev.files) - MAX_FILES_SHOWN
        if hidden > 0:
            lines.append(f"  _…and {hidden} more_")
        blocks.append("\n".join(lines))

    for result in _latest_per_framework(ev.tests):
        blocks.append(_render_test(result))

    for err in ev.errors[-2:]:
        head = "❌ *Failed*"
        if err.command:
            head += f" — {_code(err.command)}"
        body = "\n".join(err.lines)
        blocks.append(head + "\n```\n" + body.replace("```", "'''") + "\n```")

    return "\n\n".join(blocks)


def _latest_per_framework(tests: list[TestResult]) -> list[TestResult]:
    """The most recent result from each runner, in first-seen order.

    A turn that runs the suite three times has one answer, not three: the last
    one. But a turn that runs pytest and then npm test has two, and dropping
    either would hide a failure.
    """
    latest: dict[str, TestResult] = {}
    for result in tests:
        latest[result.framework] = result
    return list(latest.values())


def _render_test(result: TestResult) -> str:
    icon = "✅" if result.ok else "❌"
    parts = []
    if result.passed:
        parts.append(f"{result.passed} passed")
    if result.failed:
        parts.append(f"{result.failed} failed")
    if result.skipped:
        parts.append(f"{result.skipped} skipped")
    detail = " · ".join(parts) or ("passed" if result.ok else "failed")
    return f"{icon} *{result.framework}* — {detail}"
