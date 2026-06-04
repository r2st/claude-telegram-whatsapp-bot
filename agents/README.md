# agents/

Coordination workspace for multiple AI agents working on telechat in
parallel. See top-level `AGENTS.md` for the full protocol.

## Quick map

- `inbox/` — unclaimed tickets. Grab one here.
- `tasks/` — in-progress tickets. Each has exactly one owner.
- `done/` — completed tickets with outcome summaries (immutable).
- `_template.md` — copy this to start a new ticket.
- `roles.md` — what each agent role is responsible for.

## Creating a ticket

```bash
cp agents/_template.md agents/inbox/NNNN-short-slug.md
```

NNNN = next 4-digit ID (look at the highest existing one across
inbox/tasks/done and add 1). Keep slugs short and kebab-cased. Fill in
the `touches:` list in frontmatter — every file the ticket will
modify, one per line. Reads don't count; new file creation does.

## Claiming a ticket

```bash
# 1. Sync first so tasks/ is current
git pull

# 2. Check for file overlap with other in-progress claims
agents/check-overlap.sh NNNN
#    exit 0: clean, proceed
#    exit 1: conflict reported OR ticket missing touches:
#    exit 2: ticket not found

# 3. If clean: move it and update frontmatter
git mv agents/inbox/NNNN-slug.md agents/tasks/
# Edit frontmatter:
#   owner: <your-agent-name>
#   started: <YYYY-MM-DD>
#   status: in-progress
```

On conflict, three responses:

1. **Wait** for the holding ticket to move to `agents/done/`.
2. **Coordinate** — post a note in both ticket bodies and proceed only
   if both owners agree on serialization.
3. **Pick another ticket** from `agents/inbox/`.

See ADR 0002 (`docs/decisions/0002-touches-field-coordination.md`) for
rationale, limitations (no globs yet, advisory not enforced), and how
to handle pre-existing tickets without a `touches:` field.

## Finishing a ticket

1. Check all acceptance criteria boxes.
2. Append a 2–3 line **Outcome** section: what shipped, files touched,
   test evidence (e.g. `pytest -q tests/test_foo.py` passing).
3. Move to done:
   ```bash
   git mv agents/tasks/NNNN-slug.md agents/done/
   ```
4. If the ticket corresponds to a row in
   `docs/implementation-tracker.md`, flip its status there too.
