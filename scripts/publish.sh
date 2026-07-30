#!/usr/bin/env bash
#
# Publish to PyPI and npm — only after e2e tests pass.
#
# Usage:
#   bash scripts/publish.sh          # publish both
#   bash scripts/publish.sh pypi     # PyPI only
#   bash scripts/publish.sh npm      # npm only
#

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-both}"

echo "🔢 Syncing version numbers from pyproject.toml..."
bash "$REPO_ROOT/scripts/sync-version.sh"
echo ""

echo "🧪 Running e2e tests before publish..."
echo ""
bash "$REPO_ROOT/tests/run-all.sh"
echo ""
echo "✅ E2e tests passed."
echo ""

if [ "$TARGET" = "pypi" ] || [ "$TARGET" = "both" ]; then
    echo "📦 Publishing to PyPI..."
    cd "$REPO_ROOT"
    rm -rf dist/
    python3 -m build
    python3 -m twine upload dist/*
    echo "✅ PyPI publish complete."
    echo ""
fi

if [ "$TARGET" = "npm" ] || [ "$TARGET" = "both" ]; then
    echo "📦 Publishing to npm..."
    cd "$REPO_ROOT/npm"
    npm publish
    echo "✅ npm publish complete."
    echo ""
fi

echo "🎉 Done."
