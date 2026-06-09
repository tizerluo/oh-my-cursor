#!/bin/sh
set -e
cd "$(dirname "$0")/.."
python3 tests/test_review_gate.py
python3 tests/test_mmr_fixes.py
python3 tests/test_merge_gate_e2e.py
python3 tests/test_install.py
python3 tests/test_security.py
python3 tests/test_migrate_map_state.py
python3 tests/test_def09.py
