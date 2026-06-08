#!/usr/bin/env bash
# Post-cutover smoke checks for oh-my-cursor install (copy + link).
set -euo pipefail
OMC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CURSOR="${HOME}/.cursor"
GATE="${CURSOR}/hooks/review_gate.py"

echo "== oh-my-cursor cutover smoke =="
echo "OMC_ROOT: $OMC_ROOT"

echo "-- copy install doctor --"
python3 "$OMC_ROOT/scripts/install.py" --doctor --security --target "$CURSOR"

echo "-- sessionStart (session-resume) --"
out=$(echo '{"workspace_roots":["'"$OMC_ROOT"'"]}' | python3 "$GATE" session-resume)
echo "$out" | python3 -m json.tool >/dev/null && echo "OK session-resume returned valid JSON"

echo "-- merge gate: non-merge allows --"
echo '{"tool_name":"Shell","tool_input":{"command":"git status"}}' | python3 "$GATE" check-merge | grep -q '"permission": "allow"' && echo "OK non-merge allowed"

echo "-- merge gate: merge blocked without verdict --"
deny=$(echo '{"tool_name":"Shell","tool_input":{"command":"gh pr merge 1 --merge"},"workspace_roots":["'"$OMC_ROOT"'"]}' | python3 "$GATE" check-merge)
echo "$deny" | grep -q '"permission": "deny"' && echo "OK merge blocked without verdict"

echo "-- link mode temp install --"
LINK_HOME=$(mktemp -d)
trap 'rm -rf "$LINK_HOME"' EXIT
python3 "$OMC_ROOT/scripts/install.py" --link --i-know-symlink-risk --target "$LINK_HOME/.cursor" >/dev/null
python3 "$OMC_ROOT/scripts/install.py" --doctor --security --target "$LINK_HOME/.cursor" >/dev/null
test -L "$LINK_HOME/.cursor/hooks/review_gate.py" && echo "OK link mode symlink"

echo "All cutover smoke checks passed."
