---
status: accepted
title: oh-my-cursor v1.0 — MAP extraction, install, DEF-09
workflow: map-hyperplan
round: 2
implementation_plan: ~/.cursor/plans/oh-my-cursor_map_extract_29b2ee1e.plan.md
---

> **施工主文档：** 本 spec 已通过 map-hyperplan 审查（round 2）。结论已融合进 `~/.cursor/plans/oh-my-cursor_map_extract_29b2ee1e.plan.md` — **实施以该 plan 为准**；本文档作设计审计存档。

# oh-my-cursor v1.0 — MAP Extraction Spec

## Problem statement

MAP (Multi-Agent PR) lives as unversioned files under `~/.cursor/`. This blocks independent semver/CHANGELOG, CI on the gate engine, team/project pinning via `.cursor/`, and DEF-09 canonical state paths.

**Goal:** Extract MAP into **oh-my-cursor** v1.0 with install (`--link` / `--copy` / `--project`), DEF-09 dual-read + migrate script, and docs/CI — without breaking existing global hooks or legacy merge-gate state.

## Scope

### In scope (v1.0)

| Area | Deliverable |
|------|-------------|
| Core | Migrate `review_gate.py`, 5 schemas, tests, spikes (no logs) |
| Skills | `multi-agent-pr`, `map-hyperplan`, `map-security`, `map-refactor`, `MAP_SKILL_DISCOVERY.md` |
| Agent | `planner.md` |
| Install | `install.sh` + `scripts/install.py` — link / copy / project |
| Paths | `OMC_ROOT`, `OMC_SECRET_FILE`; `hooks.json.template` with `{{OMC_ROOT}}` |
| Security | Secret trust contract, fail-closed `_secret()`, symlink hardening, install allowlist |
| DEF-09 | Canonical write + legacy dual-read + `migrate_map_state.py` (copy default) |
| Ops | `omc doctor --security`, CI workflow, CHANGELOG v1.0.0, state-migration runbook |

### Out of scope

- `gitnexus-*` skills
- issue-to-pr business code
- Multi-user/shared-home threat model (local user = trusted)
- Packaged `omc` CLI (v1.1; v1.0 uses rendered absolute paths in hooks + docs)

## Architecture

```text
oh-my-cursor/
├── install.sh
├── scripts/{install.py, migrate_map_state.py}
├── hooks/{review_gate.py, hooks.json.template, schemas/, spikes/, run_tests.sh}
├── skills/{multi-agent-pr, map-*, MAP_SKILL_DISCOVERY.md}
├── agents/planner.md
├── tests/{test_review_gate.py, test_mmr_fixes.py}
├── docs/{architecture.md, install.md, state-migration.md, security.md}
└── .github/workflows/ci.yml
```

**Path variables (must not be conflated):**

| Variable | Meaning |
|----------|---------|
| `OMC_ROOT` | Immutable package root (install source) |
| `OMC_SECRET_FILE` | Mutable HMAC secret path (default: `$OMC_ROOT/hooks/.review-gate-secret`) |
| Legacy secret | `~/.cursor/hooks/.review-gate-secret` — read/migrate on first run |

Install targets: `~/.cursor/` (global) or `repo/.cursor/` (project). Consumer git repos use `.review/` (canonical) with `.review-session/` legacy read fallback until **v2.0.0**.

## Secret trust contract (P0)

1. **No ephemeral fallback.** `_secret()` must fail closed (`sys.exit(2)`) if the secret file is unavailable. Current `review_gate.py:228-229` ephemeral random key is **removed** in omc v1.0.
2. **One-time migration.** If `~/.cursor/hooks/.review-gate-secret` exists and `OMC_SECRET_FILE` does not → **copy** (not move) with mode `0600`. Never regenerate when legacy exists.
3. **Symlink hardening.** On read: reject symlinks (`O_NOFOLLOW` or `lstat` check), enforce mode `0600`, owner == euid.
4. **Creation.** `O_CREAT|O_EXCL`, parent dir `0700` where feasible.
5. **Mixed installs.** Global + project install with different secrets is **unsupported**. `omc doctor` fails if hooks resolve to conflicting secret paths.

## Install integrity (P0)

### Merge semantics

- Parse and validate existing `hooks.json`; preserve unknown hooks and fields.
- **MAP allowlist:** only entries whose `command` matches `python3 "<OMC_ROOT>/hooks/review_gate.py"` (rendered absolute path at install time).
- Refuse merge if modification would touch non-MAP entries.
- Atomic write; backup `hooks.json.bak.<timestamp>`; rollback on validation failure.
- Provide `install.py --uninstall` to restore from latest backup.

### Install modes

| Mode | Behavior | Production |
|------|----------|------------|
| `--link` | Symlink omc assets → target | Dev only; requires `--i-know-symlink-risk` |
| `--copy` | Copy assets to target | **Default for production** |
| `--project PATH` | Install into `PATH/.cursor/` | Supported; adds `.gitignore` entry for secret |

`--project` must idempotently append `.cursor/hooks/.review-gate-secret` to target repo `.gitignore`.

## DEF-09 path contract

| Purpose | Legacy (read fallback) | Canonical (write) |
|---------|------------------------|-------------------|
| Session markers | `.review-session/<branch>/<sha>/` | `.review/session/<branch>/<sha>/` |
| Session summary | `.review-session.json` | `.review/session-summary.json` |
| Verdict | `.review-verdict.json` | `.review/verdict.json` |

### Helper API (split read/write — P0)

Do **not** overload read resolution into write paths:

```python
def _session_marker_read_dir(git_root, branch, head_sha) -> Path:
    """Canonical if any markers exist, else legacy."""

def _session_marker_write_dir(git_root, branch, head_sha) -> Path:
    """Always canonical."""

def _verdict_read_path(git_root) -> Path:
    """Canonical if exists, else legacy."""

def _verdict_write_path(git_root) -> Path:
    """Always canonical."""
```

**Sunset:** Legacy read removed in **v2.0.0** (target 2027-Q1). v1.x emits deprecation warning when legacy-only paths detected.

### migrate_map_state.py

- **Default:** dry-run; **copy** all artifacts (never move by default).
- **`--apply`:** copy session dir, verdict, summary to canonical paths.
- **`--apply --destructive`:** move session dir (opt-in); requires typed confirmation.
- Abort if canonical destination exists (no overwrite).
- Require `.review/config.json` `active: false` or `--force`.
- Emit manifest; support `--rollback` from manifest.

## Skill / doc path strategy

- Hooks: rendered **absolute** paths at install time (not shell `$OMC_ROOT` for hook commands).
- Skills/docs: reference `omc doctor` discovery output or install-printed paths.
- Replace hardcoded `~/.cursor/hooks/` in all MAP skills during Phase 4.

## Implementation phases

| Phase | Work | Acceptance |
|-------|------|------------|
| 1 | `git init`, migrate files, consolidate tests | `run_tests.sh` runs all modules (~42 pass) |
| 2 | `install.py` + template + merge allowlist | `--copy`/`--link`/`--project` smoke; merge idempotency test |
| 2b | Secret contract + `omc doctor --security` | ≥6 security tests (seal, forge reject, symlink, migration, permissions, merge refuse) |
| 3 | DEF-09 split helpers + migrate + tests | Legacy-only + canonical-only fixtures pass; ≥8 DEF-09 tests |
| 4 | Docs, SKILL paths, expanded CI | grep no stale absolute paths; install smoke in CI |
| 5 | Global cutover, tag v1.0.0 | `~/.cursor` no longer sole SoT; link + copy smoke |

**Estimate:** **5–7 days** (full v1.0 scope per user decision to include DEF-09 + 3 install modes).

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R1 | hooks.json merge breaks non-MAP hooks | Allowlist merge; backup; doctor check |
| R2 | DEF-09 breaks in-flight PRs | Dual-read; migrate copy-only default |
| R3 | Symlink invisible in sandbox | `--copy` production default |
| R4 | Absolute paths in skills/docs | CI grep + template enforcement |
| R5 | subagentStop / Cursor version drift | CHANGELOG; pin tag in consumer repos |
| R6 | Secret path split-brain | Legacy copy migration + doctor |
| R7 | Symlink hijack of SECRET_FILE | O_NOFOLLOW + doctor |
| R8 | hooks.json merge injection | Allowlist + review_gate.py hash in doctor |

## Threat model

- **Trusted:** local user running Cursor and install scripts.
- **Out of scope:** multi-user shared `$HOME`, remote attackers without local shell.
- CLI `advance-critic-queue` / `advance-fix-queue`: local user trusted.

## Definition of Done (v1.0)

- [x] Install contract documented (3 modes + merge allowlist)
- [x] DEF-09 path table + v2.0.0 sunset defined
- [x] Migration runbook: copy default, destructive opt-in
- [x] Secret trust contract specified
- [x] Critics P0 addressed in spec (round 2)
- [x] Skill path strategy: rendered absolute paths + doctor
- [ ] Implementation (post-hyperplan, separate execution)

## Hyperplan resolution (round 2)

| Critic item | Resolution |
|-------------|------------|
| arch-p0-secret | § Secret trust contract + OMC_ROOT / OMC_SECRET_FILE split |
| arch-p0-def09 | § Helper API split read/write |
| sec-p0-1 | Fail-closed `_secret()` required |
| sec-p0-3 | Symlink hardening in secret contract |
| sec-p0-4 | § Install integrity allowlist |
| cost-p1-estimate | Estimate updated to 5–7 days |
| cost-p1-sunset-ci | v2.0.0 sunset + CI runs all test modules |
