#!/usr/bin/env bash
# agents/check-overlap.sh — report file overlap between a candidate ticket
# and the active in-progress claims in agents/tasks/.
#
# Usage:
#   agents/check-overlap.sh <NNNN>
#     e.g.  agents/check-overlap.sh 0017
#
# Exit codes:
#   0 — no overlap detected; safe to claim
#   1 — overlap reported, or candidate has no `touches:` field
#   2 — usage error / ticket not found
#
# Limitations: exact path match only (no globs). See ADR 0002.
set -euo pipefail

TID="${1:-}"
if [[ -z "$TID" ]]; then
  echo "usage: $0 <NNNN>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CAND=$(find "$ROOT/agents/inbox" "$ROOT/agents/tasks" -maxdepth 1 -name "${TID}-*.md" -print -quit 2>/dev/null || true)
if [[ -z "$CAND" ]]; then
  echo "no ticket matching '${TID}' found in agents/inbox or agents/tasks" >&2
  exit 2
fi

# Extract `touches:` list from a ticket's YAML frontmatter.
# Treats `touches: []` (or empty) as no declarations.
# Scoped strictly to the frontmatter block (between the first two `---`
# lines) so that body markdown like `## Acceptance criteria` checkbox
# bullets aren't mistaken for touches entries.
extract_touches() {
  awk '
    /^---$/ && fm { exit }                              # second --- = end of frontmatter
    /^---$/       { fm = 1; next }                      # first --- = start
    !fm           { next }
    /^touches:[[:space:]]*\[\][[:space:]]*$/ { exit }
    /^touches:[[:space:]]*$/                 { in_=1; next }
    /^[a-z_]+:/ && in_                       { in_=0 }
    in_ && /^[[:space:]]*-[[:space:]]+/      { sub(/^[[:space:]]*-[[:space:]]+/, ""); print }
  ' "$1"
}

CAND_TOUCHES=()
while IFS= read -r line; do
  CAND_TOUCHES+=("$line")
done < <(extract_touches "$CAND")
if [[ ${#CAND_TOUCHES[@]} -eq 0 ]]; then
  echo "WARN: $CAND has no \`touches:\` declared. Cannot check overlap." >&2
  echo "      Add a touches: list to the frontmatter, then re-run." >&2
  exit 1
fi

CONFLICTS=0
shopt -s nullglob
for ACTIVE in "$ROOT"/agents/tasks/*.md; do
  [[ -f "$ACTIVE" && "$ACTIVE" != "$CAND" ]] || continue
  A_TOUCHES=()
  while IFS= read -r line; do
    A_TOUCHES+=("$line")
  done < <(extract_touches "$ACTIVE")
  [[ ${#A_TOUCHES[@]} -eq 0 ]] && continue
  for c in "${CAND_TOUCHES[@]}"; do
    for a in "${A_TOUCHES[@]}"; do
      if [[ "$c" == "$a" ]]; then
        echo "CONFLICT  path='$c'"
        echo "          held by: $(basename "$ACTIVE")"
        echo "          wanted by: $(basename "$CAND")"
        echo
        CONFLICTS=$((CONFLICTS + 1))
      fi
    done
  done
done

if [[ $CONFLICTS -gt 0 ]]; then
  echo "$CONFLICTS conflicting path(s). Resolve before claiming:" >&2
  echo "  1. Wait for the holding ticket to move to agents/done/" >&2
  echo "  2. Coordinate in both ticket bodies and proceed with care" >&2
  echo "  3. Pick a different ticket from agents/inbox/" >&2
  exit 1
fi

echo "no overlap with active claims. Safe to claim ${TID}."
exit 0
