"""One version number, three consumers (docs/improvements.md item 4).

pyproject.toml, telechat_pkg/__init__.py, and npm/package.json all disagreed at
once — 1.2.0, 1.1.5, and 1.1.1 respectively — and all three shipped in the same
release. The consequences were concrete: `updater.current_version()` read the
installed metadata, `mcp_client` reported `__version__`, and `telechat --version`
printed the wrapper's number, so one install described itself three ways. Worse,
the updater compares its number against *both* the PyPI and npm registries and
nags when either is higher, so an npm-installed user on the newest release was
told to update permanently — the wrapper's version was two minors behind the
package it launches.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no version"
    return m.group(1)


def _npm_version() -> str:
    return json.loads((REPO_ROOT / "npm" / "package.json").read_text())["version"]


class TestVersionConsistency:
    def test_npm_matches_pyproject(self):
        assert _npm_version() == _pyproject_version(), (
            "npm/package.json is out of sync — run bash scripts/sync-version.sh"
        )

    def test_package_version_matches_pyproject(self):
        import telechat_pkg

        assert telechat_pkg.__version__ == _pyproject_version()

    def test_updater_and_package_agree(self):
        # These two are what the update-nag compares; if they can differ, the nag
        # can fire against a version the user does not actually have.
        import telechat_pkg
        from telechat_pkg import updater

        assert updater.current_version() == telechat_pkg.__version__

    def test_version_is_not_the_unknown_fallback(self):
        import telechat_pkg

        assert telechat_pkg.__version__ != "0.0.0"

    def test_version_is_pep440_ish(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+([.\-+].*)?", _pyproject_version())


class TestSyncScript:
    def test_check_mode_passes_on_a_synced_tree(self):
        r = subprocess.run(
            ["bash", str(REPO_ROOT / "scripts" / "sync-version.sh"), "--check"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr

    def test_check_mode_fails_on_a_diverged_tree(self, tmp_path):
        # The check is only worth having if it actually catches divergence, which
        # is the state the tree shipped in for several releases.
        fake = tmp_path / "repo"
        (fake / "npm").mkdir(parents=True)
        (fake / "scripts").mkdir()
        (fake / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
        (fake / "npm" / "package.json").write_text('{\n  "version": "1.0.0"\n}\n')
        script = (fake / "scripts" / "sync-version.sh")
        script.write_text((REPO_ROOT / "scripts" / "sync-version.sh").read_text())

        r = subprocess.run(
            ["bash", str(script), "--check"], capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "mismatch" in r.stdout.lower()

    def test_write_mode_brings_npm_into_line(self, tmp_path):
        fake = tmp_path / "repo"
        (fake / "npm").mkdir(parents=True)
        (fake / "scripts").mkdir()
        (fake / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
        # Formatting and key order around the version must survive the rewrite.
        (fake / "npm" / "package.json").write_text(
            '{\n  "name": "telechat",\n  "version": "1.0.0",\n  "bin": {"telechat": "bin/telechat.js"}\n}\n'
        )
        script = (fake / "scripts" / "sync-version.sh")
        script.write_text((REPO_ROOT / "scripts" / "sync-version.sh").read_text())

        r = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr

        raw = (fake / "npm" / "package.json").read_text()
        assert json.loads(raw)["version"] == "9.9.9"
        assert '"name": "telechat"' in raw          # untouched
        assert list(json.loads(raw)) == ["name", "version", "bin"]  # order kept

        # And it now passes its own check.
        r = subprocess.run(["bash", str(script), "--check"], capture_output=True, text=True)
        assert r.returncode == 0


class TestAgentsMdCoverageClaim:
    """Item 28: AGENTS.md told every agent coverage was ~99% when it was 84%.

    That matters more than a stale number normally would, because AGENTS.md is the
    file every agent reads first and a believed-99% figure actively suppresses the
    test-writing this tree most needs.
    """

    def test_agents_md_no_longer_quotes_a_coverage_percentage(self):
        text = (REPO_ROOT / "AGENTS.md").read_text()
        # A hardcoded percentage is what drifted; CI's enforced floor replaces it.
        assert "99%" not in text
        assert not re.search(r"~?\d{2}% coverage", text), (
            "AGENTS.md quotes a coverage percentage again — it will drift. "
            "Point at the CI floor instead."
        )

    def test_agents_md_points_at_the_enforced_floor(self):
        text = (REPO_ROOT / "AGENTS.md").read_text()
        assert "pytest.yml" in text or "floor" in text

    def test_the_ci_floor_exists_and_is_a_real_number(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "pytest.yml").read_text()
        m = re.search(r"--cov-fail-under=(\d+)", workflow)
        assert m, "no coverage floor configured"
        assert 50 <= int(m.group(1)) <= 100
