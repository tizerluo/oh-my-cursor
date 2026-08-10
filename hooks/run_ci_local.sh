#!/bin/sh
set -e
cd "$(dirname "$0")/.."

python3 -m pip install -r requirements-dev.txt
ruff check hooks/ tests/ scripts/ --select E,F,W --ignore E501
# 按 #22，mypy 检查范围仅限 review_gate.py。
mypy hooks/review_gate.py --ignore-missing-imports --no-error-summary
bash hooks/run_tests.sh
