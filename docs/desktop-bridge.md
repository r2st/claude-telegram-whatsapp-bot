# Claude Code desktop bridge

The bridge is why Telechat exists. Claude Code runs on your desktop, so the
work stops the moment you walk away from that desktop. The bridge closes that
gap: a session that finishes a turn or stalls on a question sends you a card,
you answer from your phone, and the session picks up where it left off.

This page is the setup and troubleshooting guide. For what the cards look like
and how approval rules work, see
[the README's bridge section](../README.md#claude-desktop-bridge).

---

## What it actually is

Three hook entries in `~/.claude/settings.json`, plus the Telegram poller
Telechat already runs. There is no daemon of its own and no second bot.

| Hook | Fires when | Runs |
|---|---|---|
| `Stop` | a session finishes a turn | `telechat bridge notify Stop` |
| `Notification` | a session needs your input | `telechat bridge notify Notification` |
| `SubagentStop` | a subagent finishes | `telechat bridge notify SubagentStop` |
| `PreToolUse` *(with `--approval`)* | before Bash / Write / Edit / MultiEdit | `telechat bridge approve` |

Each hook is a short-lived subprocess. `notify` reads the session transcript,
builds a triage card and posts it to Telegram. `approve` blocks until you tap,
then hands the decision back to Claude Code.

Your replies travel the other way through Telechat's running Telegram poller,
which resumes the session with `claude --resume`.

**Everything stays on your machine** except the card text itself, which goes to
Telegram because that is where you asked to be paged. There is no Telechat
server in the path.

---

## Setup

### 1. Prerequisites

You need three things before the bridge can work end to end. `telechat bridge
install` checks all of them and tells you which are missing, so you do not have
to get this right up front.

- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code && claude auth login`
- **A configured Telegram bot** — `telechat init` collects the token and your
  user id and writes `~/.telechat/.env`
- **A long-lived OAuth token** — `claude setup-token`, then put it in
  `~/.telechat/.env` as `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…`

That last one catches people out. The interactive `claude auth login` session
does **not** authenticate the headless `claude --resume` that replies and
digests run — without a long-lived token, cards arrive but every reply fails
with a 401.

### 2. Install

Run this on the machine you actually code on. The bridge reads sessions from
that machine's `~/.claude`, so it has to be the same box Claude Code runs on.

```bash
telechat bridge install                 # hooks + background service + preflight
telechat bridge install --approval      # also gate Bash/Write/Edit on your tap
telechat bridge install --no-service    # hooks only, skip the launchd service
```

It is idempotent — re-running it is how you upgrade, add `--approval` later, or
repair a `settings.json` someone edited by hand. Hook entries are identified by
what they run, not by a marker key, so re-installing replaces them instead of
stacking duplicates.

### 3. Verify

```bash
telechat bridge status
```

This is the command to reach for whenever the bridge is not behaving. It checks
every link in the chain and exits non-zero if any blocking one is broken, so it
also works as a scripted check:

```
Claude Desktop bridge

  ✓ Claude Code CLI: /Users/you/.local/bin/claude
  ✓ Hooks registered: Stop, Notification, SubagentStop  (/Users/you/.claude/settings.json)
  ✓ Telegram bot token: set
  ✓ Telegram recipient: set
  ✗ Long-lived OAuth token: missing — replies and digests will fail with 401
      Fix: claude setup-token, then add CLAUDE_CODE_OAUTH_TOKEN=… to ~/.telechat/.env
  ✓ Background service: com.telechat.bot loaded
  • Tool approval hook: not registered (optional)

✗ Not ready — 1 thing(s) above must be fixed first.
```

`telechat doctor` reports the same wiring as one line among its other checks.

### 4. First card

Run a Claude Code session and let it finish a turn. The card lands in Telegram.
Reply to it and your message becomes that session's next turn.

---

## Day-to-day

| Command | What it does |
|---|---|
| `/desktop` | List running sessions, tap one to make it current |
| `/desktop_recent` | Browse recent sessions across all projects, running or not |
| `/desktop_use <id>` | Switch to a session by its 8-character short id |
| `/desktop_which` | Which session are replies going to? |
| `/desktop_all <msg>` | Broadcast to every running session |
| `/approvals` | List standing "always allow" rules, with a revoke button each |

When you reply to a card, a **progress card** appears and edits itself as the
turn runs — each tool call as it happens, the model's prose as it arrives, a
step count, and finally ✅ or ❌. A `claude --resume` turn can take minutes, and
without it the phone shows nothing at all until the answer lands, which is
indistinguishable from a reply that silently failed. Turn it off with
`BRIDGE_STREAM=0` if you would rather just get the answer.

In the **web chat**, the 🖥 **Sessions** button opens a live dashboard of every
Claude Code session on the machine — running or recent, which project, working
or idle, and the last thing each one said. `/sessions` prints the same thing
into the conversation.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No cards at all | Hooks not registered — Claude Code rewrote `settings.json`, or the install never ran | `telechat bridge status`, then `telechat bridge install` |
| Cards arrive, replies do nothing | No long-lived OAuth token; the reply 401s | `claude setup-token`, add `CLAUDE_CODE_OAUTH_TOKEN` to `~/.telechat/.env`, restart |
| Cards arrive, replies do nothing, token is set | Telechat itself is not running — cards come from the hook subprocess, but replies need the poller | `telechat status`, then `telechat` or `telechat bridge service install` |
| Cards stop after a reboot | The background service is not loaded | `telechat bridge service status` → `telechat bridge service install` |
| `Bootstrap failed: 5` from launchctl | The service label was explicitly disabled | `launchctl enable gui/$(id -u)/com.telechat.bot`, then re-run the service install |
| Duplicate cards for one event | Two Telechat processes are polling — usually a launchd service *and* a foreground `telechat` | `telechat stop`, then let the service own it |
| Approval prompts never appear | The `PreToolUse` hook was not installed, or approval is not armed for that project | `telechat bridge install --approval`, then `/desktop_approve_on` as a reply to a card from that project |
| Every `git status` asks again | No standing rule yet | Tap **👍 Always allow** on the card instead of **✅ Approve** |
| A reply lands in the wrong session | The current session is stale | `/desktop_which`, then `/desktop_use <id>` or `/desktop_clear` |

### Where to look

- `~/.telechat/bot.log` — the bot's own log, including reply dispatch
- `~/.telechat/service.out` / `service.err` — launchd's capture of the service
- `~/.claude/settings.json` — the hook entries, exactly as installed
- `~/.claude/projects/<slugged-cwd>/<session-id>.jsonl` — the transcripts cards
  are built from

---

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_APPROVAL_TIMEOUT` | `300` | Seconds an approval card waits for a tap |
| `BRIDGE_APPROVAL_TIMEOUT_ACTION` | `fallthrough` | What an untapped card does: `fallthrough`, `deny`, or `allow` |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | Required for replies and digests |
| `BRIDGE_STREAM` | `1` | Live progress card while a reply runs; `0` waits silently |
| `BRIDGE_STREAM_EDIT_SECS` | `3` | Minimum seconds between edits to that card (floor `1`) |

Start/exit pings are on by default and toggled from the chat with `/lifecycle`,
not from `.env` — they are a preference, not deployment config, so they live in
the database alongside your session selection.

`fallthrough` hands the decision back to Claude Code's normal permission flow,
which usually means prompting at the desktop. That is the right default for
someone sitting at the machine — but if you armed approval precisely *because*
you are away from it, set `deny`. The card says which one it will do, so the
policy is visible at the moment you would act on it. An unrecognised value means
`fallthrough`: a typo cannot silently become a security posture.

Full list in [configuration.md](configuration.md).

---

## Limits

- `claude --resume` works best when the target session is **idle**. Replying
  mid-turn is undefined behaviour.
- The background service is macOS launchd. On Linux, run Telechat under
  `systemd --user`; the hooks themselves are platform-independent.
- Approval is **off per project** by default. Installing the hook does not arm
  it — `/desktop_approve_on` does, as a reply to a card from that project.
- Anyone on your Telegram allowlist can drive your Claude Code sessions.
  Read [SECURITY.md](../SECURITY.md) before adding a second person.

---

## Uninstall

```bash
telechat bridge uninstall           # remove the hooks
telechat bridge service uninstall   # also stop and remove the background service
```

`uninstall` leaves the service running on purpose — it also powers ordinary
chat, so removing it because you turned off the bridge would be a surprise.
