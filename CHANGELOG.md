# Changelog

All notable changes to **oh-my-cursor** are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **v1.3.1 follow-ups (#22):** Cursor 3.15 role lifecycle (active-scan, Commander-first, empty-role Shell deny, StrReplace/MCP shared lookup); doctor `hooks.json` `version` must be integer `1`; `requirements-dev.txt` + `hooks/run_ci_local.sh`; `merge-ready → fix-round-*` recovery; map-refactor terminal phases `merged`/`cleanup`
- Docs: cloud-agent hook limitation; README/CHANGELOG test counts; map-refactor pipeline through `cleanup`

## [1.3.0] - 2026-08-10

### Added

- **Model config externalized** to `hooks/config/models.json` with per-key fallback defaults when keys or the file are missing (F7)
- **Phase state machine** — `VALID_TRANSITIONS` matrix and `safe_transition_phase()` (graceful fallback, never blocks sessions)
- **Unified queue interface** — `QueueManager` base with `FixQueue` / `CriticQueue` / `SecurityQueue`; production wiring for fix/critic advance paths (F8)
- **Two new hook events** (8 types, 11 MAP entries): `preCompact` (`compact-inject`) and `beforeMCPExecution` (`check-mcp-permission`, fail-open)
- **Git operation caching** — `_git_root` / `_branch` / `_head` / `_tree` cached per hook invocation
- **Cursor 3.15 compat** — workspace cache for subagent lifecycle payloads with empty `workspace_roots`; tolerant case/underscore matchers on `subagentStart` / `subagentStop`; `general-purpose` → `generalPurpose` normalization (F2, F12)
- **Install doctor** — validates `hooks/config/models.json` and reviewer model keys (F9)
- **CI** — ruff + mypy (fail-closed), install smoke with `doctor --security`, `py_compile`, stale-path scan (F6)
- **87+ new tests** at tag time (140 → 266 via `hooks/run_tests.sh`); current `main` is **302** after v1.3.1 follow-ups — covering phase transitions, queues, compact-inject, MCP permission, workspace cache, subagent type normalization, role lifecycle

### Changed

- Hooks template consolidated to **11 MAP entries** after F12 matcher unification (was 13 with redundant `subagentStop` rows); `omc doctor` derives expected count from bundled template post-dedupe
- Commander (parent session) may `Write`/`Delete` under `.review/` and `.specs/` without a subagent role file (F10)

### Fixed

- **Phase machine (F1, F3):** current-side glob matching against matrix keys; no silent phase force-write in queue advance; workflow-specific fix targets; skip no-op transitions
- **MCP permission gate (F4, F5):** camelCase tool names; token-based write-verb detection (fewer false positives on read tools)
- MCP write-keyword regex false positives (`settings_get`, `address_lookup`); Chinese hyperplan keyword matching; `_git_cached` missing `OSError` handling; ruff lint issues

## [1.2] - 2026-06-09

### Added

- Public-prep hygiene (#17): redact machine-specific paths, stop tracking engine-repo `.review/` session artifacts, add `SECURITY.md`, `CONTRIBUTING.md`, `.github/CODEOWNERS`, and extend stale-path CI scan
- Public-prep engine hardening (#18): validated PoC sandbox, fail-closed shell gates for MAP roles, install doctor fail-closed, MAP `.gitignore` block for consumer repos
- Repository made public; GitHub topics (`cursor`, `ai-agents`, `merge-gate`, `multi-agent`); private vulnerability reporting enabled
- Post-public docs: README status, clone-reset note for pre-rewrite clones

### Changed

- One-time pre-public `git filter-repo` history rewrite (author email normalization; `.review/` removed from history). Existing clones must reset or re-clone — see [CONTRIBUTING.md](CONTRIBUTING.md#existing-clones-after-public-release)
- Test count: 140+ via `hooks/run_tests.sh` (was 103+ at v1.1)

## [1.1] - 2026-06-09

### Added

- **#1** [docs/workflows/plan-then-hyperplan.md](docs/workflows/plan-then-hyperplan.md): Plan → Hyperplan → merge-back workflow — P0/P1 fusion checklist, Configuration Gate (AskQuestion), debate `claims` acceptance guard alignment, extended anti-patterns

### Fixed

- **#7** P2 hardening: `failClosed: true` on `check-merge` / `check-task-alignment` preToolUse hooks; `O_NOFOLLOW` on marker create; `REVIEW_GATE_TIMEOUT` stderr on subprocess timeout (returncode 124)
- **#8** Reviewer logical role mapping: `generalPurpose` reviewer spawns infer `reviewer-*` from prompt/model; merge-gate markers use logical role; Task denies invalid `subagent_type=reviewer-*` with spawn template
- **#4** Hyperplan: `advance_critic_queue` requires non-empty debate `claims` before `accepted`; Configuration Gate in map-hyperplan SKILL; sessionStart hints

### Changed

- **#7** Shared `tests/map_test_helpers.py` (`load_review_gate`, `SecretBootstrapMixin`) for def09 / security / merge-gate e2e tests
- [skills/map-hyperplan/SKILL.md](skills/map-hyperplan/SKILL.md): cross-reference merge-back checklist and debate claims requirement in workflow doc

### Planned

- v2.0.0: remove legacy `.review-session/` read paths (target 2027-Q1)

## [1.0.2] - 2026-06-09

### Fixed

- **Issue #3** `check_merge_from_hook`: gate `map-hyperplan` deny on `config.active` only; inactive hyperplan falls through to `validate_review_state` for merge/protected push
- **Issue #3** `advance_critic_queue`: auto-deactivate `config.active` when critic queue empties and session_id matches (or is absent)
- **Issue #5** `OMC_LEGACY_SECRET_FILE` env override for legacy secret migration path
- **Issue #6** `migrate_map_state` rollback validates `dest` paths under `manifest.repo`

### Changed

- [docs/workflows/plan-then-hyperplan.md](docs/workflows/plan-then-hyperplan.md): exit checklist notes auto-teardown and multi-agent-pr handoff

## [1.0.1] - 2026-06-09

### Fixed

- **R01** `MERGE_GATE_WORKFLOWS` NameError: cross-check skip uses `merge_gate_required is False` profile guard
- **R02** `_secret()` fail-closed at hook runtime (no auto-create); install/doctor still bootstrap via `bootstrap_secret()`
- **R03–R04** Shell wired to `check-tool-permission`; `SHELL_WRITE_PATTERN` regex (`sponge`, `tee`) fixed
- **R05** `ROUTING_RULES_FALLBACK` uses package-relative path (CI routing tests pass on clean `HOME`)
- **R06–R07** `planner` Write path limits; `_path_allowed` resolves paths against `git_root`
- **R08–R10** Hooks template: `planner`/`tester-writer` matchers, shell-quoted commands, `omc` MAP allowlist + dedupe
- **R11** `migrate_map_state.py` rollback removes copied session directories
- **R12–R16** `_map_exempt_task` scoped to map-hyperplan; subprocess timeouts; config.models bidirectional check
- **R19–R29** Marker idempotent stderr; `_write_json_file` atomic temp; English session-resume; dead code removed

### Changed

- Hooks template: 11 MAP entries (Shell permission + merge), `omc: true` on all MAP hooks
- `omc doctor` expects template MAP count; duplicate detection uses `(command, matcher)` pairs
- Skills/docs: canonical `.review/` write paths emphasized

### Tests

- 98+ tests including `test_merge_gate_e2e.py`, migrate rollback, install quote/allowlist

## [1.0.0] - 2026-06-08

### Added

- Independent MAP engine extracted from personal `~/.cursor` ([`review_gate.py`](hooks/review_gate.py), skills, schemas, tests)
- [`install.sh`](install.sh) / [`scripts/install.py`](scripts/install.py): `--copy` (default), `--link --i-know-symlink-risk`, `--project`
- Non-destructive `hooks.json` MAP allowlist merge; `omc-install.json` manifest
- **Secret trust contract** (Phase 2b): fail-closed `_secret()`, legacy secret copy, symlink/mode checks
- `omc doctor` and `omc doctor --security`
- **DEF-09** (Phase 3): canonical `.review/` write paths, legacy dual-read, [`migrate_map_state.py`](scripts/migrate_map_state.py)
- [`docs/install.md`](docs/install.md), [`docs/security.md`](docs/security.md), [`docs/state-migration.md`](docs/state-migration.md), [`docs/architecture.md`](docs/architecture.md)
- GitHub Actions CI: 81 tests, install smoke, stale-path grep
- map-hyperplan accepted spec at [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md)

### Changed

- Session markers write to `.review/session/<branch>/<sha>/` (read fallback: `.review-session/`)
- Verdict write to `.review/verdict.json` (read fallback: `.review-verdict.json`)
- Skills use `$OMC_ROOT` for CLI examples (set to your oh-my-cursor clone path)

### Released — cutover (2026-06-08)

- Global `~/.cursor` installed via `./install.sh --copy`; `MAP_MOVED_TO.md` at `~/.cursor/`
- Legacy hook shell wrappers removed; `doctor --security` pass on live install
- Consumer example: `docs/integrations/issue-to-pr.md` + `--project` install
- Git tag **v1.0.0**

### Compatibility

- **Cursor 3.7.19+** verified for subagentStop marker path (Live Spike)
- Legacy review paths honored until **v2.0.0** (target 2027-Q1)

### Tests

- 81 unit/integration tests via [`hooks/run_tests.sh`](hooks/run_tests.sh)
