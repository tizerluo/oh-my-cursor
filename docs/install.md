# Install oh-my-cursor MAP

Install MAP hooks, skills, and agents into Cursor configuration.

## Requirements

- Python 3 (hook **runtime** is stdlib only — no pip)
- Cursor with hooks support (3.7.19+ verified for subagentStop; 3.15.x role lifecycle verified — see [spike-verification.md](spike-verification.md))

## Cloud agents and hook scope

Cursor **cloud agents** run in VMs that do **not** load user-level `~/.cursor/hooks.json` (home directory hooks are unavailable there). Only **project**, **team**, or **enterprise** hooks apply.

If you need MAP merge-gate / permission enforcement for cloud-agent sessions:

```bash
./install.sh --project /path/to/consumer-repo
```

That installs into `consumer-repo/.cursor/` (copy mode). Global `./install.sh --copy` (→ `~/.cursor`) is fine for local Desktop Cursor, but does **not** protect cloud runs.

## Pin a release

For production installs, clone and check out a [release tag](https://github.com/tizerluo/oh-my-cursor/releases) rather than tracking `main`:

```bash
git clone https://github.com/tizerluo/oh-my-cursor.git
cd oh-my-cursor
git checkout v1.1   # or latest tag
```

## Global install

**Production (copy — recommended):**

```bash
cd /path/to/oh-my-cursor
chmod +x install.sh
./install.sh --copy
```

**Development (symlink — supply-chain risk):**

```bash
./install.sh --link --i-know-symlink-risk
```

`--link` symlinks `review_gate.py` to your clone. If the clone moves, is deleted, or is replaced by an untrusted checkout, hooks may fail or execute unexpected code. Use only for active engine development on a trusted machine.

## Project install

Install into `my-repo/.cursor/` and append secret path to `.gitignore`:

```bash
./install.sh --project /path/to/my-repo
```

Uses copy mode internally (`--project` + `--link` is rejected).

## Verify

```bash
python3 scripts/install.py --doctor
python3 scripts/install.py --doctor --security   # Phase 2b: secret + hash checks
```

## Uninstall hooks merge only

Restores `hooks.json` from the latest `hooks.json.bak.<timestamp>`:

```bash
python3 scripts/install.py --uninstall
```

Copied/symlinked assets under `~/.cursor/` are not removed automatically.

## hooks.json merge behavior

Install merges **MAP entries only** — commands that invoke `review_gate.py` from `hooks/hooks.json.template`. All other hook entries in your existing `hooks.json` are preserved unchanged.

- Parses existing `hooks.json`; **preserves all non-MAP entries** (e.g. `rtk hook cursor`)
- Replaces MAP entries (commands containing `review_gate.py`) from `hooks/hooks.json.template`
- Backs up before write: `hooks.json.bak.<UTC timestamp>`
- Aborts if merge would alter non-MAP entries

## Install manifest

Writes `.cursor/omc-install.json` with `omc_root`, `mode`, `review_gate_path`, `secret_path`, `review_gate_sha256`, `installed_at`.

See [security.md](security.md) and [`.specs/oh-my-cursor.md`](../.specs/oh-my-cursor.md).
