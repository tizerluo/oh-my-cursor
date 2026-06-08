# oh-my-cursor

Independent, versioned **MAP (Multi-Agent PR)** engine for Cursor.

[![CI](https://github.com/tizerluo/oh-my-cursor/actions/workflows/ci.yml/badge.svg)](https://github.com/tizerluo/oh-my-cursor/actions/workflows/ci.yml)

## Status

- **Version:** 1.0.0 (see [CHANGELOG.md](CHANGELOG.md))
- **Spec:** [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md) (accepted via map-hyperplan)
- **Phases 1–4:** Core, installer, secret, DEF-09, CI/docs — **81/81 tests pass**
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

Details: [docs/install.md](docs/install.md) · [docs/architecture.md](docs/architecture.md) · [docs/state-migration.md](docs/state-migration.md)

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
