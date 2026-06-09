#!/bin/sh
# Fail CI if home-specific or legacy install paths leak into deliverables.
set -eu
cd "$(dirname "$0")/.."

SCAN_DIRS="hooks skills docs tests .github .specs"
SCAN_FILES="README.md install.sh CHANGELOG.md SECURITY.md CONTRIBUTING.md"
SELF="scripts/ci_check_stale_paths.sh"

FAIL=0

# Text deliverables only; skip __pycache__, binaries, spikes, and this script.
scan() {
  pattern="$1"
  desc="$2"
  hits=""
  for d in $SCAN_DIRS; do
    [ -d "$d" ] || continue
    hits="$hits$(find "$d" -type f \
      ! -path '*/__pycache__/*' \
      ! -path '*/spikes/*' \
      ! -name '*.pyc' \
      ! -name 'ci_check_stale_paths.sh' \
      -exec grep -lE "$pattern" {} + 2>/dev/null || true)"
  done
  for f in $SCAN_FILES; do
    [ -f "$f" ] || continue
    [ "$f" = "$SELF" ] && continue
    hits="$hits$(grep -lE "$pattern" "$f" 2>/dev/null || true)"
  done
  if [ -n "$hits" ]; then
    echo "$hits" | tr ' ' '\n' | sed '/^$/d' | sort -u
    echo "FAIL: $desc"
    FAIL=1
  fi
}

scan 'tizer_mac_studio' 'machine-specific path (tizer_mac_studio)'
scan '/Users/[^ ]+/\.cursor/hooks' 'hardcoded /Users/.../.cursor/hooks path'
scan '/Users/[^ ]+/\.cursor/plans' 'hardcoded /Users/.../.cursor/plans path'
scan '~/.cursor/hooks/review_gate' 'legacy ~/.cursor/hooks/review_gate in deliverables'
scan 'Path\.home\(\)[[:space:]]*/[[:space:]]*["'\'']\.cursor["'\''][[:space:]]*/[[:space:]]*["'\'']skills' 'Path.home() skills fallback in deliverables'

if [ "$FAIL" -ne 0 ]; then
  echo "Stale path check failed. Use \$OMC_ROOT or install-rendered paths."
  exit 1
fi

echo "Stale path check passed."
