# oh-my-cursor

Independent, versioned **MAP (Multi-Agent PR)** engine for Cursor.

## Status

- **Spec:** [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md) (accepted via map-hyperplan)
- **Phase 1–3:** Core + installer + secret + DEF-09; **81/81 tests pass**
- **Implementation plan:** see repo maintainer's Cursor plan `oh-my-cursor_map_extract`

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

Details: [docs/install.md](docs/install.md) · [docs/state-migration.md](docs/state-migration.md)

## License

MIT — see [LICENSE](LICENSE).
