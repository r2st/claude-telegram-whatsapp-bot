# 0001. Ticketing system for multi-agent coordination

Date: 2026-05-31
Status: Accepted

## Context

Multiple AI agents (Claude Code, Codex, OpenHands, human contributors)
work on telechat in parallel. Without explicit coordination they
collide: two agents pick up the same task, edits overlap, decisions
get re-litigated, and finished work is hard to find. The Authmatic
hackathon repo proved a file-based ticketing convention works well
for this exact pattern.

## Decision

Adopt a file-based ticketing system under `agents/`:

- `agents/inbox/NNNN-slug.md` — unclaimed tickets.
- `agents/tasks/NNNN-slug.md` — exactly one in-progress ticket per
  agent.
- `agents/done/NNNN-slug.md` — completed tickets with outcome
  summaries (immutable).
- `agents/_template.md` — frontmatter + sections template.
- `agents/roles.md` — agent role definitions.
- `AGENTS.md` at repo root — protocol every agent reads first.
- `docs/decisions/NNNN-slug.md` — ADRs for cross-cutting decisions.

Tickets carry YAML frontmatter: `id`, `title`, `role`, `priority`,
`owner`, `started`, `status`, `depends_on`. Lifecycle is `inbox →
tasks → done` enforced socially (no tooling yet).

## Consequences

Positive:
- Single source of truth for "what's being worked on".
- Git history records who claimed/finished what and when.
- New agents (or new humans) onboard by reading `AGENTS.md` and
  `agents/inbox/`.
- ADRs accumulate decision context where future agents look first.

Negative:
- Convention only — no tooling stops two agents from claiming the same
  ticket simultaneously. Mitigated by the "one ticket per agent" rule
  and a quick `git pull` before claiming.
- Adds friction for solo / one-shot fixes. Mitigated by allowing tiny
  fixes to skip the ticket flow if they're under ~20 lines and have a
  test.

## Alternatives considered

- **GitHub Issues only.** Works for humans, but agents that don't have
  `gh` configured (or run offline) can't see them. File-based works
  everywhere.
- **A SQLite ticket DB.** Overkill for this scale; loses git
  history's auditability.
- **No system, just chat.** What we had before. Already caused
  duplicate work.
