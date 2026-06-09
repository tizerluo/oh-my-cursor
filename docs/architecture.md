# oh-my-cursor architecture

Versioned **MAP (Multi-Agent PR)** engine for Cursor hooks, skills, and install tooling.

## Components

```text
oh-my-cursor/
├── hooks/review_gate.py     # Merge gate, phase control, queues, routing
├── hooks/hooks.json.template
├── scripts/install.py       # Install + doctor
├── scripts/migrate_map_state.py
├── skills/                  # multi-agent-pr, map-*
├── agents/planner.md
└── tests/                   # 103+ tests
```

## Install targets

| Mode | Target | review_gate.py |
|------|--------|----------------|
| `--copy` | `~/.cursor/` or `repo/.cursor/` | Copied into target `hooks/` |
| `--link` | same | Symlink to `$OMC_ROOT/hooks/review_gate.py` |
| `--project PATH` | `PATH/.cursor/` | Copy (default for project) |

`install.py` renders `hooks.json` with absolute paths and merges MAP entries only.

## Review state (consumer git repos)

| Artifact | Canonical (write) | Legacy (read v1.x) |
|----------|-------------------|---------------------|
| Markers | `.review/session/<branch>/<sha>/` | `.review-session/...` |
| Summary | `.review/session-summary.json` | `.review-session.json` |
| Verdict | `.review/verdict.json` | `.review-verdict.json` |
| MAP config | `.review/config.json` | — |

Migrate: [`state-migration.md`](state-migration.md)

## Secret

HMAC marker sealing uses `HOOKS_DIR/.review-gate-secret` (or `OMC_SECRET_FILE`). Legacy `~/.cursor/hooks/.review-gate-secret` is copied on first use. See [`security.md`](security.md).

## Path variable for docs/skills

Set **`OMC_ROOT`** to your oh-my-cursor clone (printed by `./install.sh`):

```bash
export OMC_ROOT=/path/to/oh-my-cursor
python3 "$OMC_ROOT/hooks/review_gate.py" advance-fix-queue ...
```

## Workflows

| Skill | Workflow ID | Merge gate |
|-------|-------------|------------|
| multi-agent-pr | `multi-agent-pr` | Required |
| map-hyperplan | `map-hyperplan` | Permanently blocked |
| map-security | `map-security` | Conditional |
| map-refactor | `map-refactor` | Required + regression |

Planning lifecycle: [workflows/plan-then-hyperplan.md](workflows/plan-then-hyperplan.md)

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs tests, compile check, install smoke, stale-path grep.
