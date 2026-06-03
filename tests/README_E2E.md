# End-to-End Tests (`pytest-recording` cassettes)

This directory contains **real** end-to-end tests against the Anthropic API —
not mocks. They use [`vcrpy`](https://vcrpy.readthedocs.io/) (via the
[`pytest-recording`](https://github.com/kiwicom/pytest-recording) plugin) to
record real HTTP traffic the first time, then replay the recorded responses
forever after. CI never burns API credits.

## Quick start

```bash
# 1. Install the test extras (vcrpy + pytest-recording)
pip install -e '.[dev]'

# 2. Replay against the committed cassettes (no API key needed)
pytest tests/test_anthropic_e2e_cassettes.py -v
```

## Recording new cassettes

You need a real key with budget for Claude Haiku (cheap — each test is ≤30
tokens out). Record once, commit the YAML, you're done.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_anthropic_e2e_cassettes.py \
       --record-mode=once -v

# Inspect the cassette before committing
ls tests/cassettes/test_anthropic_e2e_cassettes/
```

Record modes:

| Mode | What it does |
|---|---|
| `none` (default) | Replay only. Fail if a request isn't in the cassette. |
| `once` | If no cassette exists yet, record. Otherwise replay-only. **Use this for new tests.** |
| `new_episodes` | Replay matched requests, record new ones. Good for adding interactions to an existing test. |
| `all` | Re-record everything. Use sparingly; you'll burn credits. |

## Security: what gets scrubbed before commit

`test_anthropic_e2e_cassettes.py` configures `vcrpy` to scrub these headers
from every recorded request/response **before** the cassette hits disk:

- Request: `x-api-key`, `authorization`, `anthropic-organization-id`, all
  `x-stainless-*` SDK fingerprint headers, `user-agent`, `cookie`
- Response: `set-cookie`, `request-id`, `anthropic-organization-id`,
  `cf-ray`, `x-cloud-trace-context`, `via`, `server`

After recording, **manually grep the cassette for your key prefix** before
committing:

```bash
grep -r "sk-ant" tests/cassettes/ && echo "STOP — secret leaked" || echo "clean"
```

If you ever spot a real key in a committed cassette: rotate it on the
Anthropic console immediately and force-push a removal (or rewrite history
with `git filter-repo`).

## Re-recording when the wire format changes

Anthropic occasionally adds/renames JSON fields. If a real API call works
but the cassette test fails after an SDK upgrade:

```bash
rm tests/cassettes/test_anthropic_e2e_cassettes/<test_name>.yaml
ANTHROPIC_API_KEY=sk-ant-... \
    pytest tests/test_anthropic_e2e_cassettes.py::<test_name> \
           --record-mode=once
```

## Why not mock?

The existing suite has ~3,000 tests, most of which mock `anthropic.Anthropic`
or `_get_api_client`. That gives you high line coverage but **zero** assurance
the wire format is correct — an SDK bump that changes the JSON shape would
slip through.

Cassette tests close that gap: they exercise the real `anthropic` SDK, the
real `httpx` client, real serialization, real response parsing. The only
fake piece is the socket itself.

## What about Telegram / WhatsApp / Slack?

Same pattern applies — `python-telegram-bot` and `slack-bolt` both use
`httpx`/`aiohttp` under the hood, so `vcrpy` can record them too. Patterns
to follow:

- **Telegram**: record `getMe`, `sendMessage`, `getUpdates` against
  `api.telegram.org`. The bot token is scrubbed the same way as the
  Anthropic key.
- **WhatsApp (Green API)**: record against `api.green-api.com`.
- **Slack**: record against `slack.com/api/*`.

These aren't scaffolded yet — they're follow-up work. The Anthropic cassettes
are the highest-value first step because that's where actual reasoning bugs
manifest.

## Opt-in destructive E2E (already in the tree)

`tests/e2e-features.sh` exercises a real `claude` CLI round-trip plus the
service start/stop/restart lifecycle. It's **off by default**:

```bash
TELECHAT_E2E_LIFECYCLE=1 bash tests/e2e-features.sh
```

Run this manually before cutting a release.
