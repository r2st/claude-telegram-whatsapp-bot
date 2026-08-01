"""
MCP tools marketplace — a curated catalogue of MCP servers you can install.

``mcp_client`` can already talk to any MCP server listed in the config file, but
writing that JSON by hand is the reason most people never connect one. This
module is the other half: a hand-maintained catalogue of well-known servers, and
install/uninstall helpers that edit the same config file ``mcp_client`` reads.

    from telechat_pkg import mcp_catalog

    mcp_catalog.list_catalog(query="git")        # browse
    mcp_catalog.install("fetch")                 # writes mcp.json
    mcp_catalog.installed()                      # {'fetch': {...}}
    mcp_catalog.uninstall("fetch")

Security posture — this is the important part:

- **Installs are catalogue-only.** ``install()`` takes a catalogue id, never a
  command. The marketplace UI is reachable by anyone holding the web-chat token,
  and "install this MCP server" means "run this program on the host"; letting a
  browser name the program would turn the chat UI into a remote shell. The
  commands that can be written come from this file, which lives in the repo.
- Extra arguments are accepted only for entries that declare
  ``accepts_args``, and are validated (no NUL, no newlines, no empty strings).
- Environment values are accepted only for keys the entry declares in
  ``env_keys``, and are never read back out — :func:`marketplace_snapshot`
  reports *whether* a key is set, not its value. The config file is written
  0600 because those values are usually API tokens.
- ``mcp_client.resolve_allowed_command`` still vets every command at connect
  time. This module does not bypass it: a catalogue entry whose runtime (npx,
  uvx, …) isn't installed or isn't on the allowlist will simply fail to
  register, loudly, in the log.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

#: Key written into each installed server's config block so we can map an
#: installed server back to the catalogue entry it came from. ``mcp_client``
#: reads only command/args/env, so an extra key is inert there.
CATALOG_ID_KEY = "catalogId"

#: A server name must be safe as a JSON key, a log token, and half of the
#: ``server.tool`` identifier the model sees.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class CatalogError(Exception):
    """An install/uninstall request that cannot be honoured as asked."""


@dataclass(frozen=True)
class CatalogEntry:
    """One installable MCP server."""

    id: str
    name: str
    description: str
    category: str
    command: str
    args: tuple[str, ...] = ()
    #: Env vars the server needs before it can do anything useful. Presented as
    #: required fields in the UI; values are stored in the config file.
    env_keys: tuple[str, ...] = ()
    #: True when trailing arguments are meaningful (a directory to expose, a
    #: database path, a repo). ``args_hint`` is what the UI puts in the field.
    accepts_args: bool = False
    args_hint: str = ""
    homepage: str = ""
    tags: tuple[str, ...] = ()

    def haystack(self) -> str:
        return " ".join(
            (self.id, self.name, self.description, self.category, *self.tags)
        ).lower()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "command": self.command,
            "args": list(self.args),
            "env_keys": list(self.env_keys),
            "accepts_args": self.accepts_args,
            "args_hint": self.args_hint,
            "homepage": self.homepage,
            "tags": list(self.tags),
        }


# ─── The catalogue ────────────────────────────────────────────────────────────
#
# Curated by hand. Each entry names a published MCP server and the runtime that
# launches it. Nothing here is fetched at runtime: the marketplace is offline,
# so browsing it cannot be influenced by a third party, and an entry can only
# change by way of a commit to this file.

CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="filesystem",
        name="Filesystem",
        description=(
            "Read, write, and search files under directories you name. "
            "Nothing outside those directories is reachable."
        ),
        category="Files",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        accepts_args=True,
        args_hint="Directories to expose, e.g. /Users/you/notes",
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("files", "disk", "read", "write"),
    ),
    CatalogEntry(
        id="git",
        name="Git",
        description="Read a repository's history, diffs, and branches, and stage commits.",
        category="Development",
        command="uvx",
        args=("mcp-server-git",),
        accepts_args=True,
        args_hint="--repository /path/to/repo",
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("git", "vcs", "repo", "development"),
    ),
    CatalogEntry(
        id="fetch",
        name="Fetch",
        description="Fetch a URL and hand back the page as markdown, ready to reason over.",
        category="Web",
        command="uvx",
        args=("mcp-server-fetch",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("web", "http", "scrape", "url"),
    ),
    CatalogEntry(
        id="memory",
        name="Knowledge graph memory",
        description="A persistent entity/relation store the model can write to and query later.",
        category="Memory",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("memory", "graph", "recall", "notes"),
    ),
    CatalogEntry(
        id="sequential-thinking",
        name="Sequential thinking",
        description="Scratchpad for multi-step reasoning — the model plans, revises, and branches.",
        category="Reasoning",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("reasoning", "planning", "thinking"),
    ),
    CatalogEntry(
        id="sqlite",
        name="SQLite",
        description="Query and modify a SQLite database, and inspect its schema.",
        category="Data",
        command="uvx",
        args=("mcp-server-sqlite",),
        accepts_args=True,
        args_hint="--db-path /path/to/database.db",
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("database", "sql", "sqlite", "data"),
    ),
    CatalogEntry(
        id="time",
        name="Time & timezones",
        description="Current time anywhere, and conversions between timezones.",
        category="Utilities",
        command="uvx",
        args=("mcp-server-time",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("time", "clock", "timezone", "date"),
    ),
    CatalogEntry(
        id="playwright",
        name="Playwright browser",
        description="Drive a real browser — navigate, click, fill forms, read the page.",
        category="Web",
        command="npx",
        args=("-y", "@playwright/mcp@latest"),
        homepage="https://github.com/microsoft/playwright-mcp",
        tags=("browser", "web", "automation", "playwright", "scrape"),
    ),
    CatalogEntry(
        id="github",
        name="GitHub",
        description="Issues, pull requests, code search, and file contents across your repos.",
        category="Development",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        env_keys=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("github", "issues", "pr", "development", "code"),
    ),
    CatalogEntry(
        id="brave-search",
        name="Brave Search",
        description="Web and local search through the Brave Search API.",
        category="Web",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-brave-search"),
        env_keys=("BRAVE_API_KEY",),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("search", "web", "brave"),
    ),
    CatalogEntry(
        id="slack",
        name="Slack",
        description="Read channels, post messages, and pull threads from a Slack workspace.",
        category="Communication",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        env_keys=("SLACK_BOT_TOKEN", "SLACK_TEAM_ID"),
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("slack", "chat", "messages", "communication"),
    ),
    CatalogEntry(
        id="postgres",
        name="PostgreSQL",
        description="Run read-only queries against a Postgres database and inspect its schema.",
        category="Data",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-postgres"),
        accepts_args=True,
        args_hint="postgresql://user@host/dbname",
        homepage="https://github.com/modelcontextprotocol/servers",
        tags=("database", "sql", "postgres", "data"),
    ),
)

_BY_ID = {e.id: e for e in CATALOG}


def get(entry_id: str) -> Optional[CatalogEntry]:
    """Return the catalogue entry with this id, or None."""
    return _BY_ID.get((entry_id or "").strip().lower())


def categories() -> list[str]:
    """Every category present in the catalogue, in display order."""
    seen: list[str] = []
    for e in CATALOG:
        if e.category not in seen:
            seen.append(e.category)
    return sorted(seen)


def list_catalog(query: str = "", category: str = "") -> list[CatalogEntry]:
    """Catalogue entries matching ``query`` and ``category``.

    The query is matched as whitespace-separated terms against the entry's id,
    name, description, category, and tags — every term must appear somewhere,
    so "browser web" narrows rather than widens.
    """
    terms = [t for t in (query or "").lower().split() if t]
    cat = (category or "").strip().lower()
    out = []
    for e in CATALOG:
        if cat and cat not in ("all", e.category.lower()):
            continue
        if terms:
            hay = e.haystack()
            if not all(t in hay for t in terms):
                continue
        out.append(e)
    return out


# ─── Config file ──────────────────────────────────────────────────────────────


def default_config_path() -> str:
    """Where installed servers are recorded when MCP_CONFIG_FILE is unset.

    Lives beside ``bot.db`` in TELECHAT_HOME rather than in the installed
    package, so an upgrade never wipes what you installed.
    """
    home = os.getenv("TELECHAT_HOME") or os.path.join(os.path.expanduser("~"), ".telechat")
    return os.path.join(home, "mcp.json")


def config_path() -> str:
    """The config file this module reads and writes.

    ``MCP_CONFIG_FILE`` wins when set — that is the operator's explicit choice
    and ``mcp_client`` honours it too — otherwise the default location.
    """
    return os.getenv("MCP_CONFIG_FILE", "").strip() or default_config_path()


def load_config(path: str = "") -> dict:
    """Read the MCP config, returning ``{"mcpServers": {}}`` when absent.

    Never raises: a corrupt config should cost you the marketplace listing, not
    the whole chat UI. A parse failure is logged and treated as empty, and
    :func:`save_config` will not overwrite it silently — the caller sees the
    empty dict and any install writes a fresh, valid file.
    """
    p = path or config_path()
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"mcpServers": {}}
    except Exception as exc:
        log.error("Could not read MCP config %s: %s", p, exc)
        return {"mcpServers": {}}
    if not isinstance(data, dict):
        log.error("MCP config %s is not a JSON object; ignoring", p)
        return {"mcpServers": {}}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        data["mcpServers"] = {}
    return data


def save_config(config: dict, path: str = "") -> str:
    """Write the config atomically with 0600 permissions; return the path.

    0600 because ``env`` blocks routinely hold API tokens, and atomically
    because a half-written config is a config ``mcp_client`` will refuse to
    load on the next start.
    """
    p = path or config_path()
    parent = Path(p).parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    # Create with restrictive permissions from the outset — a chmod after the
    # write leaves a window where the token is world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, p)
    return p


def installed(path: str = "") -> dict[str, dict]:
    """Installed servers, keyed by server name.

    Values carry the catalogue id when we know it, so the marketplace can show
    "Installed" against the right card even for a server installed under a
    custom name. Env *values* are deliberately not included.
    """
    servers = load_config(path).get("mcpServers", {})
    out: dict[str, dict] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        raw_env = cfg.get("env")
        env = raw_env if isinstance(raw_env, dict) else {}
        out[name] = {
            "name": name,
            "catalog_id": cfg.get(CATALOG_ID_KEY, ""),
            "command": cfg.get("command", ""),
            "args": list(cfg.get("args", []) or []),
            "env_keys_set": sorted(k for k, v in env.items() if str(v).strip()),
        }
    return out


# ─── Install / uninstall ──────────────────────────────────────────────────────


def _validate_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RE.match(n):
        raise CatalogError(
            "Server name must be 1–32 characters of a–z, 0–9, '-' or '_', "
            "starting with a letter or digit."
        )
    return n


def _validate_extra_args(entry: CatalogEntry, extra: Iterable[str]) -> list[str]:
    args = [str(a) for a in (extra or [])]
    args = [a.strip() for a in args if str(a).strip()]
    if not args:
        return []
    if not entry.accepts_args:
        raise CatalogError(f"{entry.name} does not take extra arguments.")
    for a in args:
        # A newline or NUL in an argv entry is never legitimate here, and both
        # are the classic way to smuggle a second instruction into a config
        # file that other tools also read.
        if "\x00" in a or "\n" in a or "\r" in a:
            raise CatalogError("Arguments cannot contain newlines or null bytes.")
        if len(a) > 512:
            raise CatalogError("Argument is too long (max 512 characters).")
    if len(args) > 16:
        raise CatalogError("Too many arguments (max 16).")
    return args


def _validate_env(entry: CatalogEntry, env: Optional[dict]) -> dict[str, str]:
    if not env:
        return {}
    allowed = set(entry.env_keys)
    out: dict[str, str] = {}
    for key, value in env.items():
        k = str(key).strip()
        if k not in allowed:
            # Refusing unknown keys keeps the browser from setting PATH,
            # LD_PRELOAD, or anything else that changes what actually runs.
            raise CatalogError(
                f"{entry.name} does not use an environment variable named {k!r}."
            )
        v = str(value)
        if "\x00" in v or "\n" in v or "\r" in v:
            raise CatalogError(f"Value for {k} cannot contain newlines or null bytes.")
        if len(v) > 4096:
            raise CatalogError(f"Value for {k} is too long.")
        if v.strip():
            out[k] = v
    return out


@dataclass
class InstallResult:
    name: str
    entry_id: str
    path: str
    missing_env: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entry_id": self.entry_id,
            "path": self.path,
            "missing_env": list(self.missing_env),
        }


def install(
    entry_id: str,
    *,
    name: str = "",
    extra_args: Optional[Iterable[str]] = None,
    env: Optional[dict] = None,
    path: str = "",
    replace: bool = False,
) -> InstallResult:
    """Add a catalogue entry to the MCP config file.

    Raises :class:`CatalogError` for an unknown id, a name already taken (unless
    ``replace``), or arguments/env the entry does not accept. The write itself
    is atomic.

    Installing does not start anything: ``mcp_client`` picks the server up when
    it next builds its manager, which is what ``MCP_ENABLED=true`` plus a bot
    restart gets you.
    """
    entry = get(entry_id)
    if not entry:
        raise CatalogError(f"No such tool in the catalogue: {entry_id!r}")

    server_name = _validate_name(name or entry.id)
    args = list(entry.args) + _validate_extra_args(entry, extra_args or [])
    env_block = _validate_env(entry, env)

    cfg = load_config(path)
    servers = cfg.setdefault("mcpServers", {})
    if server_name in servers and not replace:
        raise CatalogError(
            f"A server called {server_name!r} is already installed. "
            "Uninstall it first, or install under a different name."
        )

    block: dict[str, Any] = {
        CATALOG_ID_KEY: entry.id,
        "command": entry.command,
        "args": args,
    }
    if env_block:
        block["env"] = env_block
    servers[server_name] = block
    written = save_config(cfg, path)

    missing = [k for k in entry.env_keys if k not in env_block]
    log.info("Installed MCP server %r (%s) into %s", server_name, entry.id, written)
    return InstallResult(
        name=server_name, entry_id=entry.id, path=written, missing_env=missing
    )


def uninstall(name: str, path: str = "") -> bool:
    """Remove an installed server. Returns False when it wasn't there."""
    server_name = (name or "").strip()
    cfg = load_config(path)
    servers = cfg.get("mcpServers", {})
    if server_name not in servers:
        return False
    servers.pop(server_name)
    save_config(cfg, path)
    log.info("Uninstalled MCP server %r", server_name)
    return True


# ─── Snapshot for the web UI ──────────────────────────────────────────────────


def _live_status() -> dict[str, dict]:
    """Connection status per server name, when a manager already exists.

    Deliberately does not *create* a manager: building one loads the config and
    is a side effect nobody asked for by opening a panel.
    """
    try:
        from . import mcp_client
        mgr = mcp_client._mcp_manager
        if mgr is None:
            return {}
        return {s["name"]: s for s in mgr.list_servers()}
    except Exception:
        log.debug("could not read live MCP status", exc_info=True)
        return {}


def marketplace_snapshot(query: str = "", category: str = "") -> dict:
    """A JSON-safe view of the marketplace for the web UI.

    Never raises — a broken config file degrades the panel to "nothing
    installed" rather than breaking the socket.
    """
    try:
        inst = installed()
    except Exception:
        log.debug("could not read installed MCP servers", exc_info=True)
        inst = {}
    live = _live_status()
    by_catalog: dict[str, list[dict]] = {}
    for name, info in inst.items():
        entry = dict(info)
        status = live.get(name, {})
        entry["status"] = status.get("status", "")
        entry["tools_count"] = status.get("tools_count", 0)
        by_catalog.setdefault(info.get("catalog_id") or "", []).append(entry)

    items = []
    for e in list_catalog(query, category):
        mine = by_catalog.get(e.id, [])
        d = e.to_dict()
        d["installed"] = bool(mine)
        d["installs"] = sorted(mine, key=lambda m: m["name"])
        items.append(d)

    # Servers configured by hand (or from a catalogue entry that has since been
    # removed) still deserve a row — otherwise the panel claims nothing is
    # installed while the bot is happily using three servers.
    unlisted = [
        dict(info, status=live.get(name, {}).get("status", ""))
        for name, info in sorted(inst.items())
        if not get(info.get("catalog_id") or "")
    ]

    try:
        from . import mcp_client
        enabled = mcp_client.MCP_ENABLED
    except Exception:
        enabled = False

    return {
        "available": True,
        "enabled": bool(enabled),
        "config_path": config_path(),
        "categories": categories(),
        "query": query or "",
        "category": category or "",
        "items": items,
        "unlisted": unlisted,
        "installed_count": len(inst),
        "catalog_count": len(CATALOG),
    }
