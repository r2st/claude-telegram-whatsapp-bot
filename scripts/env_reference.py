#!/usr/bin/env python3
"""Extract every environment variable telechat reads, straight from the source.

`docs/configuration.md` is generated from this. The point is that the reference
cannot drift: `tests/test_config_docs.py` regenerates it and fails if the
committed file differs, and it also fails if a newly-introduced variable has no
description here. Adding a variable to the code therefore forces a line in this
file — which is where the human-written part lives, since a default can be read
off an AST but a purpose cannot.

Usage:
    python scripts/env_reference.py            # write docs/configuration.md
    python scripts/env_reference.py --check    # exit 1 if it is out of date
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "telechat_pkg"
SCRIPTS_DIR = REPO_ROOT / "scripts"
#: Scanned directories. `scripts/` is included because watchdog.py reads its own
#: WATCHDOG_* settings and `.env.example` documents them — a reference that
#: skipped it would call itself complete while missing variables users set.
SOURCE_DIRS = (PACKAGE_DIR, SCRIPTS_DIR)
OUTPUT = REPO_ROOT / "docs" / "configuration.md"

# ─── Grouping ────────────────────────────────────────────────────────────────
# Section order is deliberate: what you must set, then what you are most likely
# to want, then the knobs almost nobody touches.

SECTIONS: list[tuple[str, str]] = [
    ("Core", "Which platforms run, and how Claude is reached."),
    ("Telegram", "The primary adapter."),
    ("WhatsApp", "Via the Green API bridge."),
    ("Slack", "Socket Mode."),
    ("Web chat", "The built-in browser UI."),
    ("Claude Desktop bridge", "Hooks that push your desktop Claude sessions to your phone."),
    ("Storage and limits", "Database, rate limiting, write-queue behaviour."),
    ("Memory and knowledge base", "Long-term recall and RAG."),
    ("Cost and routing", "Budgets, model selection, the two-agent planner."),
    ("Media and web", "Voice, images, music, video, search, fetch, documents."),
    ("Self-improvement", "Judge, preference learning, prompt A/B, auto-update."),
    ("Operations", "Health endpoint, debugging, MCP."),
    ("Watchdog", "`scripts/watchdog.py`, the optional self-healing supervisor."),
]

#: name -> (section, one-line description).
#: Every variable the extractor finds must appear here — that is the check that
#: keeps this reference honest.
DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # ── Core ──
    "BOT_MODE": ("Core", "Which adapters to run: `telegram`, `whatsapp`, `slack`, `web`, a comma-separated list, or the aliases `both` / `all`."),
    "CLAUDE_MODE": ("Core", "`cli` (free with a Claude subscription, uses the `claude` binary) or `api` (billed, uses ANTHROPIC_API_KEY)."),
    "ANTHROPIC_API_KEY": ("Core", "API key, required when CLAUDE_MODE=api."),
    "CLAUDE_CODE_OAUTH_TOKEN": ("Core", "OAuth token for the Claude CLI, passed through to spawned `claude` processes."),
    "CLAUDE_MODEL": ("Core", "Model alias for CLI mode (`sonnet`, `opus`, `haiku`)."),
    "CLAUDE_CLI_MODEL": ("Core", "Per-adapter override of the CLI model. Falls back to CLAUDE_MODEL."),
    "CLAUDE_API_MODEL": ("Core", "Model for API mode. Defaults to the current Sonnet in `models.py`."),
    "CLAUDE_CLI_WORK_DIR": ("Core", "Working directory `claude` runs in. Everything the bot can read or write is relative to this."),
    "CLAUDE_ADD_DIRS": ("Core", "Extra directories to grant the CLI access to (comma-separated), passed as `--add-dir`."),
    "CLAUDE_CLI_PERMISSION_MODE": ("Core", "Permission mode handed to the CLI/SDK: `auto` (mapped to Claude Code's `default`), `acceptEdits`, `plan`, or `bypassPermissions`."),
    "CLAUDE_TIMEOUT": ("Core", "Seconds before a single Claude turn is abandoned."),
    "MAX_TOKENS": ("Core", "Upper bound on output tokens per API-mode reply."),
    "CLAUDE_SYSTEM_PROMPT": ("Core", "Extra system prompt prepended to every conversation."),
    "SYSTEM_PROMPT": ("Core", "Legacy name for CLAUDE_SYSTEM_PROMPT, read as a fallback because `.env.example` documented it for a long time while the code ignored it."),
    "CLAUDE_CLI_ADD_DIRS": ("Core", "Legacy name for CLAUDE_ADD_DIRS, read as a fallback for the same reason."),
    "TELECHAT_HOME": ("Core", "Data home. Everything — `.env`, `bot.db`, logs, the PID file — resolves here. Defaults to `~/.telechat`."),

    # ── Telegram ──
    "TELEGRAM_BOT_TOKEN": ("Telegram", "Bot token from @BotFather. Required for the Telegram adapter."),
    "TELEGRAM_ALLOWED_USER_IDS": ("Telegram", "Comma-separated numeric user IDs allowed to talk to the bot. Empty means anyone can."),
    "TELEGRAM_CHAT_ID": ("Telegram", "Chat the bridge posts cards to. Falls back to the first entry of TELEGRAM_ALLOWED_USER_IDS. Read from the `.env` file, not the process environment."),

    # ── WhatsApp ──
    "GREEN_API_INSTANCE_ID": ("WhatsApp", "Green API instance id. Required for the WhatsApp adapter."),
    "GREEN_API_TOKEN": ("WhatsApp", "Green API token."),
    "GREEN_API_BASE_URL": ("WhatsApp", "Green API endpoint, for self-hosted or regional instances."),
    "WHATSAPP_ALLOWED_NUMBERS": ("WhatsApp", "Comma-separated numbers allowed to message the bot. Empty means anyone can."),
    "POLL_INTERVAL_SECONDS": ("WhatsApp", "Seconds between Green API receive polls."),

    # ── Slack ──
    "SLACK_BOT_TOKEN": ("Slack", "`xoxb-` bot token. Required for the Slack adapter."),
    "SLACK_APP_TOKEN": ("Slack", "`xapp-` app token for Socket Mode."),
    "SLACK_ALLOWED_USER_IDS": ("Slack", "Comma-separated Slack user IDs allowed to use the bot. Empty means anyone in the workspace can."),

    # ── Web chat ──
    "WEB_CHAT_PORT": ("Web chat", "Port for the built-in web UI."),
    "WEB_CHAT_BIND": ("Web chat", "Bind address. Defaults to `127.0.0.1`; binding wider requires WEB_CHAT_TOKEN or WEB_CHAT_ALLOW_OPEN."),
    "WEB_CHAT_TOKEN": ("Web chat", "Access token required to open the web UI. Strongly recommended whenever it is not on loopback."),
    "WEB_CHAT_ALLOW_OPEN": ("Web chat", "Explicitly permit an exposed, unauthenticated web chat. Refuses to start without this."),
    "WEB_CHAT_TRUST_PROXY": ("Web chat", "Trust `X-Forwarded-For` for client IPs. Only set this behind a proxy you control."),
    "WEB_CHAT_AUTH_MAX_ATTEMPTS": ("Web chat", "Failed token attempts from one IP before it is locked out."),
    "WEB_CHAT_AUTH_LOCKOUT_SEC": ("Web chat", "How long that lockout lasts."),

    # ── Bridge ──
    "BRIDGE_APPROVAL_TIMEOUT": ("Claude Desktop bridge", "Seconds an approval card waits for a tap before the timeout policy applies."),
    "BRIDGE_APPROVAL_TIMEOUT_ACTION": ("Claude Desktop bridge", "What an unanswered approval resolves to: `fallthrough` (default, hands back to Claude Code), `deny`, or `allow`."),
    "TELECHAT_BRIDGE_INTERNAL": ("Claude Desktop bridge", "Set by the bridge on processes it spawns, so their own hooks don't post duplicate cards. Not for operators."),

    # ── Storage and limits ──
    "DB_PATH": ("Storage and limits", "SQLite database path. Defaults to `$TELECHAT_HOME/bot.db`."),
    "DB_WRITE_QUEUE_TIMEOUT": ("Storage and limits", "Seconds a caller waits for room in a full write queue before writing synchronously."),
    "DB_WRITE_MAX_ATTEMPTS": ("Storage and limits", "Retries for a write that fails on lock/busy contention."),
    "DB_HISTORY_WAIT_TIMEOUT": ("Storage and limits", "Seconds a history read waits for pending writes so it doesn't serve a conversation missing its newest turns."),
    "RATE_LIMIT_REQUESTS": ("Storage and limits", "Messages allowed per user per window."),
    "RATE_LIMIT_WINDOW": ("Storage and limits", "Length of that window, in seconds."),
    "MAX_CONCURRENT_TASKS": ("Storage and limits", "Claude turns allowed to run at once across all users."),

    # ── Memory / KB ──
    "AUTO_MEMORY": ("Memory and knowledge base", "Extract memories from conversations automatically."),
    "AUTO_MEMORY_MIN_LENGTH": ("Memory and knowledge base", "Shortest message considered worth extracting from."),
    "KB_ENABLED": ("Memory and knowledge base", "Enable the knowledge base / RAG retrieval."),
    "KB_CHUNK_SIZE": ("Memory and knowledge base", "Characters per indexed chunk."),
    "KB_CHUNK_OVERLAP": ("Memory and knowledge base", "Overlap between adjacent chunks."),
    "KB_MAX_CONTEXT_CHUNKS": ("Memory and knowledge base", "Chunks retrieved per query."),
    "KB_MAX_CONTEXT_CHARS": ("Memory and knowledge base", "Hard cap on retrieved context, whatever the chunk count."),

    # ── Cost and routing ──
    "COST_BUDGET_ENABLED": ("Cost and routing", "Enforce spend budgets. In API mode this is what makes `/budget` mean anything."),
    "COST_DAILY_BUDGET": ("Cost and routing", "US dollars per day, per user."),
    "COST_MONTHLY_BUDGET": ("Cost and routing", "US dollars per month, per user."),
    "COST_WARN_THRESHOLD": ("Cost and routing", "Fraction of a budget (0–1) at which the user is warned."),
    "SMART_ROUTING_ENABLED": ("Cost and routing", "Pick the model per message from estimated complexity."),
    "SMART_ROUTE_HAIKU_MAX": ("Cost and routing", "Complexity score at or below which Haiku is used."),
    "SMART_ROUTE_OPUS_MIN": ("Cost and routing", "Complexity score at or above which Opus is used."),
    "SMART_ROUTE_HAIKU_API": ("Cost and routing", "Model id used for the Haiku tier in API mode."),
    "SMART_ROUTE_SONNET_API": ("Cost and routing", "Model id used for the Sonnet tier in API mode."),
    "SMART_ROUTE_OPUS_API": ("Cost and routing", "Model id used for the Opus tier in API mode."),
    "MODEL_HAIKU": ("Cost and routing", "Override the registry's Haiku id."),
    "MODEL_SONNET": ("Cost and routing", "Override the registry's Sonnet id."),
    "MODEL_OPUS": ("Cost and routing", "Override the registry's Opus id."),
    "TWO_AGENT_ENABLED": ("Cost and routing", "Enable the planner/executor split behind `/plan`."),
    "TWO_AGENT_THRESHOLD": ("Cost and routing", "Complexity above which a message is routed to the two-agent path."),
    "TWO_AGENT_MAX_STEPS": ("Cost and routing", "Executor steps before the plan is abandoned."),
    "PLANNER_MODEL": ("Cost and routing", "Model that writes the plan."),
    "EXECUTOR_MODEL": ("Cost and routing", "Model that executes each step."),

    # ── Media and web ──
    "TRANSCRIPTION_ENABLED": ("Media and web", "Transcribe inbound voice messages."),
    "TRANSCRIPTION_MAX_SIZE_MB": ("Media and web", "Largest voice file accepted for transcription."),
    "WHISPER_MODEL": ("Media and web", "Whisper model id for transcription."),
    "TTS_ENABLED": ("Media and web", "Enable text-to-speech replies."),
    "TTS_VOICE": ("Media and web", "Default voice."),
    "TTS_MODEL": ("Media and web", "Text-to-speech model id."),
    "TTS_MAX_LENGTH": ("Media and web", "Longest text sent to the TTS API in one request."),
    "OPENAI_API_KEY": ("Media and web", "OpenAI key, used for TTS and image generation."),
    "IMAGE_GEN_ENABLED": ("Media and web", "Enable image generation."),
    "IMAGE_GEN_MODEL": ("Media and web", "Image model id."),
    "IMAGE_GEN_SIZE": ("Media and web", "Generated image size."),
    "IMAGE_GEN_QUALITY": ("Media and web", "Generated image quality tier."),
    "MUSIC_GEN_ENABLED": ("Media and web", "Enable music generation."),
    "MUSIC_GEN_MODEL": ("Media and web", "Music model id."),
    "MUSIC_GEN_DURATION": ("Media and web", "Seconds of audio generated."),
    "VIDEO_GEN_ENABLED": ("Media and web", "Enable video generation."),
    "VIDEO_GEN_MODEL": ("Media and web", "Video model id."),
    "REPLICATE_API_TOKEN": ("Media and web", "Replicate token, used for music and video generation."),
    "WEB_SEARCH_ENABLED": ("Media and web", "Enable web search."),
    "BRAVE_SEARCH_API_KEY": ("Media and web", "Brave Search key."),
    "TAVILY_API_KEY": ("Media and web", "Tavily key, the other supported search backend."),
    "WEB_SEARCH_PROVIDER": ("Media and web", "Search backend: `auto` picks whichever key is present, or name `brave` / `tavily`."),
    "WEB_SEARCH_MAX_RESULTS": ("Media and web", "Results kept per search."),
    "WEB_FETCH_ENABLED": ("Media and web", "Allow fetching and summarising URLs."),
    "WEB_FETCH_MAX_KB": ("Media and web", "Kilobytes read from a fetched page before truncation."),
    "JINA_API_KEY": ("Media and web", "Jina Reader key. With it, fetches go through Jina; without it, raw HTML is fetched and stripped."),
    "WEB_FETCH_TIMEOUT": ("Media and web", "Per-fetch timeout in seconds."),
    "LINK_UNDERSTANDING_ENABLED": ("Media and web", "Automatically read links users send."),
    "LINK_MAX_CONTENT_KB": ("Media and web", "Kilobytes read from each such link."),
    "LINK_FETCH_TIMEOUT": ("Media and web", "Per-link timeout in seconds."),
    "LINK_MAX_LINKS": ("Media and web", "Links followed per message."),
    "EXTRACT_MAX_SIZE_MB": ("Media and web", "Largest document accepted."),
    "EXTRACT_MAX_TEXT_CHARS": ("Media and web", "Characters kept from an extracted document."),
    "BROWSER_ENABLED": ("Media and web", "Enable Playwright browser automation."),
    "BROWSER_HEADLESS": ("Media and web", "Run that browser headless."),
    "BROWSER_TIMEOUT": ("Media and web", "Per-action browser timeout in milliseconds."),
    "SCREENSHOT_DIR": ("Media and web", "Where browser screenshots are written."),

    # ── Self-improvement ──
    "JUDGE_MODEL": ("Self-improvement", "Model that does the judging."),
    "JUDGE_SAMPLE_RATE": ("Self-improvement", "Fraction of replies sampled for judging (0–1)."),
    "PROMPT_OPTIMIZER_ENABLED": ("Self-improvement", "A/B test system-prompt variants."),
    "PROMPT_PROMOTE_MIN_SAMPLES": ("Self-improvement", "Samples a variant needs before it can win."),
    "PROMPT_PROMOTE_MARGIN": ("Self-improvement", "Score margin required to promote a variant."),
    "AUTO_SCHEDULER_ENABLED": ("Self-improvement", "Run the background self-improvement scheduler."),
    "SCHEDULER_ENABLED": ("Self-improvement", "Run user-defined scheduled tasks (`/schedule`)."),
    "SCHEDULED_TASKS_FILE": ("Self-improvement", "Where those task definitions are stored. Defaults to a file in the data home."),
    "UPDATE_CHECK_INTERVAL": ("Self-improvement", "Seconds between update checks. 0 disables them."),
    "UPDATE_AUTO_APPLY": ("Self-improvement", "Install updates automatically rather than only notifying."),
    "UPDATE_PYPI_PACKAGE": ("Self-improvement", "PyPI package name the updater watches."),
    "UPDATE_NPM_PACKAGE": ("Self-improvement", "npm package name the updater watches."),

    # ── Operations ──
    "HEALTH_PORT": ("Operations", "Port for the `/health` endpoint."),
    "HEALTH_BIND_ADDR": ("Operations", "Bind address for it. Loopback by default — the endpoint is unauthenticated."),
    "WATCHDOG_INTERVAL": ("Operations", "Seconds between watchdog liveness checks."),
    "TELECHAT_DEBUG": ("Operations", "Verbose logging, and tracebacks instead of friendly messages."),
    "MCP_ENABLED": ("Operations", "Connect to external MCP servers."),
    "MCP_CONFIG_FILE": ("Operations", "Path to an `mcpServers` JSON config."),
    "MCP_ALLOWED_COMMANDS": ("Operations", "Extra command names MCP servers may be launched with, beyond the built-in runtime list."),
    "MCP_ALLOWED_COMMAND_PATHS": ("Operations", "Strict mode: directory prefixes a resolved MCP executable must live under."),
    "MCP_ALLOW_ANY_COMMAND": ("Operations", "Disable the MCP command allowlist entirely. Understand what that means first."),

    # ── Watchdog (scripts/watchdog.py) ──
    "WATCHDOG_ENABLED": ("Watchdog", "Master switch for the self-healing supervisor."),
    "WATCHDOG_DRY_RUN": ("Watchdog", "Diagnose and log, but never apply a fix or restart anything."),
    "WATCHDOG_PROJECT_DIR": ("Watchdog", "Checkout the watchdog operates on."),
    "WATCHDOG_SCAN_INTERVAL": ("Watchdog", "Seconds between log scans."),
    "WATCHDOG_BATCH_WINDOW": ("Watchdog", "Seconds of errors collected into one batch before diagnosing."),
    "WATCHDOG_MAX_FIXES_HOUR": ("Watchdog", "Fixes it may apply in an hour, so a fix loop cannot run away."),
    "WATCHDOG_FIX_COOLDOWN": ("Watchdog", "Seconds to wait after a fix before attempting another."),
    "WATCHDOG_REGRESSION_WATCH": ("Watchdog", "Seconds a fix is watched for regressions before it is accepted."),
    "WATCHDOG_BOT_SERVICE": ("Watchdog", "launchd service label the watchdog restarts."),
    "WATCHDOG_CLAUDE_MODEL": ("Watchdog", "Model the watchdog uses to diagnose and write fixes."),
}


def _literal(node: ast.AST) -> str | None:
    """Render a default-value node as a string, or None if it isn't a literal."""
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if value is None:
        return None
    return str(value)


def discover(*source_dirs: Path) -> dict[str, dict]:
    """Find every environment variable the package reads.

    Recognises ``os.getenv("X")``, ``os.environ.get("X")``, ``os.environ["X"]``,
    and ``env.get("X")`` on a dict parsed out of the `.env` file — the bridge and
    the CLI read some settings that way and never touch ``os.environ``, so a
    scan that ignored it would report the reference as complete while missing
    them. Returns name -> {default, modules}.
    """
    found: dict[str, dict] = {}

    def record(name: str, default: str | None, module: str) -> None:
        entry = found.setdefault(name, {"default": None, "modules": set()})
        entry["modules"].add(module)
        # First non-None default wins; a later bare read shouldn't erase it.
        if entry["default"] is None and default is not None:
            entry["default"] = default

    paths = [
        path
        for directory in (source_dirs or SOURCE_DIRS)
        for path in sorted(directory.glob("*.py"))
        # This file's own DESCRIPTIONS table would otherwise scan as source.
        if path.name != "env_reference.py"
    ]
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        module = path.stem
        for node in ast.walk(tree):
            name = default = None
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                is_getenv = isinstance(func, ast.Attribute) and func.attr == "getenv"
                is_environ_get = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
                # A dict parsed from the .env file, e.g. `env.get("TELEGRAM_CHAT_ID")`.
                is_env_dict_get = (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in ("env", "_env", "final_env")
                )
                if (is_getenv or is_environ_get or is_env_dict_get) and isinstance(
                    node.args[0], ast.Constant
                ):
                    name = node.args[0].value
                    if len(node.args) > 1:
                        default = _literal(node.args[1])
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
            ):
                name = node.slice.value
            if isinstance(name, str) and name.isupper():
                record(name, default, module)
    return found


def undocumented(found: dict[str, dict] | None = None) -> list[str]:
    """Variables the code reads that have no description here."""
    found = discover() if found is None else found
    return sorted(set(found) - set(DESCRIPTIONS))


def orphaned(found: dict[str, dict] | None = None) -> list[str]:
    """Descriptions for variables the code no longer reads."""
    found = discover() if found is None else found
    return sorted(set(DESCRIPTIONS) - set(found))


def render(found: dict[str, dict] | None = None) -> str:
    found = discover() if found is None else found
    lines = [
        "# Configuration reference",
        "",
        "Every environment variable telechat reads, with the default the code",
        "actually uses. Set them in `$TELECHAT_HOME/.env` (`~/.telechat/.env` by",
        "default) or in the process environment.",
        "",
        "**This file is generated.** Run `python scripts/env_reference.py` after",
        "adding a variable; `tests/test_config_docs.py` fails if it is stale, and",
        "also fails if a new variable has no description in",
        "`scripts/env_reference.py`. That is deliberate: the reference cannot",
        "silently fall behind the code again.",
        "",
        "A blank Default column means the variable is unset by default.",
        "",
    ]
    by_section: dict[str, list[str]] = {title: [] for title, _ in SECTIONS}
    for name in sorted(found):
        section, description = DESCRIPTIONS[name]
        default = found[name]["default"]
        rendered_default = f"`{default}`" if default not in (None, "") else ""
        by_section[section].append(
            f"| `{name}` | {rendered_default} | {description} |"
        )

    for title, blurb in SECTIONS:
        rows = by_section[title]
        if not rows:
            continue
        lines += [f"## {title}", "", blurb, "", "| Variable | Default | Purpose |",
                  "|---|---|---|", *rows, ""]

    lines += [
        "---",
        "",
        f"{len(found)} variables, generated from `telechat_pkg/*.py` and `scripts/*.py`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    found = discover()
    missing = undocumented(found)
    if missing:
        print("Undocumented environment variables (add them to DESCRIPTIONS in", file=sys.stderr)
        print("scripts/env_reference.py):", file=sys.stderr)
        for name in missing:
            print(f"  {name}  (read in: {', '.join(sorted(found[name]['modules']))})", file=sys.stderr)
        return 1

    stale = orphaned(found)
    if stale:
        print("Described but no longer read by the code — remove them:", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        return 1

    content = render(found)
    if "--check" in argv:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != content:
            print(f"{OUTPUT} is out of date — run: python scripts/env_reference.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT} is up to date ({len(found)} variables).")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content)
    print(f"Wrote {OUTPUT} ({len(found)} variables).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
