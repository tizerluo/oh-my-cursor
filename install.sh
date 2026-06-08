#!/usr/bin/env bash
set -euo pipefail
OMC_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$OMC_ROOT/scripts/install.py" "$@"
