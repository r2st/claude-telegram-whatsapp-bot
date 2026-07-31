# Security policy

## Reporting a vulnerability

Report privately, not in a public issue: open a [GitHub security
advisory](https://github.com/telechatai/telechat/security/advisories/new) on
this repository.

Please include what an attacker gets, the smallest reproduction you have, and
the version (`telechat --version`). You will get an acknowledgement, and a fix
or a clear explanation of why it is not one. This is a small self-hosted
project — there is no bounty, and no SLA beyond a genuine effort to respond
quickly.

## What this software is

**telechat is a personal, single-operator tool.** Understanding the trust
boundary matters more here than in most projects, because in its default
configuration the bot is a remote shell:

- In **CLI mode** it runs the `claude` binary with *your* authentication and
  *your* filesystem access. Anyone who can send the bot a message can ask Claude
  to read, write, or execute — bounded only by `CLAUDE_CLI_PERMISSION_MODE` and
  `CLAUDE_CLI_WORK_DIR`.
- Access control is a **flat allowlist per platform**
  (`TELEGRAM_ALLOWED_USER_IDS`, `WHATSAPP_ALLOWED_NUMBERS`,
  `SLACK_ALLOWED_USER_IDS`). Everyone who passes it shares one Claude auth, one
  working directory, and one permission ceiling — and `/permissions` lets any
  allowed user raise it, up to `bypassPermissions`.
- Conversations and memory are keyed per user. **Capability is not.**

That is a reasonable design for the documented use case — you, your phone, your
laptop. It is not a multi-tenant deployment, and adding a second person to the
allowlist grants them what it grants you. If you need per-user isolation, it
does not exist yet; say so in an issue rather than assuming it.

## Running it safely

- **Always set an allowlist.** Empty means anyone who finds the bot can use it.
  `telechat init` warns about this; the warning is not decorative.
- **Scope the working directory.** `CLAUDE_CLI_WORK_DIR` and `CLAUDE_ADD_DIRS`
  are the blast radius. Point them at a projects directory, not `~`.
- **Choose a permission mode deliberately.** `bypassPermissions` means exactly
  that.
- **Keep the web chat on loopback**, or set `WEB_CHAT_TOKEN`. It refuses to
  start exposed and unauthenticated unless you set `WEB_CHAT_ALLOW_OPEN`, and
  that refusal is there for a reason. Do not set `WEB_CHAT_TRUST_PROXY` unless a
  proxy you control is setting `X-Forwarded-For`.
- **The health endpoint is unauthenticated** and binds loopback by default
  (`HEALTH_BIND_ADDR`). It reports status, not secrets, but there is no reason
  to expose it.
- **MCP servers are separate programs.** The command allowlist and the
  world-writable-path check exist because a name on `PATH` is forgeable;
  `MCP_ALLOW_ANY_COMMAND=1` turns all of that off.
- **Bridge approval fails open by default** — an unanswered approval falls
  through to Claude Code's normal permission flow after five minutes. If you
  rely on approval while away from the machine, set
  `BRIDGE_APPROVAL_TIMEOUT_ACTION=deny`.
- **Never commit `.env`.** It is in `.gitignore` and excluded from the Docker
  image. `telechat env` masks tokens when printing; log files do not.

## Supported versions

The latest release on PyPI (`telechatai`) and npm (`telechat`). Fixes go into
the next release rather than being backported.

## Out of scope

- Anything requiring an attacker who already has local access to the machine
  running the bot, or who is already on the platform allowlist. Both are inside
  the trust boundary by design.
- The absence of per-user isolation, documented above. Requests for it are
  feature requests, not vulnerability reports.
- Third-party services the bot talks to — Telegram, Green API, Slack,
  Anthropic, Replicate. Report those to them.
