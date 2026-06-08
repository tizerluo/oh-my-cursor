#!/usr/bin/env python3
"""DEF-09: migrate MAP review state from legacy paths to canonical .review/ layout."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEGACY_SESSION_DIR = ".review-session"
LEGACY_SESSION_FILE = ".review-session.json"
LEGACY_VERDICT = ".review-verdict.json"
REVIEW_DIR = ".review"
CANONICAL_SESSION = ".review/session"
CANONICAL_SUMMARY = ".review/session-summary.json"
CANONICAL_VERDICT = ".review/verdict.json"
MANIFEST_NAME = ".review/migrate-manifest.json"


class MigrateError(Exception):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _session_active(git_root: Path, force: bool) -> None:
    config_path = git_root / REVIEW_DIR / "config.json"
    if not config_path.is_file():
        return
    config = _load_json(config_path)
    if config.get("active") and not force:
        raise MigrateError(
            f"{config_path} has active=true; stop MAP session or pass --force"
        )


def _plan_actions(git_root: Path, destructive: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    legacy_session = git_root / LEGACY_SESSION_DIR
    canonical_session = git_root / CANONICAL_SESSION
    if legacy_session.is_dir() and any(legacy_session.rglob("*.json")):
        if canonical_session.exists():
            raise MigrateError(f"canonical session dir already exists: {canonical_session}")
        actions.append(
            {
                "kind": "session_dir",
                "source": str(legacy_session),
                "dest": str(canonical_session),
                "mode": "move" if destructive else "copy",
            }
        )
    legacy_verdict = git_root / LEGACY_VERDICT
    canonical_verdict = git_root / CANONICAL_VERDICT
    if legacy_verdict.is_file():
        if canonical_verdict.exists():
            raise MigrateError(f"canonical verdict already exists: {canonical_verdict}")
        actions.append(
            {
                "kind": "verdict",
                "source": str(legacy_verdict),
                "dest": str(canonical_verdict),
                "mode": "copy",
            }
        )
    legacy_summary = git_root / LEGACY_SESSION_FILE
    canonical_summary = git_root / CANONICAL_SUMMARY
    if legacy_summary.is_file():
        if canonical_summary.exists():
            raise MigrateError(f"canonical session summary already exists: {canonical_summary}")
        actions.append(
            {
                "kind": "session_summary",
                "source": str(legacy_summary),
                "dest": str(canonical_summary),
                "mode": "copy",
            }
        )
    return actions


def _apply_actions(actions: list[dict[str, Any]]) -> None:
    for action in actions:
        src = Path(action["source"])
        dest = Path(action["dest"])
        mode = action["mode"]
        if action["kind"] == "session_dir":
            dest.parent.mkdir(parents=True, exist_ok=True)
            if mode == "move":
                shutil.move(str(src), str(dest))
            else:
                shutil.copytree(src, dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def _rollback(manifest_path: Path) -> None:
    if not manifest_path.is_file():
        raise MigrateError(f"manifest not found: {manifest_path}")
    manifest = _load_json(manifest_path)
    for action in reversed(manifest.get("actions", [])):
        src = Path(action["source"])
        dest = Path(action["dest"])
        if not dest.exists():
            continue
        if action["kind"] == "session_dir" and action.get("mode") == "move":
            if src.exists():
                raise MigrateError(f"rollback blocked: legacy session restored path exists: {src}")
            shutil.move(str(dest), str(src))
        else:
            dest.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)


def migrate(
    git_root: Path,
    *,
    apply: bool,
    destructive: bool,
    force: bool,
    confirm: str | None,
) -> dict[str, Any]:
    git_root = git_root.resolve()
    if not git_root.is_dir():
        raise MigrateError(f"not a directory: {git_root}")
    if destructive and confirm != "MOVE":
        raise MigrateError("destructive session move requires --confirm MOVE")
    _session_active(git_root, force)
    actions = _plan_actions(git_root, destructive)
    manifest = {
        "schema_version": 1,
        "repo": str(git_root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "destructive": destructive,
        "actions": actions,
    }
    if not actions:
        manifest["note"] = "nothing to migrate"
        return manifest
    if apply:
        _apply_actions(actions)
        _write_json(git_root / MANIFEST_NAME, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate MAP state to canonical .review/ paths")
    parser.add_argument("repo", type=Path, help="Git repository root")
    parser.add_argument("--apply", action="store_true", help="Execute migration (default dry-run)")
    parser.add_argument(
        "--destructive",
        action="store_true",
        help="Move session dir instead of copy (requires --confirm MOVE)",
    )
    parser.add_argument("--force", action="store_true", help="Allow migrate while .review/config active")
    parser.add_argument(
        "--confirm",
        help="Typed confirmation for destructive mode (use MOVE)",
    )
    parser.add_argument("--rollback", action="store_true", help="Rollback last --apply using migrate manifest")
    args = parser.parse_args(argv)
    try:
        if args.rollback:
            _rollback(args.repo.resolve() / MANIFEST_NAME)
            print(f"Rollback complete for {args.repo.resolve()}")
            return 0
        manifest = migrate(
            args.repo,
            apply=args.apply,
            destructive=args.destructive,
            force=args.force,
            confirm=args.confirm,
        )
        print(json.dumps(manifest, indent=2))
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to migrate.", file=sys.stderr)
        return 0
    except MigrateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
