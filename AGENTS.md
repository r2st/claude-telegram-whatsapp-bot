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
| Code review notes                  | `CODE_REVIEW.md`                         |
| App code                           | `telechat_pkg/`                          |
| Tests                              | `tests/` (3000+ tests, ~99% coverage)    |
| Scripts                            | `scripts/`                               |
| Why a decision was made            | `docs/decisions/` (ADRs — create as needed) |

## Coordination rules (read this)

1. **Claim before you build.** Move the ticket file from `agents/inbox/`
   to `agents/tasks/` and set `owner:` + `started:` in its frontmatter.
   Don't start work on something nobody owns.
2. **One ticket per agent at a time.** If you find yourself "while I'm
   here…" write a new ticket file in `agents/inbox/` instead.
3. **Update the ticket as you go.** It's the single source of truth for
   what's done and what's blocked. Other agents read it.
4. **When done, move it to `agents/done/`** with a 2–3 line outcome
   summary at the bottom (what shipped, where the code lives, test
   evidence).
5. **Big decisions get an ADR.** Anything another agent might
   second-guess (schema change, dependency add, architectural shift) →
   `docs/decisions/NNNN-short-title.md`.
6. **Don't break the test suite.** ~99% coverage today. Run `pytest -q`
   before moving a ticket to `done/`. If a test must change, say why in
   the outcome summary.
7. **Keep `docs/implementation-tracker.md` in sync.** When you finish a
   ticket that maps to a feature row, flip its status there too.

## Ticket lifecycle

```
agents/inbox/   ← drop new tickets here; unclaimed
       │
       │  agent claims: set owner + started, git mv
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
