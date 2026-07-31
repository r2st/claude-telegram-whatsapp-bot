import json
import os
import tempfile
import threading

import pytest

_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "test_coder.db")

from telechat_pkg.coder import (
    _load,
    _save,
    get_project,
    set_project,
    clear_project,
    build_task_prompt,
    CODER_SYSTEM,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def isolated_projects_path(tmp_path, monkeypatch):
    """Redirect _PROJECTS_PATH to a temp file for every test."""
    monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", tmp_path / "projects.json")


def _projects_path(tmp_path):
    return tmp_path / "projects.json"


# ══════════════════════════════════════════════════════════════════════════════
# 1. _load
# ══════════════════════════════════════════════════════════════════════════════


class TestLoad:
    def test_missing_file_returns_empty_dict(self):
        result = _load()
        assert result == {}

    def test_valid_json_loads_correctly(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        data = {"tg:123": "/home/user/myproject"}
        p.write_text(json.dumps(data))
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert _load() == data

    def test_invalid_json_returns_empty_dict(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text("NOT { valid JSON !!!")
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert _load() == {}

    def test_empty_file_returns_empty_dict(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text("")
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert _load() == {}

    def test_permission_error_returns_empty_dict(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text(json.dumps({"a": "b"}))
        p.chmod(0o000)
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        try:
            result = _load()
            assert result == {}
        finally:
            p.chmod(0o644)

    def test_multiple_entries_all_loaded(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        data = {"tg:1": "/a", "slack:2": "/b", "wa:3": "/c"}
        p.write_text(json.dumps(data))
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert _load() == data


# ══════════════════════════════════════════════════════════════════════════════
# 2. _save
# ══════════════════════════════════════════════════════════════════════════════


class TestSave:
    def test_writes_valid_json(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        data = {"tg:99": "/srv/project"}
        _save(data)
        assert json.loads(p.read_text()) == data

    def test_pretty_prints_json(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        _save({"x": "y"})
        raw = p.read_text()
        assert "\n" in raw  # indent=2 produces newlines

    def test_os_error_silently_caught(self, tmp_path, monkeypatch):
        p = tmp_path / "no_such_dir" / "projects.json"
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        # Should not raise
        _save({"k": "v"})

    def test_overwrites_existing_data(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        _save({"old": "data"})
        _save({"new": "data"})
        assert json.loads(p.read_text()) == {"new": "data"}

    def test_concurrent_writes_no_exception(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        errors = []

        def worker(i):
            try:
                _save({f"k{i}": f"v{i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # File should contain valid JSON after concurrent writes
        assert isinstance(json.loads(p.read_text()), dict)


# ══════════════════════════════════════════════════════════════════════════════
# 3. get_project
# ══════════════════════════════════════════════════════════════════════════════


class TestGetProject:
    def test_returns_none_when_no_project_set(self):
        assert get_project("tg", "42") is None

    def test_returns_none_for_unknown_platform(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text(json.dumps({"slack:42": "/some/path"}))
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert get_project("tg", "42") is None

    def test_returns_path_when_set(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text(json.dumps({"tg:7": "/home/user/repo"}))
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert get_project("tg", "7") == "/home/user/repo"

    def test_different_platforms_isolated(self, tmp_path, monkeypatch):
        p = tmp_path / "projects.json"
        p.write_text(json.dumps({"tg:1": "/tg-path", "slack:1": "/slack-path"}))
        monkeypatch.setattr("telechat_pkg.coder._PROJECTS_PATH", p)
        assert get_project("tg", "1") == "/tg-path"
        assert get_project("slack", "1") == "/slack-path"


# ══════════════════════════════════════════════════════════════════════════════
# 4. set_project
# ══════════════════════════════════════════════════════════════════════════════


class TestSetProject:
    def test_valid_dir_returns_true_and_path(self, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        ok, result = set_project("tg", "1", str(project_dir))
        assert ok is True
        assert result == str(project_dir)

    def test_nonexistent_dir_returns_false_with_message(self, tmp_path):
        bad_path = str(tmp_path / "doesnotexist")
        ok, msg = set_project("tg", "1", bad_path)
        assert ok is False
        assert "Not a directory" in msg

    def test_file_path_returns_false(self, tmp_path):
        f = tmp_path / "afile.txt"
        f.write_text("hello")
        ok, msg = set_project("tg", "1", str(f))
        assert ok is False
        assert "Not a directory" in msg

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        real_home = tmp_path / "fakehome"
        real_home.mkdir()
        monkeypatch.setenv("HOME", str(real_home))
        ok, result = set_project("tg", "1", "~")
        assert ok is True
        assert result == str(real_home)

    def test_whitespace_stripped_from_path(self, tmp_path):
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        padded = "  " + str(project_dir) + "  "
        ok, result = set_project("tg", "1", padded)
        assert ok is True
        assert result == str(project_dir)

    def test_path_persisted_after_set(self, tmp_path):
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        set_project("tg", "55", str(project_dir))
        assert get_project("tg", "55") == str(project_dir)

    def test_path_is_absolute_after_set(self, tmp_path, monkeypatch):
        # Give a relative path and verify the stored result is absolute
        project_dir = tmp_path / "relrepo"
        project_dir.mkdir()
        monkeypatch.chdir(tmp_path)
        ok, result = set_project("tg", "1", "relrepo")
        assert ok is True
        assert os.path.isabs(result)

    def test_overwrite_existing_project(self, tmp_path):
        d1 = tmp_path / "proj1"
        d2 = tmp_path / "proj2"
        d1.mkdir()
        d2.mkdir()
        set_project("tg", "1", str(d1))
        ok, result = set_project("tg", "1", str(d2))
        assert ok is True
        assert get_project("tg", "1") == str(d2)


# ══════════════════════════════════════════════════════════════════════════════
# 5. clear_project
# ══════════════════════════════════════════════════════════════════════════════


class TestClearProject:
    def test_removes_existing_project(self, tmp_path):
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        set_project("tg", "10", str(project_dir))
        assert get_project("tg", "10") is not None
        clear_project("tg", "10")
        assert get_project("tg", "10") is None

    def test_clearing_nonexistent_is_noop(self):
        # Must not raise
        clear_project("tg", "999")

    def test_clear_only_affects_target_user(self, tmp_path):
        d1 = tmp_path / "r1"
        d2 = tmp_path / "r2"
        d1.mkdir()
        d2.mkdir()
        set_project("tg", "1", str(d1))
        set_project("tg", "2", str(d2))
        clear_project("tg", "1")
        assert get_project("tg", "1") is None
        assert get_project("tg", "2") == str(d2)

    def test_clear_persists_across_load(self, tmp_path):
        project_dir = tmp_path / "repo"
        project_dir.mkdir()
        set_project("slack", "5", str(project_dir))
        clear_project("slack", "5")
        # Verify by re-loading from disk
        assert _load().get("slack:5") is None


# ══════════════════════════════════════════════════════════════════════════════
# 6. build_task_prompt
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildTaskPrompt:
    def test_contains_project_dir(self, tmp_path):
        result = build_task_prompt("fix the bug", str(tmp_path))
        assert str(tmp_path) in result

    def test_contains_task_text(self):
        result = build_task_prompt("add unit tests", "/some/dir")
        assert "add unit tests" in result

    def test_contains_workflow_keywords(self):
        result = build_task_prompt("refactor auth", "/some/dir")
        assert "EXPLORE" in result
        assert "PLAN" in result
        assert "IMPLEMENT" in result
        assert "TEST" in result
        assert "REVIEW" in result
        assert "REPORT" in result

    def test_strips_task_whitespace(self):
        result = build_task_prompt("  my task  ", "/some/dir")
        assert "my task" in result
        # The stripped version should appear, not the padded one
        assert "  my task  " not in result

    def test_project_dir_label_present(self):
        result = build_task_prompt("do work", "/proj")
        assert "working directory" in result.lower() or "Project working directory" in result


# ══════════════════════════════════════════════════════════════════════════════
# 7. CODER_SYSTEM
# ══════════════════════════════════════════════════════════════════════════════


class TestCoderSystem:
    def test_is_a_nonempty_string(self):
        assert isinstance(CODER_SYSTEM, str)
        assert len(CODER_SYSTEM) > 0

    def test_contains_explore_step(self):
        assert "EXPLORE" in CODER_SYSTEM

    def test_contains_plan_step(self):
        assert "PLAN" in CODER_SYSTEM

    def test_contains_implement_step(self):
        assert "IMPLEMENT" in CODER_SYSTEM

    def test_contains_test_step(self):
        assert "TEST" in CODER_SYSTEM

    def test_contains_review_step(self):
        assert "REVIEW" in CODER_SYSTEM

    def test_contains_report_step(self):
        assert "REPORT" in CODER_SYSTEM

    def test_mentions_working_directory(self):
        assert "working directory" in CODER_SYSTEM.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 8. PipelineStage + PipelineTracker
# ══════════════════════════════════════════════════════════════════════════════

from telechat_pkg.coder import PipelineStage, PipelineTracker
from unittest.mock import MagicMock


class TestPipelineStage:
    def test_exploring_tuple(self):
        assert PipelineStage.EXPLORING == ("exploring", "🔍 Exploring")

    def test_done_tuple(self):
        assert PipelineStage.DONE == ("done", "✅ Done")


class TestPipelineTracker:
    def test_initial_state(self):
        t = PipelineTracker()
        assert t.current_stage == PipelineStage.EXPLORING

    def test_tool_read_stays_exploring(self):
        t = PipelineTracker()
        sid, label = t.on_tool("Read")
        assert sid == "exploring"

    def test_tool_grep_stays_exploring(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Grep")
        assert sid == "exploring"

    def test_tool_listdir_stays_exploring(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("ListDir")
        assert sid == "exploring"

    def test_tool_write_moves_to_coding(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Write")
        assert sid == "coding"

    def test_tool_edit_moves_to_coding(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Edit")
        assert sid == "coding"

    def test_tool_todowrite_planning(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("TodoWrite")
        assert sid == "planning"

    def test_tool_todoread_planning(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("TodoRead")
        assert sid == "planning"

    def test_tool_agent_coding(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Agent")
        assert sid == "coding"

    def test_bash_test_command(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Bash", "npm test")
        assert sid == "testing"

    def test_bash_pytest_command(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Bash", "pytest tests/")
        assert sid == "testing"

    def test_bash_lint_command(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Bash", "ruff check .")
        assert sid == "reviewing"

    def test_bash_deploy_command(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Bash", "git push origin main")
        assert sid == "deploying"

    def test_bash_install_command(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("Bash", "pip install requests")
        assert sid == "coding"

    def test_bash_unknown_defaults_to_current(self):
        t = PipelineTracker()
        t.current_stage = PipelineStage.EXPLORING
        sid, _ = t.on_tool("Bash", "echo hello")
        assert sid == "exploring"

    def test_bash_unknown_after_coding_goes_to_testing(self):
        t = PipelineTracker()
        t.current_stage = PipelineStage.CODING
        sid, _ = t.on_tool("Bash", "echo hello")
        assert sid == "testing"

    def test_unknown_tool_stays_current(self):
        t = PipelineTracker()
        t.current_stage = PipelineStage.CODING
        sid, _ = t.on_tool("SomeUnknown")
        assert sid == "coding"

    def test_fix_loop_detection(self):
        t = PipelineTracker()
        t.on_tool("Write")  # coding
        t.on_tool("Bash", "pytest")  # testing
        assert t._test_count == 1
        sid, _ = t.on_tool("Edit")  # back to coding → fixing
        assert sid == "fixing"
        assert t._fix_count == 1

    def test_on_error_returns_summary(self):
        t = PipelineTracker()
        s = t.on_error("TypeError: cannot read 'x'")
        assert "type" in s.lower() or "error" in s.lower()

    def test_on_success_records_clean(self):
        t = PipelineTracker()
        t.on_success()
        assert t.convergence.check().status == "progressing"

    def test_convergence_warning_none(self):
        t = PipelineTracker()
        assert t.get_convergence_warning() is None

    def test_convergence_warning_oscillating(self):
        t = PipelineTracker()
        t.on_error("SyntaxError: invalid syntax in file.py")
        t.on_error("SyntaxError: invalid syntax in file.py")
        w = t.get_convergence_warning()
        assert w is not None
        assert "oscillat" in w.lower()

    def test_convergence_warning_stuck(self):
        t = PipelineTracker()
        t.convergence = MagicMock()
        t.convergence.check.return_value = MagicMock(
            status="stuck", reason="No progress", action="replan"
        )
        w = t.get_convergence_warning()
        assert "Stuck" in w

    def test_convergence_warning_diverging(self):
        t = PipelineTracker()
        t.convergence = MagicMock()
        t.convergence.check.return_value = MagicMock(
            status="diverging", reason="Errors up", action="escalate"
        )
        w = t.get_convergence_warning()
        assert "Diverging" in w

    def test_stage_summary_basic(self):
        t = PipelineTracker()
        s = t.stage_summary()
        assert "Exploring" in s

    def test_stage_summary_with_fix_count(self):
        t = PipelineTracker()
        t._fix_count = 3
        assert "fix attempt 3" in t.stage_summary()

    def test_stage_summary_with_error(self):
        t = PipelineTracker()
        t._last_error = "🔤 syntax error"
        assert "syntax" in t.stage_summary()

    def test_pipeline_bar_initial(self):
        t = PipelineTracker()
        bar = t.pipeline_bar()
        assert "▶" in bar

    def test_pipeline_bar_after_progress(self):
        t = PipelineTracker()
        t.on_tool("Read")
        t.on_tool("Write")
        t.on_tool("Bash", "pytest")
        bar = t.pipeline_bar()
        assert "✓" in bar
        assert "▶" in bar

    def test_stage_history_records(self):
        t = PipelineTracker()
        t.on_tool("Read")
        t.on_tool("Write")
        assert len(t.stage_history) == 2
        assert t.stage_history[0][0] == "exploring"
        assert t.stage_history[1][0] == "coding"

    def test_bash_no_detail_stays_current(self):
        """Bash with empty detail and None stage → stays current."""
        t = PipelineTracker()
        t.current_stage = PipelineStage.PLANNING
        sid, _ = t.on_tool("Bash", "")
        # Bash with no detail: stage=None from map, so stays current
        assert sid == "planning"

    def test_websearch_exploring(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("WebSearch")
        assert sid == "exploring"

    def test_webfetch_exploring(self):
        t = PipelineTracker()
        sid, _ = t.on_tool("WebFetch")
        assert sid == "exploring"
