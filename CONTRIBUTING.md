# Contributing to oh-my-cursor

Thank you for helping improve the MAP engine for Cursor.

## Getting started

```bash
git clone https://github.com/tizerluo/oh-my-cursor.git
cd oh-my-cursor
./hooks/run_tests.sh
```

No external pip dependencies — tests and tooling use Python 3 stdlib only.

## Existing clones (after public release)

Before the repository went public (2026-06-09), maintainers ran a one-time `git filter-repo` history rewrite:

- Normalized commit author email to `tizerluo@gmail.com`
- Removed `.review/` paths from all historical commits

**If your local clone predates that rewrite**, `git pull` will not fast-forward. Either:

```bash
git fetch origin
git reset --hard origin/main
```

or delete the directory and `git clone` again. **Forks** created from pre-rewrite history should be deleted and re-forked from current `main`.

Release tags (`v1.0.0`, `v1.0.2`, `v1.1`) were force-updated to point at the rewritten commits; same content, new SHAs.

## Install for local development

```bash
chmod +x install.sh
./install.sh --copy              # recommended for production-like installs
python3 scripts/install.py --doctor --security
```

Prefer `--copy` over `--link`. Symlink installs (`--link --i-know-symlink-risk`) tie hook execution to your clone path and are intended for active engine development only.

## MAP PR workflow

Feature and fix PRs that touch merge-gate behavior should follow the MAP multi-agent PR process:

1. Write or update a spec under `.specs/` when the change is non-trivial.
2. Run `./hooks/run_tests.sh` and `./scripts/ci_check_stale_paths.sh` before opening a PR.
3. Use the [multi-agent-pr skill](skills/multi-agent-pr/SKILL.md) for Commander orchestration, architect review, coder implementation, and parallel reviewer gates when appropriate.

Changes to `hooks/review_gate.py` or `scripts/install.py` require maintainer review (see `.github/CODEOWNERS`).

## Pull requests

- Keep diffs focused; avoid unrelated refactors.
- Do not commit `.review/` session artifacts from this engine repo (consumer repos use `.review/` at runtime).
- Ensure CI passes: tests, compile check, install smoke, stale-path grep.

## Questions

Open a [GitHub Discussion](https://github.com/tizerluo/oh-my-cursor/discussions) or issue for design questions. Security reports: see [SECURITY.md](SECURITY.md).
