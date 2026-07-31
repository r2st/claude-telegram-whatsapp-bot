"""Which environment variables telechat actually reads, derived from its source.

Two callers, one answer:

* ``scripts/env_reference.py`` generates ``docs/configuration.md`` from this.
* :mod:`telechat_pkg.doctor` uses it to flag keys in a user's ``.env`` that the
  code never reads — the check that would have caught ``SYSTEM_PROMPT``, which
  ``.env.example`` documented for a long time while the code read
  ``CLAUDE_SYSTEM_PROMPT`` and silently ignored anything set under the other
  name.

The scan is an AST walk rather than a hand-maintained list, because a
hand-maintained list is exactly what decayed the first time.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

#: Names a dict-of-parsed-.env is bound to in this codebase. `main._read_env`
#: and the bridge read some settings from the file directly and never touch
#: ``os.environ``, so a scan that ignored these would report itself complete
#: while missing them.
_ENV_DICT_NAMES = ("env", "_env", "final_env")


def _literal_default(node: ast.AST) -> str | None:
    """Render a default-value node as a string, or None if it isn't a literal."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return None if value is None else str(value)


def _names_in(tree: ast.AST):
    """Yield ``(name, default)`` for every environment read in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            is_getenv = func.attr == "getenv"
            is_environ_get = (
                func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            )
            is_env_dict_get = (
                func.attr == "get"
                and isinstance(func.value, ast.Name)
                and func.value.id in _ENV_DICT_NAMES
            )
            if not (is_getenv or is_environ_get or is_env_dict_get):
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            default = _literal_default(node.args[1]) if len(node.args) > 1 else None
            yield node.args[0].value, default
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.slice, ast.Constant)
        ):
            yield node.slice.value, None


def discover(*source_dirs: Path) -> dict[str, dict]:
    """Find every environment variable read under ``source_dirs``.

    Defaults to the installed package. Returns ``name -> {default, modules}``;
    ``default`` is None when the read has no literal default (or none at all).
    """
    found: dict[str, dict] = {}
    for directory in (source_dirs or (PACKAGE_DIR,)):
        for path in sorted(Path(directory).glob("*.py")):
            try:
                tree = ast.parse(path.read_text(), filename=str(path))
            except (OSError, SyntaxError):  # pragma: no cover - defensive
                continue
            for name, default in _names_in(tree):
                if not (isinstance(name, str) and name.isupper()):
                    continue
                entry = found.setdefault(name, {"default": None, "modules": set()})
                entry["modules"].add(path.stem)
                # First non-None default wins; a later bare read must not
                # erase a default an earlier module declared.
                if entry["default"] is None and default is not None:
                    entry["default"] = default
    return found


def known_names(*source_dirs: Path) -> set[str]:
    """Just the names, for membership checks."""
    return set(discover(*source_dirs))
