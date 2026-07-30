# Project guide for agents

Telechat is a Telegram + WhatsApp Claude bot with a self-improving
architecture (feedback loop, watchdog, smart routing, memory, scheduler,
KB/RAG, browser, MCP). Multiple AI agents work on this tree in parallel.
Read this file before doing anything. It points to the canonical source
for every kind of context so you don't have to grep.

## Where things live

| You need…                          | Look in                                  |
|------------------------------------|------------------------------------------|
| **What to work on next**           | `agents/inbox/` (unclaimed tickets)      |
| Work in progress                   | `agents/tasks/` (claimed tickets)        |
| Completed work                     | `agents/done/`                           |
| Ticket template                    | `agents/_template.md`                    |
| Role definitions                   | `agents/roles.md`                        |
| Architecture overview              | `docs/architecture.md`                   |
| Self-improving system design       | `docs/self-improving-design.md`          |
| Feature implementation status      | `docs/implementation-tracker.md`         |
| Telegram-specific features         | `docs/advanced-telegram-features.md`     |
| Review findings / what to fix next | `docs/improvements.md`                   |
| Older review (CLOSED, historical)  | `docs/CODE_REVIEW.md` — superseded; don't work from it |
| App code                           | `telechat_pkg/`                          |
| Tests                              | `tests/` (3000+ tests; coverage floor enforced in CI) |
| Scripts                            | `scripts/`                               |
| Why a decision was made            | `docs/decisions/` (ADRs — create as needed) |

## Coordination rules (read this)

1. **Declare what you'll touch.** Every ticket frontmatter has a
   `touches:` list naming the files the ticket will modify. Reads don't
   count; new file creation does. See `agents/_template.md` and ADR 0002.
2. **Check for overlap before you claim.** Run `agents/check-overlap.sh
   <NNNN>` to scan `agents/tasks/*.md` for any active claim on the same
   paths. If clean, proceed. If conflicting: wait, coordinate via the
   ticket bodies, or pick a different ticket.
3. **Claim before you build.** `git pull` first, then move the ticket
   file from `agents/inbox/` to `agents/tasks/` and set `owner:` +
   `started:` + `status: in-progress` in its frontmatter. Don't start
   work on something nobody owns.
4. **One ticket per agent at a time.** If you find yourself "while I'm
   here…" write a new ticket file in `agents/inbox/` instead.
5. **Update the ticket as you go.** It's the single source of truth for
   what's done and what's blocked. Other agents read it.
6. **When done, move it to `agents/done/`** with a 2–3 line outcome
   summary at the bottom (what shipped, where the code lives, test
   evidence).
7. **Big decisions get an ADR.** Anything another agent might
   second-guess (schema change, dependency add, architectural shift) →
   `docs/decisions/NNNN-short-title.md`.
8. **Don't break the test suite.** Run `pytest -q` before moving a ticket
   to `done/`. If a test must change, say why in the outcome summary.
   Coverage is enforced by a floor in `.github/workflows/pytest.yml` rather
   than quoted here, so the number can't drift out of date again — read it
   there, or run `pytest --cov=telechat_pkg` for the current per-module
   figures. It is not uniform: the older modules are near-total, and
   `desktop_bridge.py` is the thinnest.
9. **Keep `docs/implementation-tracker.md` in sync.** When you finish a
   ticket that maps to a feature row, flip its status there too.

## Ticket lifecycle

```
agents/inbox/   ← drop new tickets here; unclaimed
       │
       │  agent: git pull && agents/check-overlap.sh <NNNN>
       │         set owner + started + status, git mv
       ▼
agents/tasks/   ← in progress; only one ticket per agent
       │
       │  agent finishes: append outcome summary, git mv
       ▼
agents/done/    ← shipped; immutable history
```

## Tone for human-facing artifacts

- Tickets and ADRs: terse. Bullet points beat paragraphs.
- Code comments: explain why, not what.
- No emojis unless the human asks for them.
