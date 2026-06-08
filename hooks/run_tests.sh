#!/bin/sh
set -e
cd "$(dirname "$0")/.."
python3 tests/test_review_gate.py
python3 tests/test_mmr_fixes.py
