# Install oh-my-cursor MAP

Install MAP hooks, skills, and agents into Cursor configuration.

## Requirements

- Python 3
- Cursor with hooks support (3.7.19+ verified for subagentStop)

## Global install

**Production (copy — recommended):**

```bash
cd /path/to/oh-my-cursor
chmod +x install.sh
./install.sh --copy
```

**Development (symlink — requires explicit ack):**

```bash
./install.sh --link --i-know-symlink-risk
```

## Project install

Install into `my-repo/.cursor/` and append secret path to `.gitignore`:

```bash
./install.sh --project /path/to/my-repo
```

Uses copy mode internally (`--project` + `--link` is rejected).

## Verify

```bash
python3 scripts/install.py --doctor
# or after install to a custom target:
python3 scripts/install.py --doctor --target /path/to/.cursor
```

## Uninstall hooks merge only

Restores `hooks.json` from the latest `hooks.json.bak.<timestamp>`:

```bash
python3 scripts/install.py --uninstall
```

Copied/symlinked assets under `~/.cursor/` are not removed automatically.

## hooks.json merge behavior

- Parses existing `hooks.json`; **preserves all non-MAP entries** (e.g. `rtk hook cursor`)
- Replaces MAP entries (commands containing `review_gate.py`) from `hooks/hooks.json.template`
- Backs up before write: `hooks.json.bak.<UTC timestamp>`
- Aborts if merge would alter non-MAP entries

## Install manifest

Writes `.cursor/omc-install.json` with `omc_root`, `mode`, `review_gate_path`, `installed_at`.

See [`.specs/oh-my-cursor.md`](../.specs/oh-my-cursor.md) for Secret trust contract (Phase 2b).
