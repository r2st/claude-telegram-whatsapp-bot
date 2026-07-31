"""
Entry point for the Claude messenger bot.

Subcommands:
  telechat              — start the bot (default)
  telechat init         — interactive .env setup
  telechat start        — start the bot

Set BOT_MODE in your .env to one or more platforms (comma-separated):

  BOT_MODE=telegram              — Telegram only  (default)
  BOT_MODE=whatsapp              — WhatsApp only
  BOT_MODE=slack                 — Slack only
  BOT_MODE=web                   — Web chat only (browser)
  BOT_MODE=telegram,whatsapp     — Telegram + WhatsApp
  BOT_MODE=telegram,slack        — Telegram + Slack
  BOT_MODE=telegram,web          — Telegram + Web
  BOT_MODE=whatsapp,slack        — WhatsApp + Slack
  BOT_MODE=all                   — all platforms

Legacy alias: BOT_MODE=both → telegram,whatsapp
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys


# ─── Workdir resolution ───────────────────────────────────────────────────────
# The npm CLI stores the chosen working directory in ~/.telechat/config.json.
# Resolve and chdir there so the pip-installed `telechat` behaves identically
# regardless of which directory the user runs it from.

_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".telechat", "config.json")


_DATA_HOME = os.path.join(os.path.expanduser("~"), ".telechat")


def _resolve_workdir() -> str | None:
    """Chdir to the data home (~/.telechat) so .env, logs, db resolve there.

    Priority: TELECHAT_HOME env var → ~/.telechat → legacy config.workdir.
    """
    home = os.environ.get("TELECHAT_HOME") or _DATA_HOME
    if os.path.isdir(home):
        os.chdir(home)
        return home
    # Legacy fallback: config.json may carry an old workdir
    try:
        with open(_CONFIG_FILE) as f:
            cfg = json.load(f)
        wd = cfg.get("workdir") or cfg.get("claudeWorkdir")
        if wd and os.path.isdir(wd):
            os.chdir(wd)
            return wd
    except (OSError, ValueError):
        pass
    return None


def _save_workdir(wd: str) -> None:
    """Persist workdir to ~/.telechat/config.json (shared with npm CLI)."""
    try:
        os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
        cfg = {}
        if os.path.isfile(_CONFIG_FILE):
            try:
                with open(_CONFIG_FILE) as f:
                    cfg = json.load(f)
            except ValueError:
                cfg = {}
        cfg["workdir"] = wd
        with open(_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
    except OSError:
        pass


# ─── .env helpers ─────────────────────────────────────────────────────────────

def _find_env_file() -> str:
    """Return path to .env. Data home (~/.telechat) is authoritative.

    Order: $TELECHAT_HOME/.env → ~/.telechat/.env → cwd/.env.
    The editable-install package dir is intentionally NOT used — it would
    pick up the placeholder .env.example template.
    """
    home = os.environ.get("TELECHAT_HOME") or _DATA_HOME
    home_env = os.path.join(home, ".env")
    if os.path.isfile(home_env):
        return home_env
    cwd_env = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(cwd_env):
        return cwd_env
    return home_env  # default creation location = data home


def _has_any_platform(env: dict[str, str]) -> bool:
    """True if at least one platform has credentials configured."""
    platforms = _parse_platforms(env.get("BOT_MODE", "telegram"))
    return bool(
        env.get("TELEGRAM_BOT_TOKEN")
        or (env.get("GREEN_API_INSTANCE_ID") and env.get("GREEN_API_TOKEN"))
        or (env.get("SLACK_BOT_TOKEN") and env.get("SLACK_APP_TOKEN"))
        or "web" in platforms
    )


def _print_setup_guidance() -> None:
    """Friendly guidance when no usable .env is found (no traceback)."""
    print()
    print("  telechat is not configured yet — no platform credentials found.")
    print()
    print("  Set it up with one of:")
    print()
    print("    telechat init     AI-guided setup (recommended)")
    print("                      Opens browser, validates tokens automatically.")
    print()
    print("    telechat setup    Manual step-by-step wizard")
    print()
    print("  Quick start (Telegram only):")
    print("    1. Message @BotFather on Telegram → /newbot → copy the token")
    print("    2. telechat init   (or create a .env with TELEGRAM_BOT_TOKEN=...)")
    print()


def _read_env(path: str) -> dict[str, str]:
    """Read a .env file into a dict (ignores comments, blank lines)."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def _set_env_var(path: str, key: str, value: str) -> None:
    """Set a variable in the .env file, updating in-place or appending."""
    lines: list[str] = []
    found = False
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                lines[i] = f"{key}={value}\n"
                found = True
                break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


def _env_example_path() -> str | None:
    """Find .env.example in the project."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    proj_dir = os.path.dirname(pkg_dir)
    for d in [os.getcwd(), proj_dir]:
        p = os.path.join(d, ".env.example")
        if os.path.isfile(p):
            return p
    return None


# ─── Init command ─────────────────────────────────────────────────────────────

def _validate_telegram_token(token: str) -> str | None:
    """Validate a Telegram bot token via getMe. Returns bot username or None."""
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return f"@{data['result'].get('username', '???')}"
    except Exception:
        pass
    return None


def _validate_green_api(instance_id: str, token: str) -> str | None:
    """Validate Green API credentials. Returns state string or None."""
    import urllib.request
    try:
        url = f"https://api.green-api.com/waInstance{instance_id}/getStateInstance/{token}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("stateInstance", "unknown")
    except Exception:
        pass
    return None


def _validate_slack_token(token: str) -> str | None:
    """Validate a Slack bot token via auth.test. Returns team name or None."""
    import urllib.request
    try:
        req = urllib.request.Request(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return data.get("team", "???")
    except Exception:
        pass
    return None


def _cmd_init() -> None:
    """Interactive .env setup wizard with token validation."""
    import shutil

    env_path = _find_env_file()
    exists = os.path.isfile(env_path)

    if not exists:
        example = _env_example_path()
        if example:
            shutil.copy2(example, env_path)
            print(f"Created {env_path} from template")
        else:
            open(env_path, "w").close()
            print(f"Created {env_path}")
    else:
        print(f"Using {env_path}")

    env = _read_env(env_path)

    # ── Platform selection ────────────────────────────────────────────────
    current_mode = env.get("BOT_MODE", "telegram")
    print(f"\n── Platforms ({'current: ' + current_mode}) ──")
    print("  1) telegram")
    print("  2) whatsapp")
    print("  3) slack")
    print("  4) web (browser chat)")
    print("  5) telegram,whatsapp")
    print("  6) telegram,slack")
    print("  7) telegram,web")
    print("  8) all (telegram + whatsapp + slack + web)")
    choice = input(f"\nChoose platforms [enter to keep '{current_mode}']: ").strip()
    mode_map = {"1": "telegram", "2": "whatsapp", "3": "slack", "4": "web",
                "5": "telegram,whatsapp", "6": "telegram,slack",
                "7": "telegram,web", "8": "all"}
    if choice in mode_map:
        current_mode = mode_map[choice]
        _set_env_var(env_path, "BOT_MODE", current_mode)
        print(f"  → BOT_MODE={current_mode}")
    elif choice:
        current_mode = choice
        _set_env_var(env_path, "BOT_MODE", current_mode)
        print(f"  → BOT_MODE={current_mode}")

    platforms = _parse_platforms(current_mode)

    # ── Telegram setup ────────────────────────────────────────────────────
    if "telegram" in platforms:
        print("\n── Telegram ──")
        current = env.get("TELEGRAM_BOT_TOKEN", "")
        need_new_token = False
        if current and current != "your_telegram_bot_token":
            # Validate existing token
            print(f"  Token: {current[:10]}...{current[-4:]}", end="")
            bot_name = _validate_telegram_token(current)
            if bot_name:
                print(f"  ✓ {bot_name}")
                if input("  Change? [y/N]: ").strip().lower() == "y":
                    need_new_token = True
                # else: keep existing, skip new token prompt
            else:
                print("  ✗ invalid")
                need_new_token = True
        else:
            need_new_token = True
        if need_new_token:
            print("  Get a token: open Telegram → @BotFather → /newbot")
            while True:
                token = input("  Bot token: ").strip()
                if not token:
                    print("  ⚠ Skipped — set TELEGRAM_BOT_TOKEN in .env later")
                    break
                bot_name = _validate_telegram_token(token)
                if bot_name:
                    _set_env_var(env_path, "TELEGRAM_BOT_TOKEN", token)
                    print(f"  ✓ Bot verified: {bot_name}")
                    break
                print("  ✗ Invalid token. Check and try again (Enter to skip).")

        current_ids = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        print(f"  Allowed user IDs: {current_ids or '(everyone)'}")
        print("  Find your ID: message @userinfobot on Telegram")
        ids = input("  Telegram user IDs (comma-sep, enter to keep, 'none' for all): ").strip()
        if ids.lower() == "none":
            _set_env_var(env_path, "TELEGRAM_ALLOWED_USER_IDS", "")
        elif ids:
            _set_env_var(env_path, "TELEGRAM_ALLOWED_USER_IDS", ids)

    # ── WhatsApp setup ────────────────────────────────────────────────────
    if "whatsapp" in platforms:
        print("\n── WhatsApp (Green API) ──")
        print("  Sign up free: https://console.green-api.com")

        current_id = env.get("GREEN_API_INSTANCE_ID", "")
        current_tk = env.get("GREEN_API_TOKEN", "")
        if current_id and current_tk:
            print(f"  Instance: {current_id}", end="")
            state = _validate_green_api(current_id, current_tk)
            if state:
                print(f"  ✓ {state}")
            else:
                print("  ✗ invalid")
                current_id = ""
            if current_id and input("  Change? [y/N]: ").strip().lower() == "y":
                current_id = ""

        if not current_id:
            while True:
                val_id = input("  Instance ID (idInstance): ").strip()
                val_tk = input("  API Token (apiTokenInstance): ").strip()
                if not val_id or not val_tk:
                    print("  ⚠ Skipped — set GREEN_API_INSTANCE_ID and GREEN_API_TOKEN later")
                    break
                state = _validate_green_api(val_id, val_tk)
                if state:
                    _set_env_var(env_path, "GREEN_API_INSTANCE_ID", val_id)
                    _set_env_var(env_path, "GREEN_API_TOKEN", val_tk)
                    print(f"  ✓ Connected (status: {state})")
                    if state == "notAuthorized":
                        print("  ⚠ Scan QR code in Green API console to link WhatsApp")
                    break
                print("  ✗ Invalid credentials. Try again (Enter to skip).")

        # ── WhatsApp allowed numbers ──────────────────────────────────
        current_nums = env.get("WHATSAPP_ALLOWED_NUMBERS", "")
        print(f"\n  Allowed WhatsApp numbers: {current_nums or '(everyone)'}")
        print("  Format: country code + number, no '+' or spaces")
        print("  Example: 919876543210,14155552671")
        print("  Tip: send !id to the bot to discover your number")
        nums = input("  Numbers (comma-sep, enter to keep, 'none' for all): ").strip()
        if nums.lower() == "none":
            _set_env_var(env_path, "WHATSAPP_ALLOWED_NUMBERS", "")
            print("  → allowing everyone")
        elif nums:
            clean = re.sub(r"[\s+\-()]", "", nums)
            _set_env_var(env_path, "WHATSAPP_ALLOWED_NUMBERS", clean)
            print(f"  → WHATSAPP_ALLOWED_NUMBERS={clean}")

    # ── Slack setup ───────────────────────────────────────────────────────
    if "slack" in platforms:
        print("\n── Slack ──")
        print("  Create app: https://api.slack.com/apps")

        current = env.get("SLACK_BOT_TOKEN", "")
        if current and current.startswith("xoxb-"):
            print(f"  Bot Token: {current[:10]}...{current[-4:]}", end="")
            team = _validate_slack_token(current)
            if team:
                print(f"  ✓ team: {team}")
            else:
                print("  ✗ invalid")
                current = ""
            if current and input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        else:
            current = ""

        if not current:
            while True:
                val = input("  Bot Token (xoxb-...): ").strip()
                if not val:
                    print("  ⚠ Skipped — set SLACK_BOT_TOKEN later")
                    break
                if not val.startswith("xoxb-"):
                    print("  ✗ Must start with xoxb-. You may have the User OAuth Token (xoxp-) instead.")
                    print("    Go to OAuth & Permissions → copy 'Bot User OAuth Token'.")
                    continue
                team = _validate_slack_token(val)
                if team:
                    _set_env_var(env_path, "SLACK_BOT_TOKEN", val)
                    print(f"  ✓ Slack verified: team {team}")
                    break
                print("  ✗ Invalid token. Try again (Enter to skip).")

        current = env.get("SLACK_APP_TOKEN", "")
        if current and current.startswith("xapp-"):
            print(f"  App Token: {current[:10]}...{current[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current = ""
        else:
            current = ""
        if not current:
            val = input("  App Token (xapp-...): ").strip()
            if val:
                if not val.startswith("xapp-"):
                    print("  ⚠ App tokens typically start with xapp-. Saving anyway.")
                _set_env_var(env_path, "SLACK_APP_TOKEN", val)

        current_ids = env.get("SLACK_ALLOWED_USER_IDS", "")
        print(f"  Allowed Slack user IDs: {current_ids or '(everyone)'}")
        print("  Find yours: click profile pic → Profile → ⋮ → Copy member ID")
        ids = input("  Slack member IDs (comma-sep, enter to keep, 'none' for all): ").strip()
        if ids.lower() == "none":
            _set_env_var(env_path, "SLACK_ALLOWED_USER_IDS", "")
        elif ids:
            _set_env_var(env_path, "SLACK_ALLOWED_USER_IDS", ids)

    # ── Web chat setup ───────────────────────────────────────────────────
    if "web" in platforms:
        print("\n── Web Chat ──")
        current_port = env.get("WEB_CHAT_PORT", "8585")
        port = input(f"  Port [enter to keep {current_port}]: ").strip()
        if port and port.isdigit():
            _set_env_var(env_path, "WEB_CHAT_PORT", port)
            print(f"  → WEB_CHAT_PORT={port}")

        current_token = env.get("WEB_CHAT_TOKEN", "")
        if current_token:
            print(f"  Access token: {current_token[:4]}...{current_token[-4:]}")
            if input("  Change? [y/N]: ").strip().lower() == "y":
                current_token = ""
        if not current_token:
            print("  Set a token to restrict access (recommended).")
            print("  Leave empty to allow anyone with the URL.")
            token = input("  Access token (Enter to skip): ").strip()
            if token:
                _set_env_var(env_path, "WEB_CHAT_TOKEN", token)
                print(f"  ✓ Token set")
            else:
                print("  ⚠ No token — web chat is open to anyone with the URL")

    # ── Claude settings ───────────────────────────────────────────────────
    print("\n── Claude ──")
    current_cmode = env.get("CLAUDE_MODE", "cli")
    print(f"  Mode: {current_cmode} (cli = Claude Code CLI, api = Anthropic API)")
    cmode = input(f"  Claude mode [enter to keep '{current_cmode}']: ").strip().lower()
    if cmode in ("cli", "api"):
        _set_env_var(env_path, "CLAUDE_MODE", cmode)
        current_cmode = cmode

    if current_cmode == "api":
        current_key = env.get("ANTHROPIC_API_KEY", "")
        if current_key and current_key != "your_api_key_here":
            print(f"  API Key: {current_key[:8]}...{current_key[-4:]}")
        else:
            key = input("  Anthropic API key: ").strip()
            if key:
                _set_env_var(env_path, "ANTHROPIC_API_KEY", key)

    # ── Optional features ─────────────────────────────────────────────────
    print("\n── Optional Features ──")
    print("  These are optional — you can enable them later in .env")
    print("  1) Voice transcription (OpenAI Whisper)")
    print("  2) Text-to-speech (OpenAI TTS)")
    print("  3) Image generation (DALL-E 3)")
    print("  4) Web search (Brave / Tavily)")
    print("  5) Web fetch (extract readable URL content)")
    print("  6) Music generation (Replicate)")
    print("  7) Video generation (Replicate)")
    feat_choice = input("  Enable features (e.g. '1,2,3', Enter = skip all): ").strip()
    features = {int(x.strip()) for x in feat_choice.split(",") if x.strip().isdigit()} if feat_choice else set()

    if features & {1, 2, 3}:
        current_oai = env.get("OPENAI_API_KEY", "")
        if not current_oai:
            oai_key = input("  OpenAI API key (sk-...): ").strip()
            if oai_key:
                _set_env_var(env_path, "OPENAI_API_KEY", oai_key)
        if 1 in features:
            _set_env_var(env_path, "TRANSCRIPTION_ENABLED", "true")
            print("  ✓ Voice transcription enabled")
        if 2 in features:
            _set_env_var(env_path, "TTS_ENABLED", "true")
            print("  ✓ Text-to-speech enabled")
        if 3 in features:
            _set_env_var(env_path, "IMAGE_GEN_ENABLED", "true")
            print("  ✓ Image generation enabled")

    if 4 in features:
        _set_env_var(env_path, "WEB_SEARCH_ENABLED", "true")
        print("  Search provider:")
        print("    1) Brave Search — https://api.search.brave.com (free: 2000/month)")
        print("    2) Tavily — https://tavily.com (free: 1000/month)")
        sp = input("  Choose (1/2): ").strip()
        if sp == "1":
            key = input("  Brave Search API key: ").strip()
            if key:
                _set_env_var(env_path, "BRAVE_SEARCH_API_KEY", key)
        elif sp == "2":
            key = input("  Tavily API key: ").strip()
            if key:
                _set_env_var(env_path, "TAVILY_API_KEY", key)
        print("  ✓ Web search enabled")

    if 5 in features:
        _set_env_var(env_path, "WEB_FETCH_ENABLED", "true")
        jina = input("  Jina Reader API key (optional, Enter to skip): ").strip()
        if jina:
            _set_env_var(env_path, "JINA_API_KEY", jina)
        print("  ✓ Web fetch enabled")

    if features & {6, 7}:
        current_rep = env.get("REPLICATE_API_TOKEN", "")
        if not current_rep:
            rep_key = input("  Replicate API token — https://replicate.com : ").strip()
            if rep_key:
                _set_env_var(env_path, "REPLICATE_API_TOKEN", rep_key)
        if 6 in features:
            _set_env_var(env_path, "MUSIC_GEN_ENABLED", "true")
            print("  ✓ Music generation enabled")
        if 7 in features:
            _set_env_var(env_path, "VIDEO_GEN_ENABLED", "true")
            print("  ✓ Video generation enabled")

    # Persist the directory containing .env as the workdir (shared with npm CLI)
    _save_workdir(os.path.dirname(os.path.abspath(env_path)))

    # ── Summary ───────────────────────────────────────────────────────────
    final_env = _read_env(env_path)
    print(f"\n── Setup Complete ──")

    tg_token = final_env.get("TELEGRAM_BOT_TOKEN", "")
    has_tg = bool(tg_token and tg_token != "your_telegram_bot_token")
    wa_id = final_env.get("GREEN_API_INSTANCE_ID", "")
    has_wa = bool(wa_id and final_env.get("GREEN_API_TOKEN"))
    sl_token = final_env.get("SLACK_BOT_TOKEN", "")
    has_sl = bool(sl_token and sl_token.startswith("xoxb-"))
    has_web = "web" in _parse_platforms(final_env.get("BOT_MODE", "telegram"))

    print(f"  Telegram : {'✓ configured' if has_tg else '── skipped'}")
    print(f"  WhatsApp : {'✓ configured' if has_wa else '── skipped'}")
    print(f"  Slack    : {'✓ configured' if has_sl else '── skipped'}")
    web_port = final_env.get("WEB_CHAT_PORT", "8585")
    print(f"  Web      : {'✓ http://localhost:' + web_port if has_web else '── skipped'}")
    print(f"  Claude   : {final_env.get('CLAUDE_MODE', 'cli')} mode")
    print(f"  Config   : {env_path}")

    if not has_tg and not has_wa and not has_sl and not has_web:
        print("\n  ⚠ No platform configured. The bot won't start without credentials.")
        print("  Run 'telechat init' again or edit .env manually.")
    else:
        # Security warnings
        warnings = []
        if has_tg and not final_env.get("TELEGRAM_ALLOWED_USER_IDS"):
            warnings.append("Telegram: no user restriction (anyone can message the bot)")
        if has_wa and not final_env.get("WHATSAPP_ALLOWED_NUMBERS"):
            warnings.append("WhatsApp: no number restriction (anyone can message the bot)")
        if has_sl and not final_env.get("SLACK_ALLOWED_USER_IDS"):
            warnings.append("Slack: no user restriction (anyone in workspace can use the bot)")
        if has_web and not final_env.get("WEB_CHAT_TOKEN"):
            warnings.append("Web: no access token (anyone with the URL can chat)")
        if warnings:
            print("\n  ⚠ Security:")
            for w in warnings:
                print(f"    • {w}")

        if has_web:
            from .qr_util import print_web_qr
            print_web_qr(web_port)

        print("\n  Run 'telechat' or 'telechat start' to launch the bot.")


def _parse_platforms(mode: str) -> set[str]:
    aliases = {"both": {"telegram", "whatsapp"}, "all": {"telegram", "whatsapp", "slack", "web"}}
    mode = mode.lower().strip()
    if mode in aliases:
        return aliases[mode]
    return {p.strip() for p in mode.split(",") if p.strip()}


# ─── Single-instance enforcement ─────────────────────────────────────────────
# Starting the bot replaces any instance already running. The original
# implementation did that with `pgrep -f telechat_pkg.main` and SIGTERMed every
# match — which also matched an editor with main.py open, a `grep` for the
# string, or a pytest run, and did not filter by user. A PID file written by
# the running bot is the precise answer; the pgrep sweep survives only as a
# fallback for instances that predate the PID file, and every candidate is now
# checked against its actual argv before it is signalled.

#: Shared with the npm wrapper, which has written this file since before the
#: Python side had one (npm/bin/telechat.js). One file means `telechat stop`
#: from npm can stop a bot that was started by the pip entry point, and vice
#: versa.
_PID_FILE_NAME = ".telechat.pid"

#: argv0 basenames that mean "this process *is* the bot".
_BOT_ARGV0 = ("telechat",)

#: Module paths that mean "this python process is running the bot".
_BOT_MODULES = ("telechat_pkg.main", "telechat_pkg")


def _pid_file_path() -> str:
    """Path of the single-instance PID file inside the data home."""
    home = os.environ.get("TELECHAT_HOME") or _DATA_HOME
    return os.path.join(home, _PID_FILE_NAME)


def _is_telechat_cmdline(cmdline: str) -> bool:
    """True if ``cmdline`` is a telechat bot process rather than a mention of one.

    ``pgrep -f`` matches the whole command line, so `grep telechat_pkg.main`,
    `vim telechat_pkg/main.py`, and `pytest tests/test_main.py` all matched the
    old check. Only three shapes are the bot: the console script, `python -m
    telechat_pkg.main`, and a direct `python …/telechat_pkg/main.py`.
    """
    if not cmdline:
        return False
    import shlex
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    if not tokens:
        return False
    argv0 = os.path.basename(tokens[0])
    if argv0 in _BOT_ARGV0:
        return True
    if not argv0.startswith("python"):
        return False
    for i, tok in enumerate(tokens[1:], start=1):
        if tok == "-m":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            return nxt in _BOT_MODULES
        if tok.endswith(os.path.join("telechat_pkg", "main.py")):
            return True
    return False


def _process_cmdline(pid: int) -> str:
    """argv of ``pid`` as one string, or "" when it can't be determined."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)],
            text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return out.strip().splitlines()[0].strip() if out.strip() else ""


def _read_pid_file() -> int | None:
    """PID recorded by a previous run, or None if absent/unreadable/garbage."""
    try:
        with open(_pid_file_path()) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid_file() -> None:
    """Record this process as the running instance. Best effort."""
    path = _pid_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
    except OSError:
        pass


def _remove_pid_file() -> None:
    """Remove the PID file, but only if it still names *this* process.

    A replaced instance must not delete its successor's file: the new bot
    overwrites the PID before the old one finishes shutting down.
    """
    try:
        if _read_pid_file() == os.getpid():
            os.unlink(_pid_file_path())
    except OSError:
        pass


def _terminate_previous_instances(log=None, health_port: int | None = None) -> list[int]:
    """SIGTERM any other running telechat bot. Returns the PIDs signalled.

    Every candidate — from the PID file, the pgrep fallback, or the health port
    — is checked against its own argv first, so nothing that merely *mentions*
    telechat gets killed.
    """
    import subprocess

    my_pid = os.getpid()
    signalled: list[int] = []

    def _try_kill(pid: int, source: str) -> None:
        if pid == my_pid or pid in signalled or pid <= 0:
            return
        if not _is_telechat_cmdline(_process_cmdline(pid)):
            if log:
                log.debug("Ignoring PID %d from %s — not a telechat process", pid, source)
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        signalled.append(pid)
        if log:
            log.info("Stopped existing telechat process (PID %d, via %s)", pid, source)

    recorded = _read_pid_file()
    if recorded:
        _try_kill(recorded, "pid file")

    # Fallback for instances started before the PID file existed. `-u` keeps the
    # sweep inside this user's processes; the argv check in _try_kill does the
    # rest.
    try:
        out = subprocess.check_output(
            ["pgrep", "-u", str(os.getuid()), "-f", "telechat_pkg.main"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, AttributeError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        try:
            _try_kill(int(line.strip()), "pgrep")
        except ValueError:
            continue

    # A previous run may still be holding the health port — that binds before
    # anything else and would make this start fail. Same argv check applies: if
    # some *other* service owns the port, say so and leave it alone. Failing to
    # bind is a much better outcome than killing the user's dev server.
    if health_port:
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{health_port}"], text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid == my_pid or pid in signalled:
                continue
            if _is_telechat_cmdline(_process_cmdline(pid)):
                _try_kill(pid, f"port {health_port}")
            elif log:
                log.warning(
                    "Port %d is held by PID %d, which is not telechat — leaving it alone. "
                    "Set HEALTH_PORT to a free port.", health_port, pid,
                )

    return signalled



# ─── Start command (heavy setup deferred here) ───────────────────────────────

def _cmd_start() -> None:
    """Load config and start the bot."""
    import asyncio
    import logging
    import logging.handlers
    import threading

    from dotenv import load_dotenv

    # Load the resolved .env explicitly. Bare load_dotenv() searches upward
    # from the package directory (wrong for pip installs) — it would never
    # find ~/.telechat/.env. Always pass the data-home path.
    _env_path = _find_env_file()
    load_dotenv(_env_path, override=True)

    _debug = os.getenv("TELECHAT_DEBUG", "").lower() in ("1", "true", "yes")
    _log_level = logging.DEBUG if _debug else logging.INFO

    _console = logging.StreamHandler()
    _console.setLevel(logging.WARNING if not _debug else logging.DEBUG)

    _file = logging.handlers.RotatingFileHandler(
        "bot.log", maxBytes=5_000_000, backupCount=3
    )
    _file.setLevel(_log_level)

    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[_console, _file],
    )
    log = logging.getLogger(__name__)

    # ── Parse BOT_MODE ────────────────────────────────────────────────────
    _raw_mode = os.getenv("BOT_MODE", "telegram").lower().strip()
    _ALIASES = {"both": {"telegram", "whatsapp"}, "all": {"telegram", "whatsapp", "slack", "web"}}

    if _raw_mode in _ALIASES:
        PLATFORMS: set[str] = _ALIASES[_raw_mode]
    else:
        PLATFORMS = {p.strip() for p in _raw_mode.split(",") if p.strip()}

    _VALID = {"telegram", "whatsapp", "slack", "web"}
    _unknown = PLATFORMS - _VALID
    if _unknown:
        print(f"ERROR: Unknown platform(s) in BOT_MODE: {_unknown}")
        print(f"Valid values: {_VALID} (or 'both', 'all')")
        print("Run 'telechat init' to configure.")
        sys.exit(1)

    log.info("Platforms enabled: %s", ", ".join(sorted(PLATFORMS)))

    # ── WhatsApp pre-flight check ─────────────────────────────────────────
    if "whatsapp" in PLATFORMS:
        wa_nums = os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").strip()
        if not wa_nums:
            print("Note: WHATSAPP_ALLOWED_NUMBERS is empty — anyone can message the bot.")
            print("      Send !id to the bot to find your number, then run 'telechat init'")
            print("      or set WHATSAPP_ALLOWED_NUMBERS in .env to restrict access.\n")
        gid = os.getenv("GREEN_API_INSTANCE_ID", "").strip()
        gtk = os.getenv("GREEN_API_TOKEN", "").strip()
        if not gid or not gtk:
            print("ERROR: WhatsApp enabled but GREEN_API_INSTANCE_ID / GREEN_API_TOKEN not set.")
            print("Run 'telechat init' to configure.")
            sys.exit(1)

    # ── Replace any existing instance ─────────────────────────────────────
    # Use the configured health port so we don't touch an unrelated service on
    # :8484 when HEALTH_PORT has been overridden. Reading the env directly
    # (instead of importing health) keeps this safe in test contexts where main
    # is imported outside the package.
    try:
        _health_port = int(os.getenv("HEALTH_PORT", "8484"))
    except ValueError:
        _health_port = 8484
    _terminate_previous_instances(log, health_port=_health_port)
    _write_pid_file()
    import atexit
    atexit.register(_remove_pid_file)

    # ── Background thread wrappers ────────────────────────────────────────
    def _run_whatsapp() -> None:
        from .whatsapp_bot import run_whatsapp
        try:
            run_whatsapp()
        except Exception:
            log.exception("WhatsApp bot crashed")

    def _run_slack() -> None:
        from .slack_bot import run_slack
        try:
            run_slack()
        except Exception:
            log.exception("Slack bot crashed")

    # ── Async main ────────────────────────────────────────────────────────
    async def _main() -> None:
        await asyncio.sleep(1)

        from . import health
        health.start_health_server()

        # Auto-update checker: notify (and optionally self-upgrade) when a newer
        # telechat is published. Runs off-thread; never blocks startup.
        try:
            from . import updater
            updater.start_background_check()
        except Exception as _upd_exc:  # pragma: no cover - defensive
            log.debug("update checker not started: %s", _upd_exc)

        platforms = ", ".join(sorted(PLATFORMS))
        print(f"telechat — {platforms}")
        if _debug:
            print("Debug mode ON (verbose logging)")

        if "whatsapp" in PLATFORMS:
            threading.Thread(target=_run_whatsapp, daemon=True, name="whatsapp").start()

        if "slack" in PLATFORMS:
            threading.Thread(target=_run_slack, daemon=True, name="slack").start()

        web_task = None
        if "web" in PLATFORMS:
            from .web_chat import run_web_chat
            web_task = asyncio.create_task(run_web_chat())

        if "telegram" in PLATFORMS:
            from .telegram_bot import run_telegram
            await run_telegram()
        elif web_task:
            await web_task
        else:
            log.info("Running without Telegram. Press Ctrl-C to stop.")
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nShutting down…")
        try:
            from . import store
            # Drains the queue *and* stops the writer thread so its SQLite
            # connection is closed rather than killed at interpreter exit.
            store.shutdown_writer(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
    except RuntimeError as e:
        # Missing token or similar misconfiguration — clean message, no traceback
        msg = str(e)
        if "TOKEN" in msg or "not set" in msg:
            print(f"\n  ✗ Configuration error: {msg}")
            _print_setup_guidance()
            sys.exit(1)
        raise


# ─── CLI entry point ─────────────────────────────────────────────────────────

_sigint_count = 0


def _sigint_handler(sig, frame):
    """Handle Ctrl-C.

    The first SIGINT raises KeyboardInterrupt so the running event loop unwinds
    cleanly (writer thread drains, aiohttp runners clean up). A second SIGINT
    forces an immediate hard exit in case something is wedged.
    """
    global _sigint_count
    _sigint_count += 1
    if _sigint_count == 1:
        print("\nShutting down… (press Ctrl-C again to force quit)")
        # Raising in a signal handler interrupts the running asyncio.run()
        # and lets the KeyboardInterrupt path above run flush_writes() and
        # any aiohttp runner cleanup.
        raise KeyboardInterrupt
    else:
        # Second Ctrl-C — give up on graceful shutdown.
        os._exit(1)


def cli_entry():
    """Entry point for `pip install telechatai` → `telechat` command."""
    signal.signal(signal.SIGINT, _sigint_handler)

    args = sys.argv[1:]
    cmd = args[0] if args else "start"

    # Resolve the working directory saved by `telechat init` (npm CLI shares
    # this config). Ensures the pip-installed entry point behaves the same
    # no matter which directory it's invoked from.
    _resolve_workdir()

    if cmd == "init":
        _cmd_init()
    elif cmd == "bridge":
        # Claude Desktop bridge: install/uninstall hooks, or hook entrypoints
        # (notify / approve). All persistence is in telechat's own bot.db.
        from .desktop_bridge import cli_dispatch as _bridge_cli
        sys.exit(_bridge_cli(args[1:]))
    elif cmd in ("start", "run"):
        # Pre-flight: no usable config → guidance, not a traceback
        env = _read_env(_find_env_file())
        if not _has_any_platform(env):
            _print_setup_guidance()
            sys.exit(1)
        _cmd_start()
    elif cmd in ("-h", "--help", "help"):
        print("Usage: telechat [command]")
        print()
        print("Commands:")
        print("  init                          Interactive setup wizard (creates/updates .env)")
        print("  start                         Start the bot (default)")
        print("  bridge install [--approval]   Full Claude Desktop bridge setup:")
        print("                                  hooks + persistent service + checks")
        print("  bridge uninstall              Remove bridge hooks")
        print("  bridge status                 Show running Claude Desktop sessions")
        print("  bridge service <install|uninstall|status>   Manage the persistent service")
        print("  help                          Show this help")
        print()
        print("Examples:")
        print("  telechat init                       # configure platforms & credentials")
        print("  telechat                            # start the bot")
        print("  telechat bridge install             # enable bridge + persistent service")
        print("  telechat bridge install --approval  # also gate Bash/Write/Edit on Telegram approval")
        print("  telechat bridge install --no-service  # hooks only, skip the launchd service")
    elif cmd == "--version":
        try:
            from importlib.metadata import version
            print(f"telechat {version('telechatai')}")
        except Exception:
            print("telechat (unknown version)")
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'telechat help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    cli_entry()
