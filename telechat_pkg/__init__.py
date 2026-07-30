"""Telechat — Claude AI messenger bot for Telegram, WhatsApp, and Slack."""

from __future__ import annotations


def _read_version() -> str:
    """Resolve the package version from installed metadata, or pyproject.toml.

    Item 4 of docs/improvements.md: this was a hardcoded string and it had
    drifted — pyproject said 1.2.0, this said 1.1.5, npm/package.json said 1.1.1,
    and all three shipped in one release. `updater.current_version()` read the
    metadata, `mcp_client` reported this constant, and `telechat --version`
    printed the wrapper's, so the same install described itself three ways — and
    the updater compared its number against both registries and nagged
    permanently.

    pyproject.toml is the single source now. Installed metadata is preferred
    because it is what the updater reads; the pyproject fallback covers running
    from a source checkout with nothing installed.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("telechatai")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 — a version lookup must never break the import
        pass
    try:
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "0.0.0"


__version__ = _read_version()
