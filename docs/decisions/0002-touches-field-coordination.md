# 0002. `touches:` field + pre-claim overlap check

Date: 2026-06-03
Status: Accepted

## Context

ADR 0001 established the file-based ticketing system but explicitly noted
under Negative consequences: *"Convention only — no tooling stops two
agents from claiming the same ticket simultaneously. Mitigated by the
'one ticket per agent' rule and a quick `git pull` before claiming."*

That mitigation addresses **same-ticket** collisions (two agents grab the
same NNNN). It does not address the more likely collision in practice:
two agents claim *different* tickets that both modify the same source
file. Examples concrete to the telechat tree:

- Two tickets that touch `telechat_pkg/web_chat.py` (multiple CODE_REVIEW
  P0 security findings live there).
- A ticket adding a Telegram command + a ticket testing telegram_bot.py.
- Any two tickets that need to edit `pyproject.toml` (deps, extras,
  pytest config).

In each case both agents would silently branch off the same file state
and the second push would either fail or, worse, overwrite work.

## Decision

Add a structured `touches:` field to ticket frontmatter listing every
file the ticket will **modify** (create, edit, or delete). Reads don't
count. Plus a small shell script `agents/check-overlap.sh` that an agent
runs before claiming to grep `agents/tasks/*.md` for path overlap with
its candidate ticket.

Frontmatter shape:

```yaml
touches:
  - telechat_pkg/main.py
  - tests/test_main.py
  - pyproject.toml
```

Claim flow becomes:

```bash
# 1. Pull latest so tasks/ is current
git pull

# 2. Run the overlap check on the ticket you want to claim
agents/check-overlap.sh 0017

# 3. If clean: proceed with the existing claim flow from agents/README.md
#    If overlap: coordinate, wait, or pick a different ticket
```

When the check reports a conflict, the agent has three responses:

1. **Wait**: hold off until the holding ticket moves to `agents/done/`.
2. **Coordinate**: post a note in the holding ticket's body and the
   candidate's body, then proceed if both agents agree on serialization.
3. **Pick another ticket**: usually the right answer for early-stage
   tickets where the work is fungible.

The check is **advisory**. It does not (yet) gate via a git hook. Same
philosophy as ADR 0001: social enforcement, easy to override when needed,
auditable via `git log`. The cost of skipping it the rare time it's
wrong is bounded by git's normal merge mechanics.

## Consequences

Positive:
- Cross-ticket file collisions surface at claim time (cheap to handle)
  instead of at push/merge time (expensive).
- `touches:` doubles as documentation: a future agent reading a `done/`
  ticket sees at-a-glance which files moved.
- Lightweight: one frontmatter field + one shell script. No git hook,
  no CI dep, no rewriting AGENTS.md.

Negative:
- Agents must declare `touches:` honestly. Forgotten files = silent
  collision. Mitigated by code review of completed PRs and by retrofit:
  if you finish a ticket and `touches:` was wrong, update the ticket
  before moving it to `done/`.
- Pre-existing tickets (0001-0004 in inbox at decision time) don't have
  `touches:`. The check script warns and exits non-zero so the claiming
  agent has to either add the field (recommended) or override the check.
- Globs not yet supported. A ticket that needs to touch many files
  (e.g., the `telegram_bot.py` split) lists them all or lists a sentinel
  like `telechat_pkg/telegram/**` understood by convention only. Add
  glob support to the script when first needed.

## Alternatives considered

- **Branch per ticket.** Git-native isolation: each claimed ticket gets
  its own branch, conflicts surface at merge. Heavier protocol change
  (agents need branch + merge discipline), shifts the work from
  "claim-time prevention" to "merge-time resolution". Rejected for now
  because the inbox is dominated by small disjoint test-writing tickets;
  the friction of branching exceeds the friction of overlap check. Can
  be layered on later if collisions become common.
- **OS-level flock or .lock files.** Doesn't survive across agents
  on different machines. Lockfiles need their own coordination, which
  is the original problem.
- **`depends_on:` for serialization.** Already in the frontmatter but
  used for *logical* dependencies (ticket B needs ticket A's outcome).
  Could be repurposed for file overlap but conflates two distinct
  concerns. Better to keep them separate.
- **Tooling to extract `touches:` from a planned diff.** Adds friction
  at ticket-creation time and false confidence (diff doesn't account
  for "in scope" but uncoded changes). Manual + honest is better at
  this scale.

## Related

- ADR 0001 — Ticketing system (this builds on it)
- AGENTS.md — Updated to reference the field and the script
- agents/_template.md — Frontmatter template now includes `touches:`
- agents/check-overlap.sh — The script itself
