#!/bin/sh
set -e
cd "$(dirname "$0")/.."
python3 tests/test_review_gate.py
python3 tests/test_mmr_fixes.py
python3 tests/test_merge_gate_e2e.py
python3 tests/test_workspace_cache.py
python3 tests/test_install.py
python3 tests/test_security.py
python3 tests/test_migrate_map_state.py
python3 tests/test_def09.py
python3 tests/test_p2_hooks.py
python3 tests/test_p3_coverage.py
python3 tests/test_phase_transitions.py
python3 tests/test_queues_unified.py
