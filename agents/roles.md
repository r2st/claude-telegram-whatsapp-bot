# Agent roles

Anyone can pick up any ticket, but tickets are tagged with a `role:`
field so the right kind of agent gravitates to the right kind of work.

## builder

Writes production code: features, fixes, refactors. Owns tests for
what they ship. Default role.

Responsibilities:
- Implement the acceptance criteria.
- Add/extend tests so the change is covered.
- Run `pytest -q` before marking done.
- Update `docs/implementation-tracker.md` if the ticket maps to a
  tracked feature.

## reviewer

Audits other agents' work before it ships. Catches regressions,
security issues, and style drift. Does not normally write new features.

Responsibilities:
- Run the test suite against the diff.
- Flag missing tests, broken contracts, leaky secrets, etc.
- Open a new ticket in `inbox/` for any follow-up they spot.

## researcher

Investigates, prototypes, and writes design docs. Output is usually a
new doc under `docs/` or an ADR under `docs/decisions/`, not code.

Responsibilities:
- Time-box exploration; don't sprawl.
- Land findings as a doc or ADR with a clear recommendation.
- File a builder ticket if the research surfaces work to do.

## pitch

Owns user-facing artifacts: README sections, release notes, demo
scripts, marketing copy. Editorial, not engineering.

Responsibilities:
- Match the tone defined in `AGENTS.md`.
- Keep claims accurate — verify features actually exist before listing
  them.
