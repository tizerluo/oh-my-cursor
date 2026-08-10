---
name: multi-agent-pr
description: >-
  Multi-agent PR development workflow: Commander orchestrates spec writing,
  Architect review, Coder implementation, and three-engine parallel code review
  (correctness/quality/architecture). Use when the user says "multi-agent PR",
  "multi-agent workflow", "multi-engine review", asks to develop a feature
  with architect+coder+reviewer pipeline, or references this workflow pattern.
---

# Multi-Agent PR Workflow

Commander-driven development pipeline with Architect gate, Coder implementation, independent Tester coverage, and mandatory non-Commander model review before merge. Proven on 4 PRs (43 files, 811 tests, 5 P0s caught pre-implementation).

## Roles

| Role | Model | Subagent Type | Focus |
|------|-------|---------------|-------|
| Commander | parent agent | — | Spec, adjudicate, merge |
| Architect | `gpt-5.5-medium` | `architect` | Spec review → P0/P1/P2 |
| Coder | `composer-2.5-fast` | `coder` | Implement + unit tests + commit |
| Tester | `kimi-k2.7-code` | `tester-writer` | Integration tests, boundary tests, contract validation |
| Reviewer-Grok | `grok-4.5` | `reviewer-grok` | Correctness, boundaries, concurrency |
| Reviewer-Codex | `gpt-5.3-codex-high-fast` | `reviewer-codex` | Quality, DRY, naming, patterns |
| Reviewer-Gemini | `gemini-3.1-pro` | `reviewer-gemini` | Architecture consistency, config propagation |

## Pipeline

```
Commander writes spec (.specs/prN-xxx.md)
       ↓
Architect reviews spec → P0/P1 list
       ↓
Commander adjudicates → revises spec
       ↓
Coder implements on feature branch + unit tests
       ↓
Tester writes integration/boundary tests  ← 独立视角补测试
       ↓
┌─────────────┬─────────────┬──────────────┐
│ Grok        │ Codex       │ Gemini       │  ← parallel
│ correctness │ quality     │ architecture │
└──────┬──────┴──────┬──────┴──────┬───────┘
       ↓             ↓             ↓
Commander synthesizes → deduplicate → adjudicate
       ↓
Coder fixes P1s → tests pass
       ↓
(Optional R2 if needed)
       ↓
Minimum Review Gate: write `.review/verdict.json`
       ↓
Commander: CI green → merge
```

Hotfix is a shortened variant, not a review bypass:

```
Commander classifies tier + writes short note
       ↓
Coder subagent implements fix (Commander MUST NOT edit code directly)
       ↓
Reviewer-Grok reviews correctness
       ↓
Commander synthesizes verdict → writes `.review/verdict.json`
       ↓
CI green → merge
```

## When to Use Each Tier

| Scope | Files | Process |
|-------|-------|---------|
| Hotfix | 1-3, no new logic, no cross-module contract change | Coder subagent implements; Commander MUST run at least 1 reviewer subagent before merge |
| Standard | 3-10 or any meaningful behavior change | Full pipeline, 1 review round usually enough |
| Large | 10+ or security/daemon/push/auth/scheduler work | Full pipeline, expect R2 |

There is no zero-review merge path. Even a one-line fix requires at least one non-Commander model review.

## Enforcement Rules

These rules are mandatory for every use of this skill:

- MUST have at least 1 non-Commander model review before merge, for every tier.
- MUST use all three reviewer subagents for Standard and Large work. No scope-narrowing escape hatch.
- MUST launch `Task(subagent_type="coder")` for implementation in every tier. Commander MUST NOT edit implementation files directly.
- MUST write **`.review/verdict.json`** after review synthesis and before any merge command (legacy `.review-verdict.json` read until v2.0.0).
- MUST ensure **`.review/session/<branch>/<HEAD>/`** contains hook-recorded subagent marker files (legacy `.review-session/` read until v2.0.0).
- MUST document review model(s), verdict, and remaining P2s in the PR body, commit message, or handoff.
- NEVER merge with only Commander's own verification, even for a 1-line fix.
- NEVER hand-write session markers or session summary JSON. Hooks record subagent completions automatically (canonical write: `.review/session/`, `.review/session-summary.json`).
- NEVER classify daemon scheduling, push/auth, trusted login, CI/merge automation, or security-sensitive work as Hotfix.
- NEVER classify a production/daemon runtime crash as Hotfix if the fix involves cross-module assumptions.
- NEVER declare a tier lower than the hook-inferred minimum from the git diff.
- MUST escalate to Standard if module A assumes module B's data format, return shape, auth behavior, or side effects.
- MUST include both sides of a cross-module contract in reviewer prompts.
- MUST document in spec or handoff why a change qualifies as Hotfix instead of Standard.

## Hooks Enforcement

Cursor Hooks provide hard guardrails around the soft skill rules.

User-level hooks enforce:

- `beforeShellExecution` merge gate: fail-closed for merge/push commands
- `preToolUse` merge-prep gate: fail-open Shell merge validation
- `preToolUse` Task alignment gate: enforced only when `.review/config.json` has `active=true` and spawn uses a MAP-managed role (DEF-02)
- `preToolUse` Write/Delete permission gate: fail-closed role matrix (V1.3)
- `preToolUse` Shell write-pattern gate: blocks redirects/heredoc/cp/mv for read-only MAP roles (V1.3 depth defense)
- `subagentStart` set-role: writes `.review/roles/{subagent_id}.json`
- `subagentStop` session recorder: HMAC-sealed markers under **`.review/session/`** (canonical write; legacy `.review-session/` read fallback)
- `stop` fix-loop driver: reads fix-queue when phase allows (WORKFLOW_STOP_PHASES)
- `sessionStart` resume + routing hints: injects additional_context (recommendation only)

The merge gate is intentionally fail-closed. It validates all of the following:

- `.review/session/<branch>/<HEAD>/` (or legacy `.review-session/`) contains marker files for required subagents
- Marker files are hook-sealed; hand-written marker JSON is rejected
- `.review/verdict.json` (or legacy `.review-verdict.json`) exists and matches current branch + HEAD
- Required reviewer subagents were actually recorded for the tier
- Every tier has a recorded `coder` subagent completion
- Declared tier is not lower than the hook-inferred tier from git diff/file risk
- Verdict reviewer claims match recorded reviewer marker types/models
- Verdict reports `p0: 0` and `p1: 0`

Markers are written by hooks when subagents finish (canonical: `.review/session/`). `.review/session-summary.json` is a human-readable derived summary only; the merge gate validates marker files, not the summary. Commander MUST NOT forge either.

**Note (Cursor 3.15+):** lifecycle payloads deliver hyphenated subagent types (e.g. `general-purpose` instead of `generalPurpose`). v1.3 hooks normalize these before role/marker recording — if session markers are missing after subagent completion, verify the installed hook version includes F12 normalization.

Migrate legacy paths: `python3 scripts/migrate_map_state.py <repo> --apply` (see oh-my-cursor [state-migration.md](https://github.com/tizerluo/oh-my-cursor/blob/main/docs/state-migration.md)).

### Tool permission depth defense (V1.3)

Write/Delete hook + Shell write-pattern interception provide **depth defense**, not a kernel-level sandbox.
A trusted Commander can still write files indirectly. The design goal is preventing **subagent misuse**, not malicious Commander bypass.

Role matrix (Write/Delete):
- `reviewer-*`, `explore`: read-only
- `architect`: `.specs/`, `.review/` only
- `tester-writer`: `tests/` only
- `poc-exploit`: `.review/poc/` only (via `logical_role` in `.review/roles/`)
- `generalPurpose` (map-hyperplan critics): `.review/reports/` only

Shell gate blocks redirect/heredoc/`cp`/`mv`/`touch`/`mkdir`/`sed -i` for the same read-only roles.
PoC role may redirect only into `.review/poc/`.
Read-only shell (`git diff`, `pytest`, `rtk grep`) is allowed.

**`.review/verdict.json`** is written by Commander after synthesis (legacy read path supported until v2.0.0). Use this format:

```json
{
  "branch": "fix/tester-audit-author",
  "head_sha": "abc123def456",
  "tree_sha": "tree123optional",
  "tier": "hotfix",
  "reviewers": ["grok-4.5"],
  "verdict": "pass",
  "p0": 0,
  "p1": 0,
  "p2": 2,
  "date": "2026-06-07"
}
```

Tier requirements enforced by hooks:

| Tier | Required subagent records |
|------|---------------------------|
| Hotfix | `coder` + at least 1 `reviewer-*` |
| Standard | `coder`, `reviewer-grok`, `reviewer-codex`, `reviewer-gemini` |
| Large | `coder`, `reviewer-grok`, `reviewer-codex`, `reviewer-gemini` |

The marker directory, derived summary, and verdict are ephemeral and MUST NOT be committed. Any new commit invalidates prior review state until reviewers re-run on the new HEAD.

## Step-by-Step

### 0. Tier Classification (Commander)

Classify scope before implementation:

- Hotfix: tiny, local, no new logic, no cross-module contract risk. Requires Coder subagent + 1 reviewer subagent.
- Standard: normal feature or bug fix. Requires Architect, Coder, optional Tester, and 3-engine review.
- Large: high-risk or broad change. Requires full pipeline and likely R2.

If unsure, choose the higher tier.

### 0.5 Configuration Gate (Commander) — MANDATORY

Commander MUST call AskQuestion before spawning any subagent. There is NO exemption.
Even if the user specified config in their request, present it as pre-selected defaults
for explicit confirmation.

Commander MUST NOT spawn any subagent until `.review/config.json` is written with
`active: true`.

**Question 0 — Workflow (intent routing)**

When MAP workflow skills and registry are installed (V1.3+), offer all four workflows.
Commander analyzes the user request and recommends the best fit:

| Option | Workflow ID | Use when |
|--------|-------------|----------|
| multi-agent-pr | `multi-agent-pr` | feature, fix, implementation, merge |
| map-hyperplan | `map-hyperplan` | spec review, design debate, architecture planning |
| map-security | `map-security` | security audit, vulnerability hunt |
| map-refactor | `map-refactor` | refactor with baseline + regression (MAP mode) |

If a workflow skill is not installed, do not offer that option in AskQuestion.

After selection, Commander MUST read the corresponding skill pipeline (this skill for
`multi-agent-pr` / `map-refactor`; `map-hyperplan` or `map-security` skills otherwise).

**Question 1 — Tier** (multi-agent-pr / map-refactor only)

"Commander 建议 {tier}（原因：{reason}）。确认或调整："
Options: Hotfix / Standard / Large. Tier MUST NOT be below hook-inferred minimum.

**Question 2 — Models** (allow_multiple)

"选择模型（{workflow} 至少需要 {N} 个）："
Defaults pre-selected by workflow + tier.

**Question 3 — Max iterations**

- multi-agent-pr / map-refactor: fix-review rounds (1 / 2 recommended / 3 / 5 platform max)
- map-hyperplan: debate rounds (default 2, 3 critics per round)
- map-security: hunt rounds (default 2)

**Question 4 — Skip roles** (workflow-specific, allow_multiple)

**Question 5 — Cost estimate** (informational)

Display: `预估子代理调用次数：{workflow} x {models} x {rounds} ≈ {N} 次`

Changing reviewer models in `hooks/config/models.json` requires starting a **new MAP session** (session `config.json` is the contract the merge gate checks).

After confirmation, write `.review/config.json` and initialize `.review/progress.json`
with `phase: config-confirmed`.

#### `.review/config.json` schema (discriminated union v2)

`workflow` is required. Examples:

**multi-agent-pr / map-refactor:**

```json
{
  "schema_version": 2,
  "workflow": "multi-agent-pr",
  "session_id": "map-YYYYMMDD-xxxxxx",
  "active": true,
  "branch": "feature/my-branch",
  "head_sha": "<current HEAD>",
  "tier": "standard",
  "roles": ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
  "models": ["grok-4.5", "gpt-5.3-codex-high-fast", "gemini-3.1-pro"],
  "max_rounds": 2,
  "skip_architect": false,
  "skip_tester": false,
  "merge_gate_required": true,
  "confirmed_at": "<ISO8601>"
}
```

See `map-hyperplan` and `map-security` skills for other workflow config shapes.

#### Intent routing signal table (recommendation only — config is contract)

```
"写个 spec" / "设计方案"     → map-hyperplan
"安全审计" / "查漏洞"         → map-security
"重构" / "拆分" / "迁移"      → map-refactor
"开发" / "实现" / "fix"       → multi-agent-pr
ambiguous                     → show all options with Commander recommendation
```

Machine-readable rules: `.review/routing-rules.json` (optional, V2.0+).

Layer 1 (skill descriptions) and Layer 3 (sessionStart hints) are recommendations only.
Only AskQuestion + written config authorizes workflow execution.

### 0.6 MAP State Directory (`.review/`)

```
.review/
  config.json          # alignment gate output (workflow required)
  progress.json        # phase state machine
  fix-queue.json       # fix-review loop (multi-agent-pr, map-refactor)
  critic-queue.json    # hyperplan revise loop
  security-queue.json  # optional unverified High+ findings
  reports/             # debate, baseline, regression, security reports
  poc/                 # security PoC sandbox (gitignored)
  knowledge/pr{N}/      # persistent learnings + decisions
  roles/{subagent_id}.json  # tool permission state
```

Legacy `.review-session/` and `.review-verdict.json` are read-only fallbacks (v1.x); always **write** to `.review/session/` and `.review/verdict.json`.

**Writer separation:** Commander writes `phase`, `completed`, `head_sha`, `branch`;
stop hook writes `fix_round` only when automating fix loops.

**Phase state machine (multi-agent-pr):**

```
config-confirmed → spec-writing → architect-review → adjudication
  → coding → testing → review-pending → synthesis-complete
  → [P0/P1 > 0] fix-round-{N} → review-pending → synthesis-complete
  → [P0/P1 = 0] merge-ready → merged → cleanup
```

If CI regresses after `merge-ready`, recover through `merge-ready → fix-round-N → review-pending`.

When P0/P1 > 0 after synthesis: write fix-queue, keep phase at `synthesis-complete`.
Stop hook may emit a followup reminder at `synthesis-complete`; Commander drives fix-round manually.
Do **not** expect stop hook to advance `phase` to `fix-round-N`.

### 1. Spec (Commander)

Create `.specs/prN-description.md` with **YAML frontmatter** (required):

```yaml
---
status: draft   # draft | in-review | accepted | superseded
title: PR N — short title
---
```

Schema: `hooks/schemas/spec-frontmatter.schema.json` (under `$OMC_ROOT`)

Body must include:
- Goal (numbered list of deliverables)
- File change manifest (file → change type → content)
- Function signatures with key parameters
- Test plan (class + method names)
- Backward compatibility notes
- Constraints

Hotfix may use a shorter written note instead of a full spec, but it MUST include:

- Why this is Hotfix instead of Standard
- Files expected to change
- The failure being fixed
- Reviewer prompt inputs and suspected risks

### 2. Architect Review

Launch foreground subagent (required on Cursor 3.15+):

```
Task(subagent_type="architect", model="gpt-5.5-medium", run_in_background=false)
```

Prompt must include:
- Path to spec file
- List of files to read (existing code for context)
- Review criteria: P0 (blocking) / P1 (severe) / P2 (advisory)
- Specific focus areas (security, backward compat, race conditions)
- If `.review/knowledge/` exists: "Read the 3 most recent pr*/learnings.md files
  (by PR number) under .review/knowledge/ for historical context."

**Large tier — adversarial Architect (optional but recommended):**

Launch 2 Architect subagents with different models in parallel:

```
Task(subagent_type="architect", model="gpt-5.5-medium", run_in_background=false, ...)
Task(subagent_type="architect", model="gemini-3.1-pro", run_in_background=false, ...)
```

After both complete, Commander cross-sends findings for 1 rebuttal round, then synthesizes:
- Both agree on P1 → high confidence, must fix
- Only one flags P1 → Commander judgment
- Contradictions → Commander resolves with reasoning

Hotfix may skip Architect only if it passes every Hotfix guardrail in "When to Use Each Tier".

### 3. Commander Adjudication

For each Architect finding:
- **P0**: Must fix in spec before coding
- **P1**: Must fix or downgrade with written justification
- **P2**: Accept as advisory, not blocking

Update the spec with an "Architect Review 裁决" section documenting all decisions. Coder must follow this section.

Hotfix may skip this step only when Architect was skipped.

### 4. Coder Implementation


> **Cursor 3.15+ — foreground subagents only:** The platform never fires `subagentStop` for **background** subagents (`run_in_background=true`). MAP merge-gate markers are recorded only on `subagentStop`, so **every** MAP subagent spawn (Coder, Architect, Tester, Reviewers) **MUST** use `run_in_background=false`. Foreground spawns are required for hook-recorded session markers under `.review/session/`.
>
> **Upstream watch:** Cursor also documents that `subagentStop` payloads may omit `summary` / `modified_files` and often carry `agent_transcript_path: null` — MAP works around this via the workspace cache (F2). Re-evaluate background spawns and cache simplification when upstream fixes land. Forum: https://forum.cursor.com/t/subagentstop-never-fires-for-background-subagents-documented-summary-modified-files-agent-transcript-path-missing-or-null/166681

Launch foreground subagent (required on Cursor 3.15+):


```
Task(subagent_type="coder", model="composer-2.5-fast", run_in_background=false)
```

Hotfix MUST use Coder. Commander MUST NOT implement Hotfix code directly.

Prompt must include:
- Branch name (create before launching)
- Path to spec file (don't repeat spec content)
- Which steps to implement
- Critical implementation notes from Architect adjudication
- Test command (`rtk python -m pytest tests/ -x -q` or project equivalent)
- Commit instruction

### 4.5 CI Watch (Optional)

After Coder pushes, Commander MAY use `/loop` dynamic mode to monitor CI:

- Event trigger: `gh run list --branch {branch} --json status,conclusion`
- Fallback heartbeat: 2 minutes
- On CI pass: continue to Step 5 (Tester) or Step 6 (Review)
- On CI fail: spawn Coder with error log to fix

**IMPORTANT:** CI Watch MUST be disabled during Fix-Review Loop when
`progress.phase` starts with `fix-round-`. The two loops are mutually exclusive.

This step is optional. Commander may also check CI manually.

### 5. Tester (Independent Test Authoring)

Coder 写的测试容易有"实现者盲区"（倾向 happy path、过度 mock 内部函数）。Tester 从独立视角补写测试，重点覆盖 Coder 不会写的部分。

Launch foreground subagent (required on Cursor 3.15+):

```
Task(subagent_type="tester-writer", model="kimi-k2.7-code", run_in_background=false)
```

Prompt must include:
- Branch name and spec path
- Coder 已写的测试文件列表（避免重复）
- 明确要求 Tester **不要** mock 被测函数内部调用，写跨模块集成测试

**Tester 重点覆盖**（与 Coder 互补）：

| Coder 倾向写 | Tester 应该写 |
|--------------|--------------|
| 单元测试（mock 内部依赖） | 集成测试（不 mock，验证跨模块契约） |
| Happy path | 边界条件（空输入、None、超长、类型错误） |
| 正常返回值 | 异常路径（网络失败、权限拒绝、超时） |
| 当前配置 | 配置缺失 / 非默认配置组合 |

**何时跳过 Tester**：
- Hotfix（1-3 文件），Coder/Commander 测试已足够，且 reviewer agrees
- 纯重构 PR（无新逻辑，只移动代码）
- Coder 已在 spec 指导下写了集成测试

**何时必须用 Tester**：
- 涉及 daemon / 安全 / push 权限的改动
- 跨 3+ 模块的新功能
- spec 测试计划标注了"集成测试"类别

### 6. Review Gate

Cursor Task only accepts platform subagent types (`generalPurpose`, `coder`, etc.). MAP logical roles `reviewer-*` are **not** valid Task enums — spawn reviewers via the platform table below so hooks record the correct marker `type`.

| MAP logical role | Platform `subagent_type` | `readonly` | Model (seat) |
|------------------|--------------------------|------------|--------------|
| `reviewer-grok` | `generalPurpose` | `true` | `grok-4.5` |
| `reviewer-codex` | `generalPurpose` | `true` | `gpt-5.3-codex-high-fast` |
| `reviewer-gemini` | `generalPurpose` | `true` | `gemini-3.1-pro` |

Each reviewer prompt must include `logical_role: reviewer-<engine>` (or `Reviewer-Grok` / `Reviewer-Codex` / `Reviewer-Gemini`) so `subagentStart` writes the correct role file and `subagentStop` emits a `reviewer-*` merge-gate marker.

Hotfix review path (one reviewer):

```
Task(subagent_type="generalPurpose", model="grok-4.5", readonly=true, run_in_background=false,
     prompt="You are MAP Reviewer-Grok. logical_role: reviewer-grok\n...")
```

Standard/Large review path — launch **three subagents in parallel** in one message:

```
Task(subagent_type="generalPurpose", model="grok-4.5",         readonly=true, run_in_background=false, prompt="... logical_role: reviewer-grok\n...")
Task(subagent_type="generalPurpose", model="gpt-5.3-codex-high-fast", readonly=true, run_in_background=false, prompt="... logical_role: reviewer-codex\n...")
Task(subagent_type="generalPurpose", model="gemini-3.1-pro",          readonly=true, run_in_background=false, prompt="... logical_role: reviewer-gemini\n...")
```

Do **not** use `Task(subagent_type="reviewer-grok", ...)` — Cursor rejects it; the pre-Task hook denies it with the spawn template above.
- Path to spec file
- List of ALL files to read
- Their specific focus area
- Scoring criteria: P0/P1/P2 with file, function, description, fix

### 7. Commander Synthesis

Wait for the required reviewer set: one reviewer for Hotfix, all three reviewers for Standard/Large. Then:

1. **Deduplicate**: Same issue found by multiple reviewers = high confidence P1
2. **Adjudicate**:
   - Cross-engine consensus P1 → must fix
   - Single-engine P1 → Commander judgment (fix or downgrade)
   - P2 → accept, not blocking
3. Compile fix list for Coder
4. Evaluate P0/P1 counts and branch:

**If P0 + P1 > 0:**
1. Write `.review/fix-queue.json` (session_id, branch, head_sha, round, p0_issues, p1_issues)
2. Update `.review/progress.json`: keep `phase = synthesis-complete` (stop hook may remind only)
3. DO NOT write `.review/verdict.json` (verdict = clean only)
4. Proceed to Step 8 (Fix-Review Loop)

**If P0 + P1 = 0:**
1. Write `.review/verdict.json` for current branch and HEAD
2. Delete `.review/fix-queue.json` if it exists
3. Update `.review/progress.json`: `phase = merge-ready`
4. Write knowledge artifacts (see below)
5. Proceed to Step 9 (Merge)

#### Knowledge accumulation (after clean verdict)

Create `.review/knowledge/pr{N}/learnings.md`:
- Each entry tagged with source engine, date, severity
- Cross-engine consensus findings (highest confidence)
- Single-engine unique findings worth preserving
- Bug patterns for future reviewer prompts
- Prefer entries with ≥2 engine consensus; label single-engine with confidence

Create `.review/knowledge/pr{N}/decisions.md`:
- Architect adjudication decisions
- Commander discretion on P2s
- Scope changes during review
- Each entry should include **decision**, **rationale**, **date**

Soft validation (warnings only): `validate_knowledge_artifacts` in review_gate — learnings need `sources:` (≥2 engines) or `confidence: single-engine`; archive after 10 unreferenced PRs per `.review/knowledge/index.json` (future).

### 8. Fix-Review Loop

Commander drives manually. Stop hook at `synthesis-complete` may emit a one-time followup when fix-queue has pending items; it does **not** auto-advance phase to `fix-round-N`.

1. Read `.review/fix-queue.json` for issue list
2. Spawn Coder with specific P0/P1 fixes
3. After Coder commits: update `.review/progress.json` — `head_sha = new HEAD`, `phase = review-pending`
4. Re-launch tier-required reviewers on new HEAD
5. Return to Step 7 (Synthesis)

**After each fix-review cycle**, advance queue state (resets phase to `synthesis-complete` for stop hook):

```bash
python3 "$OMC_ROOT/hooks/review_gate.py" advance-fix-queue /path/to/repo resolved-id-1,resolved-id-2 --increment-round
```

**Exit conditions:**
- P0 = 0 and P1 = 0 → Step 7 writes verdict → Step 9
- `fix_round >= max_rounds` from config → AskQuestion:
  "达到最大修复轮数。仍有 P0/P1。继续 1 轮 / 人工审查 / 放弃 PR"
- User requests stop → cleanup and report

#### `.review/fix-queue.json` format

```json
{
  "schema_version": 1,
  "session_id": "map-YYYYMMDD-xxxxxx",
  "branch": "feature/my-branch",
  "head_sha": "<HEAD>",
  "round": 1,
  "p0_issues": [],
  "p1_issues": [{"file": "path", "line": 42, "desc": "...", "source": "grok"}],
  "created_at": "<ISO8601>"
}
```

#### `.review/progress.json` format

```json
{
  "schema_version": 2,
  "workflow": "multi-agent-pr",
  "session_id": "map-YYYYMMDD-xxxxxx",
  "branch": "feature/my-branch",
  "head_sha": "<HEAD>",
  "phase": "synthesis-complete",
  "fix_round": 0,
  "max_rounds": 2,
  "completed": ["architect", "coder", "reviewer-grok"],
  "updated_at": "<ISO8601>"
}
```

### 8b. map-refactor mode (when config.workflow == "map-refactor")

Insert after Step 2 (Architect):

**3.5 Baseline**
- AskQuestion: confirm `baseline_test_cmd` (auto-detect from pyproject.toml / package.json / Cargo.toml; user must confirm)
- Run tests; store summary in `.review/reports/baseline-{base_sha}.json` (not inline in progress.json)
- Record pointer in progress.json

After Step 4 (Coder):

**4.5 Regression**
- Re-run same test command
- Diff baseline vs post-refactor → `.review/reports/regression-{head_sha}.json`
- New failures → P0 in fix-queue
- Flaky handling: retry up to 2 times; quarantine list in `.review/quarantine-tests.json` if known flaky

Step 6 Review: use standard reviewer-* with REGRESSION_REVIEW_PROMPT (behavior equivalence, API compat, perf).

Uses standard merge gate + fix-queue.

### 9. Merge

Commander checklist:
- [ ] At least one non-Commander model reviewed the change
- [ ] No P0 or P1 in latest review round
- [ ] All tests pass locally
- [ ] CI green
- [ ] Required subagent marker files exist under `.review/session/<branch>/<HEAD>/`
- [ ] `.review/verdict.json` exists and records reviewer model(s) + verdict
- [ ] `git push -u origin <branch>` + `gh pr create` + `gh pr merge --squash --delete-branch`

**After successful merge — session cleanup:**
1. Set `.review/config.json`: `active = false`
2. Delete `.review/fix-queue.json` (if exists)
3. Delete `.review/progress.json`
4. Delete `.review/roles/` session files (if exists)
5. Keep `.review/knowledge/` (persistent across PRs)
6. Use canonical `.review/session/` and `.review/verdict.json` (HEAD invalidation handled by gate)

## Merge Criteria

**Minimum bar** (non-negotiable):
- Zero P0 issues
- Zero P1 issues
- At least one non-Commander model review
- `.review/verdict.json` written before merge
- `.review/session/<branch>/<HEAD>/` markers match current branch + HEAD
- CI passes

**Commander discretion**:
- P2 issues accepted but documented
- Scope cuts allowed only before review and only if the hook-inferred tier still matches the declared tier

## Key Lessons

1. **Architect catches P0s that would be runtime crashes** — never skip for large PRs
2. **Tester 必须独立于 Coder** — Coder 自测有盲区（过度 mock、忽略跨模块契约），独立 Tester 能在 Review 前就暴露这些问题，减少 Review P1 数量
3. **Three engines find different things** — Gemini catches config propagation breaks that others miss
4. **Spec revision before coding saves rework** — fix architecture in spec, not in code
5. **Dedup across reviewers** — consensus P1s are highest priority
6. **Keep PRs to 10-15 files** — larger PRs need more review rounds
7. **PR #56 lesson** — the tester_audit bug was a 1-line cross-module contract error. Small-looking fixes can still need a second model because the risk is in the assumption, not the diff size.

## Customization

Override models in the prompt if user requests specific ones. Available models for reference in [roles.md](roles.md).

Override review focus by modifying reviewer prompts. The three-engine split (correctness/quality/architecture) is a default — adapt to project needs.
