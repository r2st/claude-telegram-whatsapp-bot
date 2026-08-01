# Contributing to telechat

Bug reports, fixes, and features are all welcome. This file is the short
version for humans; `AGENTS.md` is the longer version that AI agents working
this tree follow, and the two describe the same workflow.

## Getting set up

```sh
git clone https://github.com/telechatai/telechat.git
cd telechat
./scripts/dev-setup.sh          # or: ./scripts/dev-setup.sh python3.12
source venv/bin/activate
```

The script picks the newest Python 3.10+ it can find and installs the package
with `[dev,all]`. The `all` matters: without the optional feature packages a
large part of the test suite skips silently rather than failing, which reads as
green.

## Before you open a pull request

```sh
pytest -q          # 4,000+ tests, a minute or two
ruff check .       # same rules CI runs
```

Both are enforced in CI, along with a coverage floor, a version-consistency
check, a Docker build, and a check that `docs/configuration.md` still matches
the code.

If you added or removed an environment variable:

```sh
python scripts/env_reference.py
```

That regenerates `docs/configuration.md`. It will refuse until you have added a
one-line description for the new variable in `scripts/env_reference.py` — that
is deliberate, since a default can be read off the source but a purpose cannot.

## What good looks like here

**Tests come with the change, not after it.** Every behaviour this project
fixed has a test named after the thing that used to go wrong, and that naming
is the point: `test_spares_processes_that_only_mention_telechat` tells the next
reader what the code is defending against. Prefer one test that would have
caught the bug over five that exercise the happy path.

**Comments explain why, not what.** If a line looks odd, the comment should say
what happens without it. Assume the reader can read Python.

**No breaking changes without a strong reason.** People run this on their own
machines and update it in place. A renamed setting should keep reading the old
name as a fallback, the way `CLAUDE_SYSTEM_PROMPT` still reads `SYSTEM_PROMPT`.

**Conventional commits.** `feat:`, `fix:`, `chore:`, `test:`, `docs:`,
`refactor:`, `ci:`, with an optional scope: `fix(store): …`. Write the subject
as what changes for the user.

**User-visible changes get a `CHANGELOG.md` entry** under `## [Unreleased]`,
written for the person running the bot.

## How this project is actually built

Most of the work here is done by AI agents coordinating through a file-based
ticket system, and the history is worth reading if you want to understand a
decision:

| Directory | What is in it |
|---|---|
| `agents/inbox/` | Unclaimed tickets — this is what to work on next |
| `agents/tasks/` | Claimed, in progress |
| `agents/done/` | Shipped, each with an outcome summary and test evidence |
| `docs/decisions/` | ADRs for anything someone might later second-guess |
| `docs/improvements.md` | The current review: what is broken, what is worth doing, in priority order |

Human contributors do not have to use the ticket system for a small fix — open
a pull request. For anything larger, a ticket in `agents/inbox/` (copy
`agents/_template.md`) is how you avoid two people landing in the same file;
`agents/check-overlap.sh <NNNN>` tells you whether anyone already claimed the
paths you are about to touch.

## Reporting bugs

Include what you ran, what happened, and `telechat --version`. If the bot is
running, `telechat logs` usually has the traceback. Please redact tokens —
`telechat env` masks them, plain log files do not always.

Security issues go to `SECURITY.md` instead, not the public issue tracker.

## Scope

telechat is a **personal, single-operator** tool: one person's Claude
credentials, one working directory, one permission mode. Features that assume a
shared multi-tenant deployment are usually out of scope unless they come with
the isolation to match. If you are unsure, open an issue before building.
