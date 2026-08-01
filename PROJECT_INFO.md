# Telechat — PROJECT_INFO

**Self-hosted Claude bot that serves Telegram, WhatsApp, Slack, Discord and a local web chat from a single process — plus a Claude Desktop bridge that pages you when a Claude Code session stalls on a question.**

- **Repo:** https://github.com/telechatai/telechat · branch `main`
- **Local path:** `claudeplus/telechat`
- **Published as:** [`telechat`](https://www.npmjs.com/package/telechat) (npm) and [`telechatai`](https://pypi.org/project/telechatai/) (PyPI) — package version 1.2.0
- **Site:** [telechat.fyi](https://telechat.fyi) · miniapp at `telechat.app/miniapp`
- **License:** MIT

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python — `telechat_pkg/`, packaged via `pyproject.toml` |
| Entry point | `telechat = telechat_pkg.main:cli_entry` |
| Distribution | npm wrapper (`npm/`) + PyPI wheel; Docker image via `Dockerfile` |
| Claude access | **CLI mode** — drives the `claude` CLI, no API key needed with a Claude subscription; **API mode** — Anthropic API directly, works in Docker |
| Channels | Telegram, WhatsApp, Slack, Discord, local web chat |
| Tests | pytest — CI in `.github/workflows/pytest.yml` |

## Deploy location

| | |
|---|---|
| Runtime | **This machine**, as a launchd agent `com.telechat.bot` |
| Deployed copy | `~/.telechat` — **not this repo.** The repo is source; the running copy lives in your home dir. |
| Web chat port | `127.0.0.1:8484` (per `docker-compose.yml`) |
| Docker | `docker-compose.yml` in the repo root for containerized runs |
| Public | npm + PyPI registries; `telechat.fyi` static site under `website/` |

> Editing this repo does not change the running bot. Reinstall (`npm i -g telechat` or
> `pip install -U telechatai`) or sync into `~/.telechat`, then restart the launchd agent.

## SSH key

None — there is no remote server. Deployment is package publication plus a local launchd
agent. Publish tokens live outside the repo:

- `~/projects/keys/npm_token.txt` — npm publish token
- `~/projects/keys/pypi-token.txt` — PyPI publish token
- `~/projects/keys/PyPI-Recovery-Codes-r2st-*.txt` — PyPI 2FA recovery codes

## Environment variables

| Where | What |
|---|---|
| `.env` (gitignored) | Telegram bot token; channel tokens and Anthropic key in API mode |
| `~/.telechat/` | The **live** config used by the running agent |
| `telechat init` | Interactive setup that writes the config |

This repo has no `keys/` directory.

## Key commands

```bash
# Install / run
npm install -g telechat
telechat init
telechat                       # start

# From source
pip install -e .
pytest

# Docker
docker compose up -d           # web chat on 127.0.0.1:8484

# The live agent on this machine
launchctl list | grep telechat
launchctl kickstart -k gui/$(id -u)/com.telechat.bot
```

## Related projects

- `claudeplus/telechat-website` — the marketing site (**not a git repo** on this machine)
- `claudeplus/telechat-features` — feature-work checkout of the same package
- `knol-local` — the other published package from this estate (npm, MCP memory server)
- `~/projects/PROJECT-INDEX.md`, `~/projects/keys/KEYS_INDEX.md` — estate-wide index
