# DEF-09 — State path migration

MAP review state moves from repo-root legacy paths to canonical `.review/` layout.

## Path mapping

| Purpose | Legacy (read fallback) | Canonical (write) |
|---------|------------------------|-------------------|
| Session markers | `.review-session/<branch>/<sha>/` | `.review/session/<branch>/<sha>/` |
| Session summary | `.review-session.json` | `.review/session-summary.json` |
| Verdict | `.review-verdict.json` | `.review/verdict.json` |

- **v1.x:** dual-read (legacy OR canonical)
- **v2.0.0 (2027-Q1):** legacy read removed (planned)

## Migrate a repository

Dry-run (default):

```bash
python3 scripts/migrate_map_state.py /path/to/repo
```

Apply (copy — non-destructive):

```bash
python3 scripts/migrate_map_state.py /path/to/repo --apply
```

Destructive session move (opt-in):

```bash
python3 scripts/migrate_map_state.py /path/to/repo --apply --destructive --confirm MOVE
```

Requirements:

- Canonical targets must **not** already exist (no overwrite)
- If `.review/config.json` has `"active": true`, pass `--force` or stop the MAP session
- Manifest written to `.review/migrate-manifest.json` on `--apply`

Rollback:

```bash
python3 scripts/migrate_map_state.py /path/to/repo --rollback
```

## After migration

- New markers and verdicts write to canonical paths only
- Legacy files remain until manually removed (copy mode) or moved (destructive mode)
- `review_gate.py` emits `MAP_DEF09` stderr warning when legacy-only paths are detected

See [`.specs/oh-my-cursor.md`](../.specs/oh-my-cursor.md) and [security.md](security.md).
