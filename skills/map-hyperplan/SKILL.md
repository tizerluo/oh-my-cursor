---
name: map-hyperplan
description: >-
  对抗性规划 workflow。用于 spec 审查、方案设计、架构辩论。
  触发词：plan, spec, design, 架构, 方案, 评审方案, debate, hyperplan.
  Requires MAP .review/ infrastructure. Does not trigger merge gate.
---

# MAP Hyperplan — Adversarial Planning

Commander-driven spec quality workflow. Output: `.specs/{name}.md` + `.review/reports/`.

## When to use (position in MAP lifecycle)

**Hyperplan runs after Plan mode — not instead of it.**

```
Plan mode → original plan (.cursor/plans/*.plan.md)
         → map-hyperplan (adversarial review)
         → merge accepted spec back into original plan  ← mandatory Commander step
         → implement per merged plan (multi-agent-pr / coder)
```

See [docs/workflows/plan-then-hyperplan.md](../../docs/workflows/plan-then-hyperplan.md) for full workflow, document hierarchy, and anti-patterns.

| Phase | Mode | Primary artifact |
|-------|------|------------------|
| 1. Draft plan | Cursor **Plan mode** | `.cursor/plans/{name}.plan.md` |
| 2. Adversarial review | **map-hyperplan** | `.specs/{name}.md`, `.review/reports/` |
| 3. Merge back | Commander | **Update original plan** (single source of truth) |
| 4. Build | extract plan phases | code, install, CI |

**Do not** stop at `status: accepted` on the spec alone — fuse critic revisions (P0 contracts, estimates, DoD) into the plan before implementation.

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
config-confirmed → draft → critics → debate → revise → accepted → merge-back-to-plan
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

Schema: `hooks/schemas/debate-round.schema.json` (under `$OMC_ROOT`)
4. **Revise** — update spec; increment round until `max_rounds` or consensus
5. **Accepted** — set spec frontmatter `status: accepted`
6. **Merge back** — fuse accepted spec + debate conclusions into the **original Plan-mode document** (`.cursor/plans/*.plan.md`); update todos, estimates, DoD. Add `implementation_plan` in spec frontmatter pointing to the plan. `.specs/` remains audit archive only.
   - **Timing:** after hyperplan session ends (hook allows only `.specs/` + `.review/` during active hyperplan; plan merge is a post-exit Commander step)

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
python3 "$OMC_ROOT/hooks/review_gate.py" advance-critic-queue /path/to/repo security,P1-item --increment-round
```

Stop hook at `phase=revise` emits critic-queue followup when pending items exist.

## Exit conditions

- critic-queue empty + all critics signed off → `phase: accepted`
- **Original plan updated** with hyperplan revisions (Commander checklist in plan-then-hyperplan.md)
- max_rounds exhausted → best spec + unresolved list in report; still merge what was resolved into plan
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
