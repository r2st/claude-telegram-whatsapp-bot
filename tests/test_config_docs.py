"""The configuration reference must not drift from the code.

~60 environment variables were read by `telechat_pkg/` and documented nowhere
before `docs/configuration.md` existed. A hand-maintained list would simply
have decayed again, so the reference is generated from the source and these
tests are what stop it going stale:

  * every variable the code reads has a description,
  * no description survives the variable it describes,
  * the committed file matches what the generator produces right now.

Run:
    pytest tests/test_config_docs.py -v
    python scripts/env_reference.py      # to regenerate after adding a variable
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import env_reference  # noqa: E402


@pytest.fixture(scope="module")
def found():
    return env_reference.discover()


class TestReferenceIsComplete:
    def test_every_variable_the_code_reads_is_described(self, found):
        missing = env_reference.undocumented(found)
        assert not missing, (
            "These environment variables are read by telechat_pkg/ but have no "
            "description. Add them to DESCRIPTIONS in scripts/env_reference.py "
            "and re-run it:\n  " + "\n  ".join(missing)
        )

    def test_no_description_outlives_its_variable(self, found):
        stale = env_reference.orphaned(found)
        assert not stale, (
            "These are documented but no longer read by any module — delete "
            "them from scripts/env_reference.py:\n  " + "\n  ".join(stale)
        )

    def test_the_committed_file_matches_the_generator(self, found):
        current = env_reference.OUTPUT.read_text()
        assert current == env_reference.render(found), (
            "docs/configuration.md is out of date. Run: "
            "python scripts/env_reference.py"
        )

    def test_every_described_variable_has_a_real_section(self):
        sections = {title for title, _ in env_reference.SECTIONS}
        wrong = {
            name: section
            for name, (section, _) in env_reference.DESCRIPTIONS.items()
            if section not in sections
        }
        assert not wrong, f"Descriptions naming a section that doesn't exist: {wrong}"

    def test_descriptions_are_sentences_not_placeholders(self):
        bad = [
            name
            for name, (_, text) in env_reference.DESCRIPTIONS.items()
            if len(text) < 12 or not text.endswith((".", "?"))
        ]
        assert not bad, f"Descriptions too short or unpunctuated: {bad}"

    def test_the_security_sensitive_ones_say_so(self):
        """A reader skimming the table should not have to infer the risk."""
        for name, needle in [
            ("MCP_ALLOW_ANY_COMMAND", "allowlist"),
            ("WEB_CHAT_TRUST_PROXY", "proxy"),
            ("WEB_CHAT_ALLOW_OPEN", "unauthenticated"),
            ("BRIDGE_APPROVAL_TIMEOUT_ACTION", "fallthrough"),
        ]:
            _, text = env_reference.DESCRIPTIONS[name]
            assert needle in text, f"{name}'s description should mention {needle!r}"


class TestExtractor:
    """The scan itself — a missed pattern is a silently incomplete reference."""

    def _discover(self, tmp_path, source: str):
        (tmp_path / "mod.py").write_text(textwrap.dedent(source))
        return env_reference.discover(tmp_path)

    def test_finds_os_getenv(self, tmp_path):
        found = self._discover(tmp_path, 'import os\nX = os.getenv("SOME_VAR", "7")\n')
        assert found["SOME_VAR"]["default"] == "7"

    def test_finds_os_environ_get(self, tmp_path):
        found = self._discover(tmp_path, 'import os\nX = os.environ.get("OTHER_VAR", "on")\n')
        assert found["OTHER_VAR"]["default"] == "on"

    def test_finds_subscript_access(self, tmp_path):
        found = self._discover(tmp_path, 'import os\nX = os.environ["REQUIRED_VAR"]\n')
        assert "REQUIRED_VAR" in found
        assert found["REQUIRED_VAR"]["default"] is None

    def test_finds_dotenv_dict_reads(self, tmp_path):
        # desktop_bridge reads TELEGRAM_CHAT_ID this way and never touches
        # os.environ; a scan that missed it would report a complete reference.
        found = self._discover(tmp_path, 'def f(env):\n    return env.get("FILE_ONLY_VAR")\n')
        assert "FILE_ONLY_VAR" in found

    def test_records_which_modules_read_a_variable(self, tmp_path):
        (tmp_path / "a.py").write_text('import os\nos.getenv("SHARED")\n')
        (tmp_path / "b.py").write_text('import os\nos.getenv("SHARED")\n')
        found = env_reference.discover(tmp_path)
        assert found["SHARED"]["modules"] == {"a", "b"}

    def test_a_bare_read_does_not_erase_a_known_default(self, tmp_path):
        (tmp_path / "a.py").write_text('import os\nos.getenv("DUAL", "5")\n')
        (tmp_path / "b.py").write_text('import os\nos.getenv("DUAL")\n')
        found = env_reference.discover(tmp_path)
        assert found["DUAL"]["default"] == "5"

    def test_lowercase_and_computed_names_are_ignored(self, tmp_path):
        found = self._discover(tmp_path, textwrap.dedent('''
            import os
            os.getenv("lowercase_key")
            name = "COMPUTED"
            os.getenv(name)
            os.getenv(f"PREFIX_{name}")
        '''))
        assert found == {} or set(found) == set()

    def test_a_non_literal_default_is_reported_as_no_default(self, tmp_path):
        found = self._discover(tmp_path, 'import os\nD = "x"\nos.getenv("COMPUTED_DEFAULT", D)\n')
        assert found["COMPUTED_DEFAULT"]["default"] is None


class TestGeneratorOutput:
    def test_render_groups_every_variable_under_a_heading(self, found):
        text = env_reference.render(found)
        for name in found:
            assert f"| `{name}` |" in text

    def test_render_is_deterministic(self, found):
        assert env_reference.render(found) == env_reference.render(found)

    def test_check_mode_passes_against_the_committed_file(self):
        assert env_reference.main(["--check"]) == 0

    def test_the_script_parses_as_python(self):
        # It is executed by CI and by contributors directly; a syntax error here
        # would only surface at release time.
        source = (REPO_ROOT / "scripts" / "env_reference.py").read_text()
        ast.parse(source)


class TestTheExtractorIsShared:
    """One scan, two callers.

    `telechat doctor` flags settings in a user's `.env` that nothing reads, and
    the generator documents the settings that are read. Those have to be the
    same answer, or the doctor starts reporting documented variables as typos.
    """

    def test_the_generator_delegates_to_the_packaged_scan(self, found):
        from telechat_pkg import env_spec
        assert set(env_spec.discover(REPO_ROOT / "telechat_pkg",
                                     REPO_ROOT / "scripts")) == set(found)

    def test_the_package_scan_defaults_to_the_package(self):
        from telechat_pkg import env_spec
        names = env_spec.known_names()
        assert "TELEGRAM_BOT_TOKEN" in names
        # scripts/ is not scanned by default — the doctor exempts those keys
        # explicitly rather than pretending the package reads them.
        assert "WATCHDOG_SCAN_INTERVAL" not in names

    def test_the_doctor_accepts_every_documented_setting(self):
        # The failure this prevents: `telechat doctor` telling a user that a
        # variable straight out of the reference is unknown.
        from telechat_pkg import doctor, env_spec
        known = env_spec.known_names() | doctor._EXTERNAL_ENV_KEYS
        documented = set(env_reference.DESCRIPTIONS)
        assert not (documented - known), sorted(documented - known)

    def test_every_env_example_key_passes_the_doctor(self):
        from telechat_pkg import doctor, env_spec
        known = env_spec.known_names() | doctor._EXTERNAL_ENV_KEYS
        declared = {
            line.split("=", 1)[0].strip()
            for line in (REPO_ROOT / ".env.example").read_text().splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        unknown = {k for k in declared if k.isupper()} - known
        assert not unknown, (
            "the shipped .env.example would make `telechat doctor` report "
            f"these as unknown settings: {sorted(unknown)}"
        )
