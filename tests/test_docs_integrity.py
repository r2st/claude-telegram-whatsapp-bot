"""The documentation must describe the repository that exists.

Docs rot silently: a link to a moved file, a section pointing at a script that
was renamed, a README promising a command that no longer runs. None of that
fails a build unless something checks it, and this project has already been
bitten — `AGENTS.md` claimed ~99% coverage against a real 84%, and
`.env.example` documented two settings the code never read.

These tests are cheap and only assert things that are actually true of the
tree, so they stay green until someone breaks the docs.

Run:
    pytest tests/test_docs_integrity.py -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Markdown files whose relative links must resolve.
LINKED_DOCS = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "AGENTS.md",
    "CHANGELOG.md",
]

#: `[text](target)` where target is not a URL, a mailto:, or a bare anchor.
_LINK = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)]+)\)")


def _relative_links(path: Path) -> list[str]:
    return [m.group(1).split("#", 1)[0] for m in _LINK.finditer(path.read_text())]


class TestConventionFilesExist:
    """The files a human contributor looks for first."""

    @pytest.mark.parametrize("name", [
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "AGENTS.md",
    ])
    def test_present(self, name):
        assert (REPO_ROOT / name).is_file(), f"{name} is missing"

    @pytest.mark.parametrize("name", [
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
    ])
    def test_github_templates_present(self, name):
        assert (REPO_ROOT / name).is_file(), f"{name} is missing"

    def test_the_changelog_has_an_unreleased_section(self):
        # Where a user-visible change goes between releases. Without it people
        # append to the last released version and the release notes lie.
        assert "## [Unreleased]" in (REPO_ROOT / "CHANGELOG.md").read_text()

    def test_the_sdist_ships_the_convention_files(self):
        manifest = (REPO_ROOT / "MANIFEST.in").read_text()
        for name in ("CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "LICENSE"):
            assert f"include {name}" in manifest, f"MANIFEST.in does not ship {name}"


class TestLinksResolve:
    @pytest.mark.parametrize("doc", LINKED_DOCS)
    def test_relative_links_point_at_files_that_exist(self, doc):
        path = REPO_ROOT / doc
        if not path.is_file():
            pytest.skip(f"{doc} not present")
        broken = [
            target for target in _relative_links(path)
            if not (REPO_ROOT / target).exists()
            # ../blob/main/X is a GitHub-relative link from .github/, not a path.
            and not target.startswith("../")
        ]
        assert not broken, f"{doc} links to files that do not exist: {broken}"


class TestReferencedPathsExist:
    """Docs that name a script or directory must name a real one."""

    @pytest.mark.parametrize("referenced", [
        "scripts/dev-setup.sh",
        "scripts/env_reference.py",
        "scripts/watchdog.py",
        "scripts/publish.sh",
        "scripts/sync-version.sh",
        "agents/check-overlap.sh",
        "agents/_template.md",
        "docs/configuration.md",
        "docs/improvements.md",
        "docs/architecture.md",
    ])
    def test_path_exists(self, referenced):
        assert (REPO_ROOT / referenced).exists(), f"docs reference a missing {referenced}"

    def test_the_dev_setup_script_is_executable(self):
        # CONTRIBUTING tells people to run `./scripts/dev-setup.sh` directly.
        script = REPO_ROOT / "scripts" / "dev-setup.sh"
        assert script.stat().st_mode & 0o111, "scripts/dev-setup.sh is not executable"


class TestCIMatchesTheDocs:
    """Commands the docs promise are the commands CI actually runs."""

    @pytest.fixture(scope="class")
    @classmethod
    def workflow(cls):
        return (REPO_ROOT / ".github" / "workflows" / "pytest.yml").read_text()

    @pytest.mark.parametrize("command", [
        "ruff check .",
        "pytest -q",
        "python scripts/env_reference.py --check",
        "bash scripts/sync-version.sh --check",
    ])
    def test_ci_runs_it(self, workflow, command):
        assert command in workflow, f"CONTRIBUTING promises `{command}`; CI does not run it"

    def test_contributing_names_the_checks_a_pr_must_pass(self):
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        for command in ("pytest -q", "ruff check ."):
            assert command in text
