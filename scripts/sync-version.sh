#!/usr/bin/env bash
#
# Derive every version number from pyproject.toml (item 4 of docs/improvements.md).
#
# pyproject.toml is the single source of truth. telechat_pkg/__init__.py reads it
# at import, so only npm/package.json needs writing.
#
# Usage:
#   bash scripts/sync-version.sh          # write npm/package.json to match
#   bash scripts/sync-version.sh --check  # exit 1 if they diverge (for CI)
#
# Three numbers used to ship in one release — pyproject 1.2.0, __init__ 1.1.5,
# npm 1.1.1 — so `telechat --version`, the MCP server banner, and the updater all
# reported something different, and the updater nagged npm users permanently
# because the wrapper's version trailed the package it launches.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-write}"

PY_VERSION="$(
    python3 - "$REPO_ROOT/pyproject.toml" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
if not m:
    sys.exit("could not find version in pyproject.toml")
print(m.group(1))
PYEOF
)"

NPM_VERSION="$(
    python3 - "$REPO_ROOT/npm/package.json" <<'PYEOF'
import json, sys
print(json.load(open(sys.argv[1]))["version"])
PYEOF
)"

if [ "$MODE" = "--check" ]; then
    if [ "$PY_VERSION" != "$NPM_VERSION" ]; then
        echo "✗ Version mismatch:"
        echo "    pyproject.toml    : $PY_VERSION"
        echo "    npm/package.json  : $NPM_VERSION"
        echo ""
        echo "  Fix with: bash scripts/sync-version.sh"
        exit 1
    fi
    echo "✓ Versions agree: $PY_VERSION"
    exit 0
fi

python3 - "$REPO_ROOT/npm/package.json" "$PY_VERSION" <<'PYEOF'
import re, sys
path, version = sys.argv[1], sys.argv[2]
with open(path) as f:
    raw = f.read()
# Rewrite in place rather than json.dump, to preserve key order and formatting.
new = re.sub(r'("version":\s*)"[^"]+"', lambda m: m.group(1) + f'"{version}"', raw, count=1)
with open(path, "w") as f:
    f.write(new)
PYEOF

echo "✓ npm/package.json set to $PY_VERSION (from pyproject.toml)"
