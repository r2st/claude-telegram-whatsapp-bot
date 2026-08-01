"""Tests for triage-card evidence extraction (telechat_pkg/bridge_evidence.py).

A triage card that says "Done — fixed the token refresh race" is unverifiable
from a phone: the same sentence gets written whether one file changed or thirty,
and whether the suite went green or was never run. This module reads those facts
out of the transcript, so the tests here are mostly about *not lying* — the two
ways a card can be worse than no card at all:

  - claiming a file changed when the edit failed, or
  - claiming "3 failed" because some log line mentioned the word.

Fixtures build transcripts in Claude Code's real on-disk shape: tool calls in
``assistant`` entries, their outcomes in ``user`` entries carrying both a
``tool_result`` block and the structured ``toolUseResult`` sibling.
"""

from __future__ import annotations

import json

import pytest

from telechat_pkg import bridge_evidence as ev


# ══════════════════════════════════════════════════════════════════════════════
# Transcript builders — the real JSONL shape, not a convenient one
# ══════════════════════════════════════════════════════════════════════════════


def human(text: str = "do the thing") -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def use(tool: str, tid: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": tool, "input": inp}


def result(tid: str, *, structured: dict | None = None, is_error: bool = False,
           text: str = "") -> dict:
    """A tool outcome entry: the block the model saw + the structured sibling."""
    entry = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tid,
                 "content": text, "is_error": is_error}
            ]
        },
    }
    if structured is not None:
        entry["toolUseResult"] = structured
    return entry


def patch(*lines: str) -> dict:
    """A structuredPatch as Claude Code records it for an Edit."""
    return {"structuredPatch": [{"oldStart": 1, "newStart": 1, "lines": list(lines)}]}


def bash_out(stdout: str = "", stderr: str = "") -> dict:
    return {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False}


def write_transcript(tmp_path, entries: list[dict]):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# 1. File changes
# ══════════════════════════════════════════════════════════════════════════════


class TestFileChanges:
    def test_counts_added_and_removed_from_structured_patch(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/proj/auth.py")),
            result("t1", structured=patch("-old", "+new", "+extra", " context")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert [(f.path, f.added, f.removed) for f in e.files] == [("auth.py", 2, 1)]

    def test_write_is_marked_as_a_new_file(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Write", "t1", file_path="/proj/new.py", content="a\nb\nc")),
            result("t1", structured={"type": "create", "filePath": "/proj/new.py"}),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert e.files[0].created is True
        assert e.files[0].added == 3

    def test_repeated_edits_of_one_file_collapse_to_one_entry(self, tmp_path):
        """Five edits to one file is one file on the card, with the totals summed."""
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/proj/a.py")),
            result("t1", structured=patch("+one", "-gone")),
            assistant(use("Edit", "t2", file_path="/proj/a.py")),
            result("t2", structured=patch("+two", "+three")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert len(e.files) == 1
        assert (e.files[0].added, e.files[0].removed) == (3, 1)

    def test_failed_edit_is_not_reported_as_a_change(self, tmp_path):
        """The load-bearing one: a rejected edit did not touch the file.

        Listing it would send someone to their laptop to look at a change that
        never landed."""
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/proj/a.py",
                          old_string="x", new_string="y")),
            result("t1", is_error=True, text="String to replace not found in file."),
        ])
        assert ev.collect_evidence(t, cwd="/proj").files == []

    def test_falls_back_to_edit_strings_when_no_patch_recorded(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/proj/a.py",
                          old_string="one\ntwo", new_string="uno\ndos\ntres")),
            result("t1", text="ok"),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert (e.files[0].added, e.files[0].removed) == (3, 2)

    def test_multiedit_sums_every_edit_in_the_call(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("MultiEdit", "t1", file_path="/proj/a.py", edits=[
                {"old_string": "a", "new_string": "b\nc"},
                {"old_string": "d\ne", "new_string": "f"},
            ])),
            result("t1", text="ok"),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert (e.files[0].added, e.files[0].removed) == (3, 3)

    def test_paths_are_relative_to_the_session_cwd(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/proj/pkg/deep/mod.py")),
            result("t1", structured=patch("+x")),
        ])
        assert ev.collect_evidence(t, cwd="/proj").files[0].path == "pkg/deep/mod.py"

    def test_path_outside_cwd_is_kept_absolute(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Edit", "t1", file_path="/etc/hosts")),
            result("t1", structured=patch("+x")),
        ])
        assert ev.collect_evidence(t, cwd="/proj").files[0].path == "/etc/hosts"

    def test_notebook_edits_are_counted(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("NotebookEdit", "t1", notebook_path="/proj/nb.ipynb",
                          new_source="import x")),
            result("t1", text="ok"),
        ])
        assert ev.collect_evidence(t, cwd="/proj").files[0].path == "nb.ipynb"

    def test_read_only_tools_are_not_reported_as_changes(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Read", "t1", file_path="/proj/a.py"),
                      use("Grep", "t2", pattern="x")),
            result("t1", text="contents"),
            result("t2", text="matches"),
        ])
        assert ev.collect_evidence(t, cwd="/proj").is_empty


# ══════════════════════════════════════════════════════════════════════════════
# 2. Test-runner summaries
# ══════════════════════════════════════════════════════════════════════════════


class TestTestParsing:
    @pytest.mark.parametrize("line,passed,failed,skipped,ok", [
        ("489 passed in 5.49s", 489, 0, 0, True),
        ("3 failed, 125 passed in 2.10s", 125, 3, 0, False),
        ("===== 12 passed, 2 skipped in 0.31s =====", 12, 0, 2, True),
        ("1 error, 4 passed", 4, 1, 0, False),
        ("2 failed, 8 passed, 1 skipped, 3 warnings in 12.00s", 8, 2, 1, False),
    ])
    def test_pytest_summaries(self, line, passed, failed, skipped, ok):
        r = ev._parse_test_line(line)
        assert r is not None and r.framework == "pytest"
        assert (r.passed, r.failed, r.skipped, r.ok) == (passed, failed, skipped, ok)

    @pytest.mark.parametrize("line", [
        "The refactor passed review, 3 files changed",
        "All 12 tests passed successfully after the fix",
        "note: 2 failed attempts were retried before it worked",
        "passed",
        "Retrying: 1 failed connection to the database host",
    ])
    def test_prose_mentioning_passed_or_failed_is_not_a_summary(self, line):
        """A card claiming '3 failed' because a log line said so is worse than
        a card with no test line at all."""
        assert ev._parse_test_line(line) is None

    def test_jest_summary(self):
        r = ev._parse_test_line("Tests:       2 failed, 10 passed, 12 total")
        assert r is not None and r.framework == "jest"
        assert (r.passed, r.failed, r.ok) == (10, 2, False)

    def test_cargo_summary(self):
        r = ev._parse_test_line(
            "test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured"
        )
        assert r is not None and r.framework == "cargo"
        assert (r.passed, r.skipped, r.ok) == (12, 1, True)

    def test_cargo_failure_is_not_ok(self):
        r = ev._parse_test_line("test result: FAILED. 8 passed; 2 failed; 0 ignored")
        assert r is not None and (r.failed, r.ok) == (2, False)

    def test_go_verdicts_carry_no_counts(self):
        ok = ev._parse_test_line("ok  \tgithub.com/me/pkg\t0.512s")
        bad = ev._parse_test_line("FAIL\tgithub.com/me/pkg\t0.210s")
        assert ok is not None and ok.framework == "go" and ok.ok is True
        assert bad is not None and bad.ok is False

    def test_bare_ok_is_not_a_go_result(self):
        assert ev._parse_test_line("ok") is None

    def test_test_output_is_read_from_command_stdout(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="pytest -q")),
            result("t1", structured=bash_out(stdout="....\n489 passed in 5.49s")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert [(r.framework, r.passed) for r in e.tests] == [("pytest", 489)]

    def test_only_the_last_run_of_a_framework_is_rendered(self, tmp_path):
        """Fix, re-run, green: the card reports green, not the failure it started
        with — but a second framework's result is never dropped."""
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="pytest")),
            result("t1", structured=bash_out(stdout="3 failed, 10 passed in 1.0s")),
            assistant(use("Bash", "t2", command="pytest")),
            result("t2", structured=bash_out(stdout="13 passed in 1.1s")),
            assistant(use("Bash", "t3", command="npm test")),
            result("t3", structured=bash_out(stdout="Tests: 4 passed, 4 total")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        shown = ev._latest_per_framework(e.tests)
        assert [(r.framework, r.passed, r.failed) for r in shown] == [
            ("pytest", 13, 0), ("jest", 4, 0),
        ]

    def test_failing_flag_tracks_red_suites(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="pytest")),
            result("t1", structured=bash_out(stdout="2 failed, 3 passed in 1.0s")),
        ])
        assert ev.collect_evidence(t, cwd="/proj").failing is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. Error snippets
# ══════════════════════════════════════════════════════════════════════════════


class TestErrorSnippets:
    def test_failed_command_yields_the_lines_that_name_a_cause(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="npm run build")),
            result("t1", is_error=True, structured=bash_out(
                stdout="compiling…\nlinking…",
                stderr="Error: Cannot find module 'left-pad'\n  at Module._resolve",
            )),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert len(e.errors) == 1
        assert e.errors[0].command == "npm run build"
        assert any("Cannot find module" in l for l in e.errors[0].lines)
        # Noise the command printed before it died is not the reason it died.
        assert not any("compiling" in l for l in e.errors[0].lines)

    def test_successful_command_produces_no_error(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="ls")),
            result("t1", structured=bash_out(stdout="a\nb")),
        ])
        assert ev.collect_evidence(t, cwd="/proj").errors == []

    def test_quiet_failure_falls_back_to_the_output_tail(self, tmp_path):
        """Nothing matched a cause marker, but the command still failed — show
        whatever it did say rather than an empty box."""
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="./deploy.sh")),
            result("t1", is_error=True, structured=bash_out(stdout="step 1\nstep 2")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert e.errors[0].lines == ["step 1", "step 2"]

    def test_error_lines_are_capped(self, tmp_path):
        many = "\n".join(f"Error line {i}" for i in range(50))
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="x")),
            result("t1", is_error=True, structured=bash_out(stderr=many)),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert len(e.errors[0].lines) == ev.MAX_ERROR_LINES
        # The tail, not the head: the last lines are the ones that name the cause.
        assert "Error line 49" in e.errors[0].lines[-1]

    def test_long_error_lines_are_truncated(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            assistant(use("Bash", "t1", command="x")),
            result("t1", is_error=True, structured=bash_out(stderr="Error: " + "y" * 500)),
        ])
        line = ev.collect_evidence(t, cwd="/proj").errors[0].lines[0]
        assert len(line) <= ev.MAX_ERROR_LINE_CHARS


# ══════════════════════════════════════════════════════════════════════════════
# 4. Turn scoping
# ══════════════════════════════════════════════════════════════════════════════


class TestTurnScoping:
    def test_only_the_last_turn_is_reported(self, tmp_path):
        """A card about the turn that just ended must not list every file
        touched since this morning."""
        t = write_transcript(tmp_path, [
            human("first ask"),
            assistant(use("Edit", "t1", file_path="/proj/old.py")),
            result("t1", structured=patch("+x")),
            human("second ask"),
            assistant(use("Edit", "t2", file_path="/proj/new.py")),
            result("t2", structured=patch("+y")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert [f.path for f in e.files] == ["new.py"]

    def test_tool_results_do_not_end_a_turn(self, tmp_path):
        """Tool outcomes are recorded as type=user. Treating them as the turn
        boundary would cut the window at the last tool call and lose the work."""
        t = write_transcript(tmp_path, [
            human("go"),
            assistant(use("Edit", "t1", file_path="/proj/a.py")),
            result("t1", structured=patch("+x")),
            assistant(use("Edit", "t2", file_path="/proj/b.py")),
            result("t2", structured=patch("+y")),
        ])
        e = ev.collect_evidence(t, cwd="/proj")
        assert sorted(f.path for f in e.files) == ["a.py", "b.py"]

    def test_transcript_with_no_human_turn_is_read_whole(self, tmp_path):
        t = write_transcript(tmp_path, [
            assistant(use("Edit", "t1", file_path="/proj/a.py")),
            result("t1", structured=patch("+x")),
        ])
        assert len(ev.collect_evidence(t, cwd="/proj").files) == 1

    def test_meta_entries_are_not_turn_boundaries(self, tmp_path):
        meta = {"type": "user", "isMeta": True,
                "message": {"content": [{"type": "text", "text": "<system>"}]}}
        t = write_transcript(tmp_path, [
            human("go"),
            assistant(use("Edit", "t1", file_path="/proj/a.py")),
            result("t1", structured=patch("+x")),
            meta,
            assistant(use("Edit", "t2", file_path="/proj/b.py")),
            result("t2", structured=patch("+y")),
        ])
        assert len(ev.collect_evidence(t, cwd="/proj").files) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 5. Robustness — this runs inside a Stop hook
# ══════════════════════════════════════════════════════════════════════════════


class TestRobustness:
    def test_missing_transcript_is_empty_not_an_error(self, tmp_path):
        assert ev.collect_evidence(None).is_empty
        assert ev.collect_evidence(tmp_path / "nope.jsonl").is_empty

    def test_corrupt_lines_are_skipped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text(
            "{not json\n"
            + json.dumps(human()) + "\n"
            + "]]]\n"
            + json.dumps(assistant(use("Edit", "t1", file_path="/proj/a.py"))) + "\n"
            + json.dumps(result("t1", structured=patch("+x"))) + "\n"
        )
        assert len(ev.collect_evidence(path, cwd="/proj").files) == 1

    def test_entries_with_unexpected_shapes_do_not_raise(self, tmp_path):
        t = write_transcript(tmp_path, [
            human(),
            {"type": "assistant", "message": {"content": "a bare string"}},
            {"type": "assistant", "message": {"content": [None, 42, {"type": "tool_use"}]}},
            {"type": "assistant"},
            assistant(use("Edit", "t1", file_path="/proj/a.py", new_string="x")),
        ])
        assert ev.collect_evidence(t, cwd="/proj").files[0].path == "a.py"

    def test_only_the_tail_of_a_huge_transcript_is_read(self, tmp_path):
        """A megabyte-scale JSONL must not be read whole on every Stop hook."""
        filler = [assistant(text_block("x" * 2000)) for _ in range(500)]
        t = write_transcript(tmp_path, [human()] + filler + [
            assistant(use("Edit", "t1", file_path="/proj/late.py")),
            result("t1", structured=patch("+x")),
        ])
        assert t.stat().st_size > ev.TAIL_BYTES
        e = ev.collect_evidence(t, cwd="/proj")
        assert [f.path for f in e.files] == ["late.py"]


# ══════════════════════════════════════════════════════════════════════════════
# 6. Rendering
# ══════════════════════════════════════════════════════════════════════════════


class TestRendering:
    def test_empty_evidence_renders_nothing(self):
        assert ev.Evidence().render() == ""

    def test_file_block_shows_totals_and_per_file_counts(self):
        e = ev.Evidence(files=[
            ev.FileChange("auth.py", 31, 8),
            ev.FileChange("tests/test_auth.py", 16, 0, created=True),
        ])
        out = e.render()
        assert "*2 files*" in out and "+47" in out and "−8" in out
        assert "`auth.py`" in out and "+31 −8" in out
        assert "*new*" in out

    def test_underscores_in_filenames_survive(self):
        """The bridge's _md() turns test_auth.py into 'test auth.py' to stop
        Markdown italicising it. Inside a code span nothing needs stripping, and
        the name stays searchable."""
        out = ev.Evidence(files=[ev.FileChange("tests/test_auth_flow.py", 3, 0)]).render()
        assert "`tests/test_auth_flow.py`" in out

    def test_backticks_in_a_path_cannot_break_the_code_span(self):
        out = ev.Evidence(files=[ev.FileChange("we`ird.py", 1, 0)]).render()
        assert "`we'ird.py`" in out
        assert out.count("`") % 2 == 0

    def test_long_file_lists_are_truncated_with_a_count(self):
        files = [ev.FileChange(f"f{i}.py", 1, 0) for i in range(9)]
        out = ev.Evidence(files=files).render()
        assert "*9 files*" in out
        assert "and 4 more" in out
        assert "f8.py" not in out

    def test_green_and_red_suites_are_distinguishable_at_a_glance(self):
        green = ev.Evidence(tests=[ev.TestResult("pytest", passed=128, skipped=1)]).render()
        red = ev.Evidence(
            tests=[ev.TestResult("pytest", passed=125, failed=3, ok=False)]
        ).render()
        assert green.startswith("✅") and "128 passed" in green and "1 skipped" in green
        assert red.startswith("❌") and "3 failed" in red

    def test_error_snippet_is_fenced(self):
        out = ev.Evidence(errors=[
            ev.ErrorSnippet("pytest -q", ["E   assert 3 == 4", "test_a.py:88: AssertionError"])
        ]).render()
        assert "```" in out and "assert 3 == 4" in out
        assert "`pytest -q`" in out

    def test_fences_inside_error_output_cannot_escape_the_block(self):
        out = ev.Evidence(errors=[ev.ErrorSnippet("x", ["oops ``` here"])]).render()
        assert out.count("```") == 2

    def test_a_full_card_reads_top_to_bottom(self):
        """The shape someone actually sees: what changed, whether it is green,
        and why not."""
        e = ev.Evidence(
            files=[ev.FileChange("auth.py", 31, 8)],
            tests=[ev.TestResult("pytest", passed=125, failed=3, ok=False)],
            errors=[ev.ErrorSnippet("pytest -q", ["E   assert 3 == 4"])],
        )
        out = e.render()
        assert out.index("📝") < out.index("❌ *pytest*") < out.index("❌ *Failed*")
