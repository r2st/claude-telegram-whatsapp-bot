"""
Discord adapter — gateway WebSocket (no webhook, no public URL).

Like the Slack adapter, everything is outbound: discord.py opens a WebSocket to
Discord's gateway and keeps it open, so this works behind NAT, on a laptop, and
on a corporate network with no inbound rule and nothing to expose.

Setup (one-time, ~3 min):
  1. https://discord.com/developers/applications → New Application
  2. Bot → Add Bot → Reset Token → copy it into DISCORD_BOT_TOKEN
  3. Bot → Privileged Gateway Intents → enable **Message Content Intent**
     (without it Discord delivers empty message bodies and the bot looks
     broken — `run_discord` refuses to start rather than let that happen
     silently)
  4. OAuth2 → URL Generator → scopes: bot; permissions: Send Messages,
     Read Message History → open the generated URL to invite it to a server
  5. Set DISCORD_ALLOWED_USER_IDS to your numeric user id (Discord →
     Settings → Advanced → Developer Mode, then right-click yourself → Copy
     User ID). Empty means anyone who can see the bot can use it.

In a server the bot answers when mentioned; in a DM it answers anything.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from . import claude_core as cc
from .memory import MemoryStore
from .text_chunking import chunk_text

log = logging.getLogger(__name__)

PLATFORM = "discord"

#: Discord's hard limit for a single message body. The chunker is given a
#: little room below it for the "(2/3)" counter this module adds.
DISCORD_MESSAGE_LIMIT = 2000
_CHUNK_LIMIT = 1900


def _allowed_users() -> list[str]:
    """Read the allowlist at call time so tests and `telechat init` see edits."""
    return [u.strip() for u in os.getenv("DISCORD_ALLOWED_USER_IDS", "").split(",") if u.strip()]


def is_allowed(user_id: str) -> bool:
    """Empty allowlist means everyone, matching the other adapters.

    That default is documented as a risk rather than fixed here: changing it
    for Discord alone would make one platform behave unlike the other three.
    """
    allowed = _allowed_users()
    return not allowed or str(user_id) in allowed


# ─── Per-user settings (in-memory, like the Slack adapter) ────────────────────

_user_model: dict[str, str] = {}
_user_engine: dict[str, str] = {}

_DEFAULT_MODEL = os.getenv("CLAUDE_CLI_MODEL", cc.CLAUDE_MODEL)
_DEFAULT_ENGINE = cc.CLAUDE_MODE

CLI_MODELS = {"haiku": "Haiku (fastest)", "sonnet": "Sonnet (balanced)", "opus": "Opus (most capable)"}
ENGINE_MODES = {"cli": "CLI (subprocess)", "api": "API (Anthropic Messages)"}


def _model(uid: str) -> str:
    return _user_model.get(uid, _DEFAULT_MODEL)


def _engine(uid: str) -> str:
    return _user_engine.get(uid, _DEFAULT_ENGINE)


_memory = MemoryStore()


# ─── Message plumbing ────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"<@!?\d+>")


def strip_mentions(text: str) -> str:
    """Drop the `<@1234>` the client sends when you @ the bot."""
    return _MENTION_RE.sub("", text or "").strip()


def split_for_discord(text: str) -> list[str]:
    """Split a reply into sendable messages.

    Uses the shared chunker rather than a fixed slice: a naive split lands
    inside code fences, which Discord then renders as an unterminated block
    and a stray ``` on the next message.
    """
    if not text:
        return []
    chunks = chunk_text(text, limit=_CHUNK_LIMIT)
    if len(chunks) == 1:
        return [chunks[0].text]
    out = []
    for c in chunks:
        body = f"{c.text}\n\n*({c.index + 1}/{c.total})*"
        # The counter must not be what pushes a chunk over the limit.
        out.append(body if len(body) <= DISCORD_MESSAGE_LIMIT else c.text)
    return out


# ─── Commands ────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "**Claude on Discord**\n\n"
    "Mention me in a channel, or just talk to me in a DM.\n\n"
    "**Settings**\n"
    "`!model <haiku|sonnet|opus>` — pick the model\n"
    "`!engine <cli|api>` — pick the backend\n"
    "`!status` — show the current settings\n"
    "`!usage` — tokens used\n\n"
    "**Conversation**\n"
    "`!reset` — clear this conversation\n"
    "`!sessions` — list your sessions\n"
    "`!new <name>` — start a named session\n"
    "`!switch <name>` — switch to a session\n\n"
    "**Memory**\n"
    "`!remember <text>` — save a memory (`#tag`, `!importance`)\n"
    "`!recall <query>` — search memories\n"
    "`!memories` — list saved memories\n"
    "`!forget <id>` — delete one\n"
)


def _parse_remember_args(text: str) -> tuple[str, list[str], float]:
    """Same `#tag` / `!importance` syntax the Slack adapter accepts."""
    tags: list[str] = []
    importance = 0.5
    words: list[str] = []
    for word in text.split():
        if word.startswith("#") and len(word) > 1:
            tags.append(word[1:].lower())
        elif word.startswith("!") and len(word) > 1:
            try:
                importance = float(word[1:])
            except ValueError:
                words.append(word)
        else:
            words.append(word)
    return " ".join(words), tags, importance


def handle_command(user_id: str, text: str) -> str | None:
    """Run a `!command` and return what to say, or None if it isn't one.

    Kept free of any discord.py object so the command surface can be tested
    without a gateway connection — the library only appears in `run_discord`
    and the client class below.
    """
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None

    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("help", "commands"):
        return HELP_TEXT

    if cmd == "model":
        if not arg:
            options = ", ".join(f"`{k}`" for k in CLI_MODELS)
            return f"Model: **{_model(user_id)}**\nAvailable: {options}"
        choice = arg.lower()
        if choice not in CLI_MODELS:
            return f"Unknown model `{choice}`. Pick one of: " + ", ".join(f"`{k}`" for k in CLI_MODELS)
        _user_model[user_id] = choice
        return f"Model set to **{choice}** — {CLI_MODELS[choice]}"

    if cmd == "engine":
        if not arg:
            options = ", ".join(f"`{k}`" for k in ENGINE_MODES)
            return f"Engine: **{_engine(user_id)}**\nAvailable: {options}"
        choice = arg.lower()
        if choice not in ENGINE_MODES:
            return f"Unknown engine `{choice}`. Pick one of: " + ", ".join(f"`{k}`" for k in ENGINE_MODES)
        _user_engine[user_id] = choice
        return f"Engine set to **{choice}** — {ENGINE_MODES[choice]}"

    if cmd in ("status", "mode"):
        sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        return (
            f"**Model:** {_model(user_id)}\n"
            f"**Engine:** {_engine(user_id)}\n"
            f"**Session:** {sess.name}"
        )

    if cmd in ("usage", "stats"):
        used = cc.get_usage(PLATFORM, user_id)
        return (
            f"**Tokens in:** {used.get('input_tokens', 0):,}\n"
            f"**Tokens out:** {used.get('output_tokens', 0):,}"
        )

    if cmd == "reset":
        sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        cc.clear_history(PLATFORM, user_id, session_name=sess.name)
        return "Conversation cleared."

    if cmd == "sessions":
        sessions = cc._session_mgr.get_all(PLATFORM, user_id)
        if not sessions:
            return "No sessions yet."
        active = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
        lines = [
            f"{'**→**' if s.name == active.name else '　'} `{s.name}`"
            for s in sessions
        ]
        return "**Sessions**\n" + "\n".join(lines)

    if cmd == "new":
        if not arg:
            return "Give the session a name: `!new refactor`"
        cc._session_mgr.create(PLATFORM, user_id, arg)
        return f"Started session `{arg}`."

    if cmd == "switch":
        if not arg:
            return "Which session? `!switch refactor`"
        if cc._session_mgr.switch_to_name(PLATFORM, user_id, arg):
            return f"Switched to `{arg}`."
        return f"No session called `{arg}`."

    if cmd == "remember":
        if not arg:
            return "What should I remember? `!remember the deploy key is in 1Password #ops`"
        content, tags, importance = _parse_remember_args(arg)
        mem = _memory.remember(PLATFORM, user_id, content, tags=tags, importance=importance)
        tag_str = (" " + " ".join(f"`#{t}`" for t in tags)) if tags else ""
        return f"Remembered (`{mem.id}`){tag_str}."

    if cmd == "recall":
        if not arg:
            return "What should I look for? `!recall deploy key`"
        hits = _memory.recall(PLATFORM, user_id, arg, limit=5)
        if not hits:
            return f"Nothing found for `{arg}`."
        return "**Recalled**\n" + "\n".join(f"• {m.content}" for m in hits)

    if cmd == "memories":
        items = _memory.list_memories(PLATFORM, user_id, limit=10)
        if not items:
            return "No memories saved yet."
        return "**Memories**\n" + "\n".join(f"`{m.id}` {m.content}" for m in items)

    if cmd == "forget":
        if not arg:
            return "Which one? `!forget <id>` — `!memories` lists them."
        return "Forgotten." if _memory.forget(PLATFORM, user_id, arg) else f"No memory `{arg}`."

    return f"Unknown command `!{cmd}`. Try `!help`."


# ─── Claude turn ─────────────────────────────────────────────────────────────

def _is_error(reply: str) -> bool:
    return reply.startswith(("[Error]", "[Claude error]", "[Timeout]", "[SDK Error]"))


async def run_turn(user_id: str, text: str) -> str:
    """Take one message to Claude and return the reply.

    Separated from the client so the whole path — allowlist, rate limit,
    history, save — is testable without a gateway.
    """
    if not is_allowed(user_id):
        log.warning("Discord message from user %s rejected by allowlist", user_id)
        return "You're not on this bot's allowlist."

    if not cc.check_rate_limit(f"{PLATFORM}:{user_id}"):
        return f"Rate limit: max {cc.RATE_LIMIT_REQUESTS} messages per {cc.RATE_LIMIT_WINDOW}s."

    command_reply = handle_command(user_id, text)
    if command_reply is not None:
        return command_reply

    sess = cc._session_mgr.get_or_create_active(PLATFORM, user_id)
    history = cc.load_history(PLATFORM, user_id, session_name=sess.name)

    log.info("← Discord [%s] %s", user_id, text[:120])

    if _engine(user_id) == "api":
        reply, stats = await cc.ask_claude_api_async(text, history)
    else:
        reply, stats = await cc.ask_claude_async(
            text, history,
            model=_model(user_id),
            platform=PLATFORM,
            user_id=user_id,
        )

    # A failed turn is not a turn: storing it would feed the error back as
    # context on the next message.
    if not _is_error(reply):
        cc.save_turn(PLATFORM, user_id, text, reply, session_name=sess.name)
    cc.track_usage(
        PLATFORM, user_id,
        stats.get("input_tokens", 0),
        stats.get("output_tokens", 0),
    )
    log.info("→ Discord [%s] %s", user_id, reply[:120])
    return reply


def should_respond(*, is_dm: bool, is_own_message: bool, was_mentioned: bool) -> bool:
    """A DM is always for us; in a server, only an @mention is.

    Without the own-message check the bot answers itself forever, which is the
    classic way to get an application rate-limited off the gateway.
    """
    if is_own_message:
        return False
    return bool(is_dm or was_mentioned)


# ─── Entry point ─────────────────────────────────────────────────────────────

def _build_client():
    """Construct the gateway client. Imported lazily — discord.py is an extra."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.dm_messages = True

    class TelechatClient(discord.Client):
        async def on_ready(self) -> None:
            log.info("Discord connected as %s", self.user)
            print(f"  Discord: connected as {self.user}")

        async def on_message(self, message) -> None:
            is_dm = message.guild is None
            mentioned = self.user in getattr(message, "mentions", [])
            if not should_respond(
                is_dm=is_dm,
                is_own_message=message.author == self.user,
                was_mentioned=mentioned,
            ):
                return

            text = strip_mentions(message.content)
            if not text:
                return

            user_id = str(message.author.id)
            try:
                async with message.channel.typing():
                    reply = await run_turn(user_id, text)
            except Exception:
                log.exception("Discord handler error")
                reply = "Something went wrong handling that message. Check the bot log."

            for part in split_for_discord(reply):
                await message.channel.send(part)

    return TelechatClient(intents=intents)


def run_discord() -> None:
    """Blocking entry point — `main` runs this on its own thread."""
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_BOT_TOKEN is not set — Discord not started")
        print("  Discord: DISCORD_BOT_TOKEN not set — skipping")
        return

    try:
        import discord  # noqa: F401
    except ImportError:
        # The same shape as the other optional features: say what to install
        # rather than dying on an ImportError traceback.
        log.error("discord.py not installed")
        print("  Discord: not installed — run `pip install 'telechatai[discord]'`")
        return

    cc.init_db()
    log.info("Discord bot starting (gateway)…")
    log.info("Model: %s | Claude mode: %s", cc.CLAUDE_MODEL, cc.CLAUDE_MODE)
    if not _allowed_users():
        log.warning("DISCORD_ALLOWED_USER_IDS is empty — anyone who can see the bot can use it")
        print("  Discord: no allowlist set — anyone who can see the bot can use it")

    client = _build_client()
    # Its own loop: this runs on a thread of its own, so there is no running
    # loop here to join.
    asyncio.run(_run_client(client, token))


async def _run_client(client, token: str) -> None:
    import discord

    try:
        await client.start(token)
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_BOT_TOKEN")
        print("  Discord: token rejected — check DISCORD_BOT_TOKEN")
    except discord.PrivilegedIntentsRequired:
        # Worth its own message: with the intent off, Discord still connects
        # and delivers messages with an empty body, so the bot looks alive and
        # ignores everything.
        log.error("Message Content Intent is not enabled for this Discord application")
        print(
            "  Discord: enable the Message Content Intent at "
            "https://discord.com/developers/applications → Bot → "
            "Privileged Gateway Intents"
        )
    finally:
        if not client.is_closed():
            await client.close()
