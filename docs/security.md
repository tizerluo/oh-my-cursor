# Security — Secret trust contract

MAP uses HMAC-sealed subagent markers. The secret file is the root of trust for merge-gate integrity.

## Paths

| Symbol | Location |
|--------|----------|
| `HOOKS_DIR` | Directory containing `review_gate.py` |
| `secret_file_path()` | `$OMC_SECRET_FILE` env, else `HOOKS_DIR/.review-gate-secret` |
| Legacy | `~/.cursor/hooks/.review-gate-secret` — copied once if target missing (global install only; project-only installs should rely on `OMC_SECRET_FILE` / target `hooks/.review-gate-secret`) |

## Rules (Phase 2b)

1. **Fail-closed** — `_secret()` never returns an ephemeral random key; errors exit with code 2.
2. **Legacy migration** — if legacy exists and target does not, **copy** with mode `0600` (never move).
3. **No symlinks** — secret path must be a regular file owned by the current user, mode `0600`.
4. **Creation** — `O_CREAT|O_EXCL`; parent directory `0700` when possible.
5. **Mixed installs** — `omc doctor --security` fails if multiple secret files disagree.

## Install

```bash
./install.sh --copy
python3 scripts/install.py --doctor --security
```

Install runs `bootstrap_secret()` at the target secret path and records `secret_path` + `review_gate_sha256` in `omc-install.json`.

## Threat model

- **Trusted:** local user running Cursor and install scripts.
- **Out of scope:** multi-user shared `$HOME`, remote attackers without shell access.

## Verification

```bash
./hooks/run_tests.sh   # includes tests/test_security.py (6 tests)
```

See [`.specs/oh-my-cursor.md`](../.specs/oh-my-cursor.md) § Secret trust contract.
