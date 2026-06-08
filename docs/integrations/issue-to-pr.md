# issue-to-pr integration

Install MAP into the **issue-to-pr** consumer repo for project-local hooks and skills.

## Install

From oh-my-cursor:

```bash
export OMC_ROOT=/path/to/oh-my-cursor
cd "$OMC_ROOT"
./install.sh --project /path/to/issue-to-pr
```

This writes:

| Path | Purpose |
|------|---------|
| `issue-to-pr/.cursor/hooks/review_gate.py` | Merge gate + hook handlers (copy) |
| `issue-to-pr/.cursor/hooks.json` | MAP hook entries (merged if file existed) |
| `issue-to-pr/.cursor/skills/multi-agent-pr/` | Commander workflow skill |
| `issue-to-pr/.cursor/skills/map-*` | Planning / security / refactor skills |
| `issue-to-pr/.cursor/omc-install.json` | Install manifest |
| `issue-to-pr/.gitignore` | Adds `.cursor/hooks/.review-gate-secret` |

Global `~/.cursor` hooks still apply for Cursor-wide behavior; project install gives a **self-contained** copy for CI clones and teammates who run `--project` after clone.

## Refresh after omc updates

```bash
cd "$OMC_ROOT" && ./hooks/run_tests.sh
./install.sh --project /path/to/issue-to-pr
```

## Review state (DEF-09)

Markers and verdicts live in the **git repo root**, not under `.cursor/`:

- `.review/session/<branch>/<sha>/`
- `.review/verdict.json`

See [state-migration.md](../state-migration.md).

## Verify

```bash
python3 "$OMC_ROOT/scripts/install.py" --doctor --target /path/to/issue-to-pr/.cursor
```

Reload Cursor when opening the consumer repo after install.
