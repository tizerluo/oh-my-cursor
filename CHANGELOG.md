# Changelog

All notable changes to **oh-my-cursor** are documented here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-06-08

### Added

- Independent MAP engine extracted from personal `~/.cursor` ([`review_gate.py`](hooks/review_gate.py), skills, schemas, tests)
- [`install.sh`](install.sh) / [`scripts/install.py`](scripts/install.py): `--copy` (default), `--link --i-know-symlink-risk`, `--project`
- Non-destructive `hooks.json` MAP allowlist merge; `omc-install.json` manifest
- **Secret trust contract** (Phase 2b): fail-closed `_secret()`, legacy secret copy, symlink/mode checks
- `omc doctor` and `omc doctor --security`
- **DEF-09** (Phase 3): canonical `.review/` write paths, legacy dual-read, [`migrate_map_state.py`](scripts/migrate_map_state.py)
- [`docs/install.md`](docs/install.md), [`docs/security.md`](docs/security.md), [`docs/state-migration.md`](docs/state-migration.md), [`docs/architecture.md`](docs/architecture.md)
- GitHub Actions CI: 81 tests, install smoke, stale-path grep
- map-hyperplan accepted spec at [`.specs/oh-my-cursor.md`](.specs/oh-my-cursor.md)

### Changed

- Session markers write to `.review/session/<branch>/<sha>/` (read fallback: `.review-session/`)
- Verdict write to `.review/verdict.json` (read fallback: `.review-verdict.json`)
- Skills use `$OMC_ROOT` for CLI examples (set to your oh-my-cursor clone path)

### Compatibility

- **Cursor 3.7.19+** verified for subagentStop marker path (Live Spike)
- Legacy review paths honored until **v2.0.0** (target 2027-Q1)

### Tests

- 81 unit/integration tests via [`hooks/run_tests.sh`](hooks/run_tests.sh)

## [Unreleased]

### Planned

- Phase 5: global `~/.cursor` cutover, v1.0.0 git tag, consumer integration docs
