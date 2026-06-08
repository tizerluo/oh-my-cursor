---
name: map-hyperplan
description: >-
  对抗性规划 workflow。用于 spec 审查、方案设计、架构辩论。
  触发词：plan, spec, design, 架构, 方案, 评审方案, debate, hyperplan.
  Requires MAP .review/ infrastructure. Does not trigger merge gate.
---

# MAP Hyperplan — Adversarial Planning

Commander-driven spec quality workflow. Output: `.specs/{name}.md` + `.review/reports/`.

## Workflow ID

`map-hyperplan`

## Subagent mapping (platform types)

| Role | Task subagent_type | Focus |
|------|-------------------|--------|
| critic-architecture | `architect` | feasibility, interfaces |
| critic-security | `generalPurpose` | attack surface, auth |
| critic-cost | `generalPurpose` | cost, maintenance |

Default: 3 critics. Extend via `config.max_critics` (max 5).

## Pipeline

```
config-confirmed → draft → critics → debate → revise → accepted
```

1. **Draft** — Commander or Planner agent (V2.0 optional) writes spec to `.specs/`
   - Frontmatter REQUIRED: `status: draft` (see `spec-frontmatter.schema.json`)
   - Lifecycle: `draft` → `in-review` → `accepted` | `superseded`
2. **Critics** — parallel Task calls, one per critic dimension
3. **Debate** — Commander synthesizes to `.review/reports/debate-round-{N}.json` (validate before write):

```json
{
  "session_id": "...",
  "round": 1,
  "claims": [{"id": "c1", "text": "...", "author": "critic-security"}],
  "counterclaims": [],
  "evidence": [{"claim_id": "c1", "ref": "file:line"}],
  "unresolved": [{"severity": "P1", "desc": "..."}],
  "consensus_items": ["Both agree on rate limit"]
}
```

Schema: `~/.cursor/hooks/schemas/debate-round.schema.json`
4. **Revise** — update spec; increment round until `max_rounds` or consensus
5. **Accepted** — set spec frontmatter `status: accepted`

## Loop control

Unresolved P0/P1 from critics → write `.review/critic-queue.json`:

```json
{
  "schema_version": 1,
  "session_id": "...",
  "round": 1,
  "pending_items": [{"dimension": "security", "severity": "P1", "desc": "..."}]
}
```

Commander drives revise → critics loop manually.

After resolving critic items:

```bash
python3 ~/.cursor/hooks/review_gate.py advance-critic-queue /path/to/repo security,P1-item --increment-round
```

Stop hook at `phase=revise` emits critic-queue followup when pending items exist.

## Exit conditions

- critic-queue empty + all critics signed off → `phase: accepted`
- max_rounds exhausted → best spec + unresolved list in report
- **No merge gate** — merge/push is **permanently blocked** for map-hyperplan (hook-enforced)
- **Forbidden:** coder subagent marker (gate fail-closed if detected)
- Allowed writes: `.specs/`, `.review/` only

## Critic completion tracking

**Path A (primary, Cursor 3.7.19+ verified):** subagentStop markers for `architect` / `generalPurpose`.

**Path B (fallback):** Commander writes `progress.completed.critics[]` — **convenience-only**, not merge gate evidence.

## Cost defaults

`max_critics: 3`, `max_rounds: 2` (~6-9 subagent calls).

## Planner (V2.0)

Optional `planner` agent for Draft step — NOT a hard dependency.
