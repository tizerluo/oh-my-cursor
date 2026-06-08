#!/usr/bin/env python3
"""Install oh-my-cursor MAP assets into ~/.cursor or a project .cursor/ directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OMC_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = OMC_ROOT / "hooks" / "hooks.json.template"
INSTALL_MANIFEST = "omc-install.json"
GITIGNORE_SECRET_LINE = ".cursor/hooks/.review-gate-secret"

MAP_SKILL_DIRS = ("multi-agent-pr", "map-hyperplan", "map-security", "map-refactor")
MAP_SKILL_FILES = ("MAP_SKILL_DISCOVERY.md",)
HOOKS_COPY_NAMES = ("review_gate.py", "run_tests.sh")
HOOKS_COPY_DIRS = ("schemas", "spikes")

REVIEW_GATE_MARKER = "review_gate.py"


class InstallError(Exception):
    pass


def is_map_hook_entry(entry: dict[str, Any]) -> bool:
    command = entry.get("command")
    return isinstance(command, str) and REVIEW_GATE_MARKER in command


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstallError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InstallError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError(f"Expected JSON object in {path}")
    return data


def render_map_hooks(review_gate_path: Path) -> dict[str, Any]:
    if not TEMPLATE_PATH.is_file():
        raise InstallError(f"Missing template: {TEMPLATE_PATH}")
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered_text = raw.replace("{{REVIEW_GATE_PATH}}", str(review_gate_path))
    try:
        data = json.loads(rendered_text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"Rendered hooks template is invalid JSON: {exc}") from exc
    return data


def merge_hooks(existing: dict[str, Any], map_hooks: dict[str, Any]) -> dict[str, Any]:
    """Merge MAP hook entries; preserve all non-MAP entries unchanged."""
    merged = json.loads(json.dumps(existing))
    existing_hooks = merged.get("hooks")
    if existing_hooks is None:
        existing_hooks = {}
        merged["hooks"] = existing_hooks
    if not isinstance(existing_hooks, dict):
        raise InstallError("hooks.json: hooks must be an object")

    map_hook_events = map_hooks.get("hooks", {})
    if not isinstance(map_hook_events, dict):
        raise InstallError("Rendered MAP hooks: hooks must be an object")

    for event, entries in existing_hooks.items():
        if event in map_hook_events:
            if not isinstance(entries, list):
                raise InstallError(f"hooks.json: hooks.{event} must be an array")
            non_map = [e for e in entries if not is_map_hook_entry(e)]
            map_entries = map_hook_events[event]
            if not isinstance(map_entries, list):
                raise InstallError(f"MAP template: hooks.{event} must be an array")
            existing_hooks[event] = non_map + map_entries

    for event, map_entries in map_hook_events.items():
        if event not in existing_hooks:
            if not isinstance(map_entries, list):
                raise InstallError(f"MAP template: hooks.{event} must be an array")
            existing_hooks[event] = list(map_entries)

    merged.setdefault("version", map_hooks.get("version", 1))
    return merged


def validate_non_map_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    before_hooks = before.get("hooks", {})
    after_hooks = after.get("hooks", {})
    if not isinstance(before_hooks, dict) or not isinstance(after_hooks, dict):
        raise InstallError("hooks.json structure invalid during merge validation")

    for event, entries in before_hooks.items():
        if not isinstance(entries, list):
            continue
        non_map_before = [e for e in entries if not is_map_hook_entry(e)]
        after_entries = after_hooks.get(event, [])
        if not isinstance(after_entries, list):
            raise InstallError(f"Merge corrupted hooks.{event}")
        non_map_after = [e for e in after_entries if not is_map_hook_entry(e)]
        if non_map_before != non_map_after:
            raise InstallError(
                f"Merge would modify non-MAP hook entries for event {event!r}; aborting."
            )


def backup_hooks_json(hooks_json: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = hooks_json.with_name(f"{hooks_json.name}.bak.{ts}")
    shutil.copy2(hooks_json, backup)
    return backup


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def resolve_target_cursor(target: Path | None, project: Path | None) -> Path:
    if project is not None:
        project = project.expanduser().resolve()
        if not project.is_dir():
            raise InstallError(f"Project path is not a directory: {project}")
        return project / ".cursor"
    if target is not None:
        return target.expanduser().resolve()
    return Path.home() / ".cursor"


def review_gate_path_for(cursor_dir: Path, mode: str) -> Path:
    if mode == "link":
        return (OMC_ROOT / "hooks" / "review_gate.py").resolve()
    return (cursor_dir / "hooks" / "review_gate.py").resolve()


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _install_link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return
    if dst.exists() or dst.is_symlink():
        _remove_path(dst)
    dst.symlink_to(src)


def _install_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _install_copy_tree(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        _remove_path(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("*.log", ".review-gate-secret", "__pycache__"),
    )


def install_hooks_assets(cursor_dir: Path, mode: str) -> None:
    hooks_dst = cursor_dir / "hooks"
    hooks_src = OMC_ROOT / "hooks"

    for name in HOOKS_COPY_NAMES:
        src = hooks_src / name
        dst = hooks_dst / name
        if not src.is_file():
            continue
        if mode == "link":
            _install_link(src.resolve(), dst)
        else:
            _install_copy_file(src, dst)

    for dirname in HOOKS_COPY_DIRS:
        src = hooks_src / dirname
        dst = hooks_dst / dirname
        if not src.is_dir():
            continue
        if mode == "link":
            _install_link(src.resolve(), dst)
        else:
            _install_copy_tree(src, dst)


def install_skills(cursor_dir: Path, mode: str) -> None:
    skills_dst_root = cursor_dir / "skills"
    skills_src_root = OMC_ROOT / "skills"
    for dirname in MAP_SKILL_DIRS:
        src = skills_src_root / dirname
        dst = skills_dst_root / dirname
        if not src.is_dir():
            raise InstallError(f"Missing skill directory: {src}")
        if mode == "link":
            _install_link(src.resolve(), dst)
        else:
            _install_copy_tree(src, dst)
    for filename in MAP_SKILL_FILES:
        src = skills_src_root / filename
        dst = skills_dst_root / filename
        if not src.is_file():
            continue
        if mode == "link":
            _install_link(src.resolve(), dst)
        else:
            _install_copy_file(src, dst)


def install_agents(cursor_dir: Path, mode: str) -> None:
    src = OMC_ROOT / "agents" / "planner.md"
    dst = cursor_dir / "agents" / "planner.md"
    if not src.is_file():
        return
    if mode == "link":
        _install_link(src.resolve(), dst)
    else:
        _install_copy_file(src, dst)


def ensure_project_gitignore(project_root: Path) -> None:
    gitignore = project_root / ".gitignore"
    line = GITIGNORE_SECRET_LINE
    if gitignore.is_file():
        content = gitignore.read_text(encoding="utf-8")
        if line in content.splitlines():
            return
        with gitignore.open("a", encoding="utf-8") as fh:
            if content and not content.endswith("\n"):
                fh.write("\n")
            fh.write(f"{line}\n")
    else:
        gitignore.write_text(f"{line}\n", encoding="utf-8")


def write_install_manifest(cursor_dir: Path, mode: str, review_gate_path: Path) -> None:
    manifest = {
        "schema_version": 1,
        "omc_root": str(OMC_ROOT),
        "mode": mode,
        "review_gate_path": str(review_gate_path),
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(cursor_dir / INSTALL_MANIFEST, manifest)


def merge_hooks_json(cursor_dir: Path, mode: str) -> tuple[Path, Path]:
    hooks_json = cursor_dir / "hooks.json"
    map_hooks = render_map_hooks(review_gate_path_for(cursor_dir, mode))

    if hooks_json.is_file():
        existing = load_json(hooks_json)
        merged = merge_hooks(existing, map_hooks)
        validate_non_map_preserved(existing, merged)
        backup = backup_hooks_json(hooks_json)
        atomic_write_json(hooks_json, merged)
        return backup, hooks_json

    atomic_write_json(hooks_json, map_hooks)
    return hooks_json, hooks_json


def find_latest_backup(cursor_dir: Path) -> Path | None:
    pattern = re.compile(r"^hooks\.json\.bak\.(\d{14})$")
    backups: list[tuple[str, Path]] = []
    for path in cursor_dir.glob("hooks.json.bak.*"):
        match = pattern.match(path.name)
        if match:
            backups.append((match.group(1), path))
    if not backups:
        return None
    backups.sort(key=lambda item: item[0], reverse=True)
    return backups[0][1]


def uninstall_hooks_json(cursor_dir: Path) -> Path:
    hooks_json = cursor_dir / "hooks.json"
    backup = find_latest_backup(cursor_dir)
    if backup is None:
        raise InstallError(f"No hooks.json backup found in {cursor_dir}")
    shutil.copy2(backup, hooks_json)
    return backup


def run_doctor(cursor_dir: Path | None = None) -> int:
    """Minimal Phase 2 doctor; full --security checks land in Phase 2b."""
    issues: list[str] = []
    ok: list[str] = []

    if shutil.which("python3") is None:
        issues.append("python3 not found on PATH")
    else:
        ok.append("python3 available")

    cursor = cursor_dir or (Path.home() / ".cursor")
    manifest_path = cursor / INSTALL_MANIFEST
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        gate = Path(str(manifest.get("review_gate_path", "")))
        ok.append(f"install manifest: {manifest.get('mode')} @ {manifest.get('installed_at', '?')}")
    else:
        gate = cursor / "hooks" / "review_gate.py"
        issues.append(f"no {INSTALL_MANIFEST} (install may not have run)")

    if gate.is_file():
        ok.append(f"review_gate.py: {gate}")
    else:
        issues.append(f"review_gate.py missing: {gate}")

    hooks_json = cursor / "hooks.json"
    if hooks_json.is_file():
        data = load_json(hooks_json)
        map_count = sum(
            1
            for entries in data.get("hooks", {}).values()
            if isinstance(entries, list)
            for entry in entries
            if is_map_hook_entry(entry)
        )
        if map_count >= 8:
            ok.append(f"hooks.json: {map_count} MAP entries")
        else:
            issues.append(f"hooks.json: expected >=8 MAP entries, found {map_count}")
        if gate.is_file():
            mismatched = [
                e.get("command")
                for entries in data.get("hooks", {}).values()
                if isinstance(entries, list)
                for e in entries
                if is_map_hook_entry(e) and str(gate) not in str(e.get("command", ""))
            ]
            if mismatched:
                issues.append(f"hooks.json MAP commands do not reference {gate}")
            else:
                ok.append("hooks.json MAP commands reference expected review_gate path")
    else:
        issues.append(f"missing {hooks_json}")

    tests_script = OMC_ROOT / "hooks" / "run_tests.sh"
    if tests_script.is_file():
        ok.append(f"tests: run {tests_script} (from omc repo)")

    print("omc doctor (Phase 2)")
    for item in ok:
        print(f"  OK  {item}")
    for item in issues:
        print(f"  FAIL {item}")
    print()
    if issues:
        print("Fix issues above, then reload Cursor.")
        return 1
    print("All checks passed. Reload Cursor to pick up hooks.json changes.")
    return 0


def install(
    mode: str,
    *,
    target: Path | None = None,
    project: Path | None = None,
    symlink_ack: bool = False,
) -> None:
    if mode == "link" and not symlink_ack:
        raise InstallError("Refusing --link without --i-know-symlink-risk")

    cursor_dir = resolve_target_cursor(target, project)
    cursor_dir.mkdir(parents=True, exist_ok=True)

    install_hooks_assets(cursor_dir, mode)
    install_skills(cursor_dir, mode)
    install_agents(cursor_dir, mode)

    if project is not None:
        ensure_project_gitignore(project)

    gate_path = review_gate_path_for(cursor_dir, mode)
    backup, hooks_json = merge_hooks_json(cursor_dir, mode)
    write_install_manifest(cursor_dir, mode, gate_path)

    print(f"oh-my-cursor install ({mode})")
    print(f"  OMC_ROOT:          {OMC_ROOT}")
    print(f"  Target .cursor:    {cursor_dir}")
    print(f"  review_gate.py:    {gate_path}")
    print(f"  hooks.json:        {hooks_json}")
    if backup != hooks_json:
        print(f"  hooks.json backup: {backup}")
    if project is not None:
        print(f"  .gitignore:        {project / '.gitignore'} (+ secret line)")
    print()
    run_doctor(cursor_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install oh-my-cursor MAP into Cursor config")
    parser.add_argument("--copy", action="store_true", help="Copy assets (production default)")
    parser.add_argument("--link", action="store_true", help="Symlink assets (dev only)")
    parser.add_argument(
        "--i-know-symlink-risk",
        action="store_true",
        help="Required with --link",
    )
    parser.add_argument("--project", type=Path, help="Install into PROJECT/.cursor/")
    parser.add_argument(
        "--target",
        type=Path,
        help="Cursor config directory (default: ~/.cursor)",
    )
    parser.add_argument("--uninstall", action="store_true", help="Restore hooks.json from latest backup")
    parser.add_argument("--doctor", action="store_true", help="Run install verification checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.doctor:
            cursor = resolve_target_cursor(args.target, args.project if args.project else None)
            return run_doctor(cursor if args.target or args.project else None)

        if args.uninstall:
            cursor_dir = resolve_target_cursor(args.target, args.project)
            backup = uninstall_hooks_json(cursor_dir)
            print(f"Restored {cursor_dir / 'hooks.json'} from {backup}")
            return 0

        if args.copy and args.link:
            raise InstallError("Use only one of --copy or --link")

        if args.link and args.project:
            raise InstallError("Use --project with --copy (not --link)")

        if args.link:
            mode = "link"
        elif args.copy or args.project:
            mode = "copy"
        else:
            mode = "copy"

        project = args.project.expanduser().resolve() if args.project else None

        install(
            mode,
            target=args.target,
            project=project,
            symlink_ack=args.i_know_symlink_risk,
        )
        return 0
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
