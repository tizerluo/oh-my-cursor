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

Rollback validates every manifest `action["dest"]` resolves under `manifest["repo"]` before deleting or moving files. Tampered manifests that point outside the repository (e.g. `/tmp/evil`) raise `MigrateError` and perform no rollback.

## After migration

- New markers and verdicts write to canonical paths only
- Legacy files remain until manually removed (copy mode) or moved (destructive mode)
- **`--destructive` moves only the session directory.** Verdict and session-summary are always copied (never moved), so `.review-verdict.json` / `.review-session.json` may remain until you delete them manually.
- `review_gate.py` emits `MAP_DEF09` stderr warning when legacy-only paths are detected

See [`.specs/oh-my-cursor.md`](../.specs/oh-my-cursor.md) and [security.md](security.md).
