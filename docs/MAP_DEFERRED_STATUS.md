# MAP Deferred Backlog — Honest Status

Last updated: 2026-06-08 (DEF backlog implementation pass)

| ID | Plan item | Status | Notes |
|----|-----------|--------|-------|
| DEF-01 | Layer 3 routing heuristics | **fixed** | `load_routing_rules`, `match_routing_rules`, thresholds in `_routing_hints` |
| DEF-02 | preToolUse Task alignment | **fixed** | MMR Issue 1 |
| DEF-03 | Flaky regression workflow | **fixed** | `validate_regression_result`, quarantine schema, [map-refactor/SKILL.md](../skills/map-refactor/SKILL.md) |
| DEF-04 | GH Issue templates (security) | **fixed** | [templates/](~/.cursor/skills/map-security/templates/), `security_fingerprint()` |
| DEF-05 | Spec lifecycle | **fixed** | `parse_spec_frontmatter`, `_draft_specs`, schema, SKILL frontmatter |
| DEF-06 | Critic fallback hook | **fixed** | Path A primary; `advance_critic_queue`, hyperplan stop phase `revise` |
| DEF-07 | security-queue driver | **fixed** | schema, `append_unverified_findings_to_security_queue`, enhanced stop followup |
| DEF-08 | Skill discovery | **fixed** | [MAP_SKILL_DISCOVERY.md](../skills/MAP_SKILL_DISCOVERY.md) matrix + decisions |
| DEF-09 | State migration | **deferred** | Separate program (user decision) |
| DEF-10 | Knowledge hardening | **fixed** | `validate_knowledge_artifacts` soft warnings, schema, SKILL |
| DEF-11 | routing-rules.json | **fixed** | `load_routing_rules()` + ≥4 routing unit tests |
| DEF-12 | Debate spec schema | **fixed** | `validate_debate_report`, schema, hyperplan JSON template |

## Phase 0 infrastructure

| Item | Status |
|------|--------|
| Live Spike (A–F) | **done** — Cursor 3.7.19, [spikes/README.md](spikes/README.md) |
| `advance_fix_queue()` | **done** — CLI `advance-fix-queue` |
| `advance_critic_queue()` | **done** — CLI `advance-critic-queue` |
| Unit tests | **done** — 34 tests via [spikes/test_review_gate.py](spikes/test_review_gate.py) + [run_tests.sh](run_tests.sh) |

## CLI helpers

```bash
python3 ~/.cursor/hooks/review_gate.py advance-fix-queue /path/to/repo [id1,id2] --increment-round
python3 ~/.cursor/hooks/review_gate.py advance-critic-queue /path/to/repo [id1,id2] --increment-round
~/.cursor/hooks/run_tests.sh
```

## DEF-09 follow-up (deferred)

Dual-path merge gate state under `.review/session/` — do not start until explicit migration spec.
