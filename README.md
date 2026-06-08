# oh-my-cursor

Independent, versioned **MAP (Multi-Agent PR)** engine for Cursor.

[![CI](https://github.com/tizerluo/oh-my-cursor/actions/workflows/ci.yml/badge.svg)](https://github.com/tizerluo/oh-my-cursor/actions/workflows/ci.yml)

## Status

- **Version:** 1.0.1 (see [CHANGELOG.md](CHANGELOG.md))
- **Spec:** [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md) (accepted via map-hyperplan)
- **Phases 1–5:** Extract complete — global cutover + v1.0.0 tag
- **Live Spike:** Cursor 3.7.19+ — [docs/spike-verification.md](docs/spike-verification.md)

## Quick start (development)

```bash
chmod +x hooks/run_tests.sh
./hooks/run_tests.sh
```

## Install (Phase 2)

```bash
chmod +x install.sh
./install.sh --copy              # production default → ~/.cursor
./install.sh --link --i-know-symlink-risk
./install.sh --project /path/to/repo
python3 scripts/install.py --doctor
```

Details: [docs/install.md](docs/install.md) · [docs/architecture.md](docs/architecture.md) · [docs/state-migration.md](docs/state-migration.md) · [docs/integrations/issue-to-pr.md](docs/integrations/issue-to-pr.md)

## Development

```bash
./hooks/run_tests.sh
./scripts/ci_check_stale_paths.sh
```

Set `OMC_ROOT` to this repo when running CLI examples in skills:

```bash
export OMC_ROOT="$(pwd)"
```

## License

MIT — see [LICENSE](LICENSE).
