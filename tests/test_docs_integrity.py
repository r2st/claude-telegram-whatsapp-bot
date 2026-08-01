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
    "docs/desktop-bridge.md",
]

#: `[text](target)` where target is not a URL, a mailto:, or a bare anchor.
_LINK = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)]+)\)")


def _relative_links(path: Path) -> list[str]:
    """Link targets, resolved the way a Markdown renderer resolves them.

    Relative to the *document's own directory*, not the repo root — a doc under
    `docs/` linking to `configuration.md` means its sibling. Resolving from the
    root instead made every intra-docs link look broken.
    """
    return [
        (path.parent / m.group(1).split("#", 1)[0]).resolve()
        for m in _LINK.finditer(path.read_text())
        if not m.group(1).startswith("../")
    ]


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
        # `../blob/main/X` is a GitHub-relative link, not a path — _relative_links
        # drops those.
        broken = [t for t in _relative_links(path) if not t.exists()]
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
        "docs/desktop-bridge.md",
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


class TestReadmeLeadsWithTheProduct:
    """The README is the second thing a prospective user reads, after the site.

    The Desktop bridge is the one capability no other Claude-on-Telegram bot
    has, and it used to appear at line 393 of a 523-line file — below three
    platform setup walkthroughs, absent from the opening and near the bottom of
    the feature list. Ordering is positioning, and positioning drifts back the
    moment nothing checks it (`docs/improvements.md` item 9).
    """

    @pytest.fixture(scope="class")
    @classmethod
    def readme(cls) -> str:
        return (REPO_ROOT / "README.md").read_text()

    def test_the_bridge_is_in_the_opening(self, readme):
        opening = readme.split("## Install", 1)[0]
        assert "Desktop bridge" in opening, (
            "the README's opening does not mention the differentiator"
        )

    def test_the_bridge_section_precedes_the_setup_walkthroughs(self, readme):
        bridge = readme.index("## Claude Desktop bridge")
        setup = readme.index("### 2a — Telegram setup")
        assert bridge < setup, (
            "the bridge section is buried below the per-platform setup guides"
        )

    def test_the_bridge_leads_the_feature_list(self, readme):
        features = readme.split("## Features", 1)[1]
        first = next(ln for ln in features.splitlines() if ln.startswith("- "))
        assert "Desktop bridge" in first, f"the feature list opens with: {first}"

    def test_the_landing_page_is_linked(self, readme):
        assert "https://telechat.fyi" in readme, "the README never links the website"

    def test_badges_point_at_the_published_packages(self, readme):
        header = readme.split("## Install", 1)[0]
        for url in (
            "https://www.npmjs.com/package/telechat",
            "https://pypi.org/project/telechatai/",
        ):
            assert url in header, f"no badge links {url}"

    def test_the_ci_badge_names_a_workflow_that_exists(self, readme):
        for workflow in re.findall(r"actions/workflows/([\w.-]+)/badge\.svg", readme):
            assert (REPO_ROOT / ".github" / "workflows" / workflow).is_file(), (
                f"the README shows a badge for {workflow}, which does not exist"
            )


@pytest.fixture(scope="module")
def guide():
    return (REPO_ROOT / "docs" / "desktop-bridge.md").read_text()


@pytest.fixture(scope="module")
def source():
    return (REPO_ROOT / "telechat_pkg" / "desktop_bridge.py").read_text()


class TestBridgeGuideMatchesTheCode:
    """The bridge guide is the page someone reads when nothing is working.

    A stale command or a hook name that no longer fires costs them the one
    thing they came for, so the claims it makes are checked against the source
    rather than trusted.
    """

    def test_every_bridge_subcommand_it_teaches_is_dispatched(self, guide, source):
        taught = set(re.findall(r"telechat bridge ([a-z]+)", guide))
        for sub in taught:
            assert f'sub == "{sub}"' in source or f'"{sub}": ' in source, (
                f"the guide teaches `telechat bridge {sub}`, which cli_dispatch doesn't handle"
            )

    def test_the_hook_events_it_documents_are_the_ones_installed(self, guide, source):
        from telechat_pkg.desktop_bridge import NOTIFY_EVENTS
        for event in NOTIFY_EVENTS:
            assert event in guide, f"the guide omits the {event} hook"
        assert "PreToolUse" in guide

    def test_the_env_vars_it_documents_are_read_by_the_code(self, guide):
        source = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (REPO_ROOT / "telechat_pkg").glob("*.py")
        )
        for name in set(re.findall(r"`(BRIDGE_[A-Z_]+|CLAUDE_CODE_OAUTH_TOKEN)`", guide)):
            assert name in source, f"the guide documents {name}, which no module reads"

    def test_it_has_a_troubleshooting_table(self, guide):
        # The symptom→cause table is the reason this file exists as a separate
        # page instead of another README section.
        assert "## Troubleshooting" in guide
        assert guide.count("|") > 40, "the troubleshooting table lost its rows"

    def test_the_readme_points_at_it(self):
        assert "docs/desktop-bridge.md" in (REPO_ROOT / "README.md").read_text()
