<!--
Conventional commit subject, please: feat: / fix: / chore: / test: / docs: /
refactor: / ci:, with an optional scope — fix(store): …
-->

## What changes for the user

<!-- One or two sentences. If nothing changes for the user, say so. -->

## Why

<!-- What was wrong, or what this makes possible. Link an issue or a ticket in
     agents/ if there is one. -->

## How it was verified

<!-- Which tests you added and what they would have caught. "Tests pass" is not
     verification; "test_x fails on main and passes here" is. -->

## Checklist

- [ ] `pytest -q` passes
- [ ] `ruff check .` passes
- [ ] Tests added for the behaviour this changes — named after what used to go wrong
- [ ] No breaking change (a renamed setting still reads its old name)
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if this is user-visible
- [ ] `python scripts/env_reference.py` re-run if an environment variable changed
