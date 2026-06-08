#!/bin/sh
# Fail CI if home-specific or legacy install paths leak into deliverables.
set -eu
cd "$(dirname "$0")/.."

SCAN_DIRS="hooks skills docs tests .github"
SCAN_FILES="README.md install.sh CHANGELOG.md"

FAIL=0

# Text deliverables only; skip __pycache__, binaries, and this script's pattern literals.
scan() {
  pattern="$1"
  desc="$2"
  hits=""
  for d in $SCAN_DIRS; do
    [ -d "$d" ] || continue
    hits="$hits$(find "$d" -type f \
      ! -path '*/__pycache__/*' \
      ! -name '*.pyc' \
      -exec grep -lE "$pattern" {} + 2>/dev/null || true)"
  done
  for f in $SCAN_FILES; do
    [ -f "$f" ] || continue
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
scan '~/.cursor/hooks/review_gate' 'legacy ~/.cursor/hooks/review_gate in deliverables'

if [ "$FAIL" -ne 0 ]; then
  echo "Stale path check failed. Use \$OMC_ROOT or install-rendered paths."
  exit 1
fi

echo "Stale path check passed."
