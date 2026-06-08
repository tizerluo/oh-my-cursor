# oh-my-cursor

Independent, versioned **MAP (Multi-Agent PR)** engine for Cursor.

## Status

- **Spec:** [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md) (accepted via map-hyperplan)
- **Phase 1:** Core assets migrated from `~/.cursor`; **58/58 tests pass**
- **Implementation plan:** see repo maintainer's Cursor plan `oh-my-cursor_map_extract`

## Quick start (development)

```bash
chmod +x hooks/run_tests.sh
./hooks/run_tests.sh
```

## Install (Phase 2 — not yet implemented)

```bash
./install.sh --copy    # production default
./install.sh --link --i-know-symlink-risk
./install.sh --project /path/to/repo
```

See [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md) for full v1.0 scope.

## License

MIT — see [LICENSE](LICENSE).
