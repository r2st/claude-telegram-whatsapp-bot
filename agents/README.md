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
inbox/tasks/done and add 1). Keep slugs short and kebab-cased.

## Claiming a ticket

```bash
git mv agents/inbox/NNNN-slug.md agents/tasks/
# Edit frontmatter:
#   owner: <your-agent-name>
#   started: <YYYY-MM-DD>
#   status: in-progress
```

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
