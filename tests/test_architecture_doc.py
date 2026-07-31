"""`docs/architecture.md` must describe *this* program.

Until 2026-07-31 it described a different one entirely — `src/main.py`,
FastAPI, APScheduler, `claude-agent-sdk`, an entry point called
`claude-telegram-bot`. Not one module it named existed here, and `AGENTS.md`
points every agent at it as the canonical architecture overview, so each of
them started from fiction.

A prose document can't be generated the way `configuration.md` is, but the
claims it makes about *names* can be checked. That is what these tests do: every
module, function, table, and environment variable the document names in code
formatting has to exist. A rename that leaves the doc behind fails here rather
than misleading the next reader.

Run:
    pytest tests/test_architecture_doc.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "architecture.md"
PACKAGE = REPO_ROOT / "telechat_pkg"


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text()


@pytest.fixture(scope="module")
def body(text) -> str:
    """The document minus its opening note.

    That note quotes the old document's module names on purpose — to say they
    do not exist — so the ghost check has to read past it.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.startswith(">")
    )


@pytest.fixture(scope="module")
def package_modules() -> set[str]:
    return {p.name for p in PACKAGE.glob("*.py")}


@pytest.fixture(scope="module")
def package_symbols() -> set[str]:
    """Every top-level function and class defined anywhere in the package."""
    names: set[str] = set()
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


class TestItDescribesThisRepository:
    def test_no_reference_to_the_program_it_used_to_describe(self, body):
        # These are the fingerprints of the old document. Any of them coming
        # back means someone restored the wrong file.
        for ghost in (
            "src/main.py", "src/bot/", "src/claude/", "src/storage/",
            "FastAPI", "APScheduler", "aiosqlite", "claude-agent-sdk",
            "claude-telegram-bot", "ClaudeIntegration", "MessageOrchestrator",
        ):
            assert ghost not in body, (
                f"architecture.md still references {ghost!r}, which does not "
                "exist in this repository"
            )

    def test_it_names_the_real_entry_point(self, text):
        assert "telechat_pkg/main.py" in text or "`main.py`" in text
        assert "cli_entry" in text

    @pytest.mark.parametrize("module", [
        "claude_core.py", "store.py", "telegram_bot.py", "whatsapp_bot.py",
        "slack_bot.py", "web_chat.py", "desktop_bridge.py", "health.py",
        "session_manager.py", "memory.py", "knowledge_base.py", "models.py",
    ])
    def test_the_load_bearing_modules_are_covered(self, text, module):
        assert module in text, f"architecture.md never mentions {module}"


class TestEveryNameItUsesExists:
    def test_every_module_it_names_exists(self, text, package_modules):
        # Backticked `something.py` tokens that look like package modules.
        named = set(re.findall(r"`([a-z_]+\.py)`", text))
        # Files outside telechat_pkg/ that the doc legitimately names.
        elsewhere = {
            "main.py", "env_reference.py", "watchdog.py", "publish.sh",
        }
        missing = {
            m for m in named
            if m not in package_modules and m not in elsewhere
        }
        assert not missing, f"architecture.md names modules that don't exist: {sorted(missing)}"

    def test_every_function_it_names_exists(self, text, package_symbols):
        named = set(re.findall(r"`([a-z_][a-z0-9_]{3,})\(\)`", text))
        missing = {n for n in named if n not in package_symbols}
        assert not missing, (
            f"architecture.md names functions that don't exist: {sorted(missing)}"
        )

    def test_every_table_it_names_exists_in_a_schema(self, text):
        schema_sql = "\n".join(
            p.read_text() for p in PACKAGE.glob("*.py")
        )
        # Table names from the persistence table, e.g. | `conversations` |
        named = set(re.findall(r"^\| `([a-z_]+)`", text, re.MULTILINE))
        # `bridge_*` is a deliberate wildcard for the six bridge tables.
        named = {t for t in named if not t.endswith("_")}
        missing = {
            t for t in named
            if f"TABLE IF NOT EXISTS {t}" not in schema_sql
            and f"TABLE {t}" not in schema_sql
        }
        assert not missing, (
            f"architecture.md names tables no schema creates: {sorted(missing)}"
        )

    def test_every_environment_variable_it_names_is_read_by_the_code(self, text):
        import sys
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import env_reference

        known = set(env_reference.discover())
        named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text))
        # Values, log levels, and module constants — not settings.
        not_variables = {"WAL", "DEBUG", "INFO", "WARNING", "TABLE", "BOT_COMMANDS"}
        missing = named - known - not_variables
        assert not missing, (
            f"architecture.md names settings the code never reads: {sorted(missing)}"
        )


class TestTheClaimsItMakesAboutStructure:
    def test_the_three_claude_modes_are_the_ones_that_exist(self, text):
        source = (PACKAGE / "claude_core.py").read_text()
        for mode in ("cli", "api", "sdk"):
            assert f"**`{mode}`**" in text, f"architecture.md omits CLAUDE_MODE={mode}"
        assert "cli | api | sdk" in source, (
            "claude_core no longer documents three modes; the doc says it does"
        )

    def test_web_is_a_real_platform(self, text):
        # The old doc predated BOT_MODE=web and never mentioned it.
        assert "Web chat" in text
        assert '"web"' in (PACKAGE / "main.py").read_text()

    def test_the_adapters_it_lists_all_have_a_runner(self, text, package_symbols):
        for runner in ("run_telegram", "run_whatsapp", "run_slack", "run_web_chat"):
            assert runner in package_symbols
            assert runner in text, f"architecture.md omits {runner}"

    def test_it_points_at_the_generated_configuration_reference(self, text):
        # The doc must not grow its own drifting copy of the settings table.
        assert "configuration.md" in text
        assert (REPO_ROOT / "docs" / "configuration.md").exists()

    def test_agents_md_still_points_here(self):
        # If this file is ever renamed, the pointer has to move with it.
        agents = (REPO_ROOT / "AGENTS.md").read_text()
        assert "docs/architecture.md" in agents
