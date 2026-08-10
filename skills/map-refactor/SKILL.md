---
name: map-refactor
description: >-
  重构 workflow。用于模块拆分、迁移、rename、baseline+regression 验证。
  触发词：refactor, 重构, 拆分, 迁移, extract, rename module, map-refactor.
  Requires MAP .review/ infrastructure. Merge gate enabled.
---

# MAP Refactor — Baseline + Regression

## Workflow ID

`map-refactor`

## Pipeline

```
config-confirmed → analysis → baseline → implement → regression
  → review-pending → synthesis-complete → merge-ready → (merged | cleanup)
```

CI / late failure recovery (same as multi-agent-pr): `merge-ready → fix-round-N → regression → …` (not a direct jump to `review-pending`).

See [multi-agent-pr SKILL §8](../multi-agent-pr/SKILL.md) for Commander steps and fix-round advance.

## Regression + flaky handling

1. Run `baseline_test_cmd` (confirmed in config gate)
2. Store baseline in `.review/reports/baseline-{sha}.json`
3. After Coder: re-run same command → `.review/reports/regression-{sha}.json`
4. On failure: retry up to `regression_retries` (default 2, config override)
5. Known flaky tests: list in `.review/quarantine-tests.json`

```json
{
  "schema_version": 1,
  "tests": ["tests/integration/test_flaky_network.py::test_timeout"],
  "notes": { "tests/integration/test_flaky_network.py::test_timeout": "network flake; tracked issue #123" }
}
```

## CLI helpers

After a fix-review or regression retry cycle:

```bash
python3 "$OMC_ROOT/hooks/review_gate.py" advance-fix-queue /path/to/repo [resolved-id,...] --increment-round
```

## False positive process

1. Confirm failure is flaky (re-run passes)
2. Add test id to quarantine with note
3. Document in regression report; do not block merge if quarantined only

## Merge gate

Standard multi-agent-pr merge gate + fix-queue apply.
