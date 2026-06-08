# MAP Skill Discovery — Verification Matrix

Last updated: 2026-06-08

## Purpose

Document Layer 1 skill discovery (Cursor recommending map-* skills) and fallback to sessionStart Layer 3 hints.

## Test matrix

| # | User prompt (sample) | Expected workflow | Skill expected | Cursor suggested skill? | Notes |
|---|---------------------|-------------------|----------------|-------------------------|-------|
| 1 | 写个 spec 评审架构方案 | map-hyperplan | map-hyperplan | manual | |
| 2 | security audit XSS injection | map-security | map-security | manual | |
| 3 | 重构 auth 模块拆分 | map-refactor | map-refactor | manual | |
| 4 | fix bug implement feature | multi-agent-pr | multi-agent-pr | manual | |
| 5 | plan debate hyperplan | map-hyperplan | map-hyperplan | manual | |
| 6 | 漏洞扫描 渗透 | map-security | map-security | manual | |
| 7 | migrate module extract | map-refactor | map-refactor | manual | |
| 8 | implement PR with reviewers | multi-agent-pr | multi-agent-pr | manual | |

Fill **manual** after testing in Cursor Agent (Settings → Skills). sessionStart routing hints provide fallback when skill not auto-invoked.

## disable-model-invocation decision

| Skill | Auto-invoke | Rationale |
|-------|-------------|-----------|
| map-hyperplan | allow | Distinct trigger words; low collision |
| map-security | allow | Security keywords specific |
| map-refactor | allow | Refactor keywords specific |
| multi-agent-pr | allow | Default dev workflow |

Do not set `disable-model-invocation: true` unless skill misfires on unrelated prompts during matrix testing.

## Fallback chain

1. User prompt → Cursor skill match (Layer 1)
2. No match → sessionStart `load_routing_rules()` + heuristics (Layer 3)
3. Active MAP session → Step 0.5 AskQuestion (hard gate)

## References

- [docs/workflows/plan-then-hyperplan.md](../docs/workflows/plan-then-hyperplan.md) — **Plan → Hyperplan → merge back** (required reading)
- [routing-rules.example.json](../multi-agent-pr/routing-rules.example.json)
- [review_gate.py load_routing_rules](../hooks/review_gate.py)
