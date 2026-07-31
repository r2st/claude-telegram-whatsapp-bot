#!/usr/bin/env bash
# Create the development virtualenv and install telechat with every extra the
# test suite needs.
#
#   ./scripts/dev-setup.sh          # use the newest supported python found
#   ./scripts/dev-setup.sh python3.12
#
# Why this exists: the default `python3` on a current macOS/Homebrew box is
# 3.14 with no pytest, so "run pytest -q before you finish" (AGENTS.md rule 8)
# was a step every new contributor and agent had to reinvent.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV_DIR="${VENV_DIR:-venv}"

# The floor is 3.10 (pyproject `requires-python`); doctor.py enforces the same.
SUPPORTED=(python3.13 python3.12 python3.11 python3.10)

pick_python() {
    if [ $# -gt 0 ]; then
        command -v "$1" >/dev/null 2>&1 || { echo "error: $1 not found on PATH" >&2; exit 1; }
        echo "$1"
        return
    fi
    for candidate in "${SUPPORTED[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            echo "$candidate"
            return
        fi
    done
    # Fall back to whatever `python3` is, but only if it clears the floor.
    if command -v python3 >/dev/null 2>&1 &&
       python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo python3
        return
    fi
    echo "error: no Python 3.10+ interpreter found (tried: ${SUPPORTED[*]}, python3)" >&2
    echo "       install one, or pass an explicit interpreter: $0 python3.12" >&2
    exit 1
}

PY="$(pick_python "$@")"
echo "→ interpreter: $PY ($("$PY" --version 2>&1))"

if [ ! -d "$VENV_DIR" ]; then
    echo "→ creating $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
else
    echo "→ reusing existing $VENV_DIR"
fi

# shellcheck disable=SC1090
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip

# `all` matters as much as `dev`: without the optional feature packages a large
# part of the suite silently skips rather than fails, which reads as green.
echo "→ installing telechat with [dev,all]"
"$VENV_DIR/bin/python" -m pip install --quiet -e ".[dev,all]"

echo
echo "Done. Activate it with:"
echo "    source $VENV_DIR/bin/activate"
echo
echo "Then:"
echo "    pytest -q                        # the full suite (~70s)"
echo "    pytest -q --cov=telechat_pkg      # with coverage (CI floor: 85%)"
echo "    ruff check .                      # lint, same rules as CI"
