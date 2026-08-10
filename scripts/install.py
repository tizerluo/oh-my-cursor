#!/usr/bin/env python3
"""Install oh-my-cursor MAP assets into ~/.cursor or a project .cursor/ directory."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
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
GITIGNORE_MAP_MARKER = "# MAP review state (oh-my-cursor)"
GITIGNORE_MAP_LINES = (
    GITIGNORE_MAP_MARKER,
    ".review/",
    ".review-session/",
    ".review-verdict.json",
    ".review-session.json",
)
INSTALL_REMINDER = "Reminder: do not commit .review/ in consumer repos."

MAP_SKILL_DIRS = ("multi-agent-pr", "map-hyperplan", "map-security", "map-refactor")
MAP_SKILL_FILES = ("MAP_SKILL_DISCOVERY.md",)
HOOKS_COPY_NAMES = ("review_gate.py", "run_tests.sh")
HOOKS_COPY_DIRS = ("schemas", "spikes", "config")

REVIEW_GATE_MARKER = "review_gate.py"

REVIEWER_MODEL_FALLBACKS: dict[str, str] = {
    "reviewer-grok": "grok-4.5",
    "reviewer-codex": "gpt-5.3-codex-high-fast",
    "reviewer-gemini": "gemini-3.1-pro",
}


def _check_models_config(gate: Path) -> tuple[list[str], list[str]]:
    """Verify hooks/config/models.json exists and reviewer models are valid."""
    ok: list[str] = []
    issues: list[str] = []
    models_path = gate.parent / "config" / "models.json"
    if not models_path.is_file():
        issues.append(f"models.json missing: {models_path}")
        return ok, issues
    try:
        data = load_json(models_path)
    except InstallError as exc:
        issues.append(f"models.json invalid: {exc}")
        return ok, issues
    reviewers = data.get("reviewers")
    if not isinstance(reviewers, dict):
        issues.append("models.json: reviewers section missing or not an object")
        return ok, issues
    for role, fallback in REVIEWER_MODEL_FALLBACKS.items():
        info = reviewers.get(role)
        if not isinstance(info, dict):
            issues.append(f"models.json: missing reviewer key {role!r}")
            continue
        model = str(info.get("model") or "")
        if not model:
            issues.append(f"models.json: reviewer {role!r} has empty model")
            continue
        if model == fallback:
            ok.append(f"models.json: {role} -> {model} (fallback default)")
        else:
            ok.append(f"models.json: {role} -> {model} (custom)")
    return ok, issues



class InstallError(Exception):
    pass


def is_map_hook_entry(
    entry: dict[str, Any],
    review_gate_path: Path | str | None = None,
) -> bool:
    if entry.get("omc") is True:
        return True
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return False
    if review_gate_path is not None:
        gate = str(review_gate_path)
        if gate in command:
            return True
    return command.strip().startswith("python3 ") and REVIEW_GATE_MARKER in command


def expected_map_hook_count(review_gate_path: Path | None = None) -> int:
    gate = review_gate_path or (OMC_ROOT / "hooks" / "review_gate.py")
    rendered = render_map_hooks(gate)
    return sum(
        len(entries)
        for entries in rendered.get("hooks", {}).values()
        if isinstance(entries, list)
    )


def _dedupe_hook_entries(entries: list[dict[str, Any]], review_gate_path: Path) -> list[dict[str, Any]]:
    seen_map_keys: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if is_map_hook_entry(entry, review_gate_path):
            key = (str(entry.get("command", "")), str(entry.get("matcher", "")))
            if key in seen_map_keys:
                continue
            seen_map_keys.add(key)
        deduped.append(entry)
    return deduped


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
    quoted = shlex.quote(str(review_gate_path))
    rendered_text = raw.replace("{{REVIEW_GATE_QUOTED_PATH}}", quoted)
    if "{{REVIEW_GATE_PATH}}" in rendered_text:
        rendered_text = rendered_text.replace("{{REVIEW_GATE_PATH}}", str(review_gate_path))
    try:
        data = json.loads(rendered_text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"Rendered hooks template is invalid JSON: {exc}") from exc
    return data


def merge_hooks(
    existing: dict[str, Any],
    map_hooks: dict[str, Any],
    *,
    review_gate_path: Path | None = None,
) -> dict[str, Any]:
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

    gate_path = review_gate_path or (OMC_ROOT / "hooks" / "review_gate.py")

    for event, entries in existing_hooks.items():
        if event in map_hook_events:
            if not isinstance(entries, list):
                raise InstallError(f"hooks.json: hooks.{event} must be an array")
            non_map = [e for e in entries if not is_map_hook_entry(e, gate_path)]
            map_entries = map_hook_events[event]
            if not isinstance(map_entries, list):
                raise InstallError(f"MAP template: hooks.{event} must be an array")
            existing_hooks[event] = _dedupe_hook_entries(non_map + map_entries, gate_path)

    for event, map_entries in map_hook_events.items():
        if event not in existing_hooks:
            if not isinstance(map_entries, list):
                raise InstallError(f"MAP template: hooks.{event} must be an array")
            existing_hooks[event] = list(map_entries)

    merged.setdefault("version", map_hooks.get("version", 1))
    return merged


def validate_non_map_preserved(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    review_gate_path: Path | None = None,
) -> None:
    before_hooks = before.get("hooks", {})
    after_hooks = after.get("hooks", {})
    if not isinstance(before_hooks, dict) or not isinstance(after_hooks, dict):
        raise InstallError("hooks.json structure invalid during merge validation")

    gate_path = review_gate_path or (OMC_ROOT / "hooks" / "review_gate.py")

    for event, entries in before_hooks.items():
        if not isinstance(entries, list):
            continue
        non_map_before = [e for e in entries if not is_map_hook_entry(e, gate_path)]
        after_entries = after_hooks.get(event, [])
        if not isinstance(after_entries, list):
            raise InstallError(f"Merge corrupted hooks.{event}")
        non_map_after = [e for e in after_entries if not is_map_hook_entry(e, gate_path)]
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


def secret_path_for_install(cursor_dir: Path, mode: str) -> Path:
    if mode == "link":
        return (OMC_ROOT / "hooks" / ".review-gate-secret").resolve()
    return (cursor_dir / "hooks" / ".review-gate-secret").resolve()


def review_gate_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_review_gate_module():
    path = OMC_ROOT / "hooks" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("omc_review_gate", path)
    if spec is None or spec.loader is None:
        raise InstallError(f"Cannot load review_gate from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_install_secret(cursor_dir: Path, mode: str) -> Path:
    rg = load_review_gate_module()
    target = secret_path_for_install(cursor_dir, mode)
    rg.bootstrap_secret(target)
    return target


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
    if gitignore.is_file():
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
    else:
        content = ""
        lines = []

    additions: list[str] = []
    if GITIGNORE_SECRET_LINE not in lines:
        additions.append(GITIGNORE_SECRET_LINE)
    if GITIGNORE_MAP_MARKER not in lines:
        additions.extend(GITIGNORE_MAP_LINES)

    if not additions:
        return

    with gitignore.open("a", encoding="utf-8") as fh:
        if content and not content.endswith("\n"):
            fh.write("\n")
        fh.write("\n".join(additions) + "\n")


def write_install_manifest(
    cursor_dir: Path,
    mode: str,
    review_gate_path: Path,
    secret_path: Path,
) -> None:
    manifest = {
        "schema_version": 1,
        "omc_root": str(OMC_ROOT),
        "mode": mode,
        "review_gate_path": str(review_gate_path),
        "secret_path": str(secret_path),
        "review_gate_sha256": review_gate_sha256(review_gate_path),
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(cursor_dir / INSTALL_MANIFEST, manifest)


def merge_hooks_json(cursor_dir: Path, mode: str) -> tuple[Path, Path]:
    hooks_json = cursor_dir / "hooks.json"
    gate_path = review_gate_path_for(cursor_dir, mode)
    map_hooks = render_map_hooks(gate_path)

    if hooks_json.is_file():
        existing = load_json(hooks_json)
        merged = merge_hooks(existing, map_hooks, review_gate_path=gate_path)
        validate_non_map_preserved(existing, merged, review_gate_path=gate_path)
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


def _check_secret_conflicts(primary_secret: Path) -> list[str]:
    issues: list[str] = []
    global_secret = (Path.home() / ".cursor" / "hooks" / ".review-gate-secret").resolve()
    primary = primary_secret.resolve()
    candidates = {p for p in (primary, global_secret) if p.is_file() and not p.is_symlink()}
    if len(candidates) <= 1:
        return issues
    contents: dict[Path, bytes] = {}
    for path in candidates:
        try:
            contents[path] = path.read_bytes()
        except OSError as exc:
            issues.append(f"cannot read secret {path}: {exc}")
    unique = {data for data in contents.values()}
    if len(unique) > 1:
        issues.append(
            "mixed install: multiple secret files with different content "
            f"({', '.join(str(p) for p in contents)})"
        )
    return issues


def run_doctor(cursor_dir: Path | None = None, *, security: bool = False) -> int:
    """Install verification; use security=True for Phase 2b secret checks."""
    issues: list[str] = []
    ok: list[str] = []

    if shutil.which("python3") is None:
        issues.append("python3 not found on PATH")
    else:
        ok.append("python3 available")

    cursor = cursor_dir or (Path.home() / ".cursor")
    manifest_path = cursor / INSTALL_MANIFEST
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        gate = Path(str(manifest.get("review_gate_path", "")))
        ok.append(f"install manifest: {manifest.get('mode')} @ {manifest.get('installed_at', '?')}")
    else:
        gate = cursor / "hooks" / "review_gate.py"
        if not security:
            issues.append(f"no {INSTALL_MANIFEST} (install may not have run)")

    if gate.is_file():
        ok.append(f"review_gate.py: {gate}")
        models_ok, models_issues = _check_models_config(gate)
        ok.extend(models_ok)
        issues.extend(models_issues)
    else:
        issues.append(f"review_gate.py missing: {gate}")

    hooks_json = cursor / "hooks.json"
    if hooks_json.is_file():
        data = load_json(hooks_json)
        expected_count = expected_map_hook_count(gate if gate.is_file() else None)
        map_count = sum(
            1
            for entries in data.get("hooks", {}).values()
            if isinstance(entries, list)
            for entry in entries
            if is_map_hook_entry(entry, gate if gate.is_file() else None)
        )
        if map_count >= expected_count:
            ok.append(f"hooks.json: {map_count} MAP entries (expected {expected_count})")
        else:
            issues.append(
                f"hooks.json: expected >={expected_count} MAP entries, found {map_count}"
            )
        if gate.is_file():
            map_entries = [
                (str(e.get("command", "")), str(e.get("matcher", "")))
                for entries in data.get("hooks", {}).values()
                if isinstance(entries, list)
                for e in entries
                if is_map_hook_entry(e, gate)
            ]
            mismatched = [cmd for cmd, _ in map_entries if str(gate) not in cmd]
            duplicates = len(map_entries) - len(set(map_entries))
            if mismatched:
                issues.append(f"hooks.json MAP commands do not reference {gate}")
            elif duplicates:
                issues.append(f"hooks.json: {duplicates} duplicate MAP command(s)")
            else:
                ok.append("hooks.json MAP commands reference expected review_gate path")
    else:
        issues.append(f"missing {hooks_json}")

    tests_script = OMC_ROOT / "hooks" / "run_tests.sh"
    if tests_script.is_file():
        ok.append(f"tests: run {tests_script} (from omc repo)")

    if security:
        if manifest.get("secret_path"):
            secret_path = Path(str(manifest["secret_path"]))
        else:
            secret_path = secret_path_for_install(cursor, str(manifest.get("mode", "copy")))
        try:
            rg = load_review_gate_module()
            rg._read_secret_bytes(secret_path)
            ok.append(f"secret trust OK: {secret_path}")
        except Exception as exc:
            issues.append(f"secret check failed: {exc}")
        if gate.is_file() and manifest.get("review_gate_sha256"):
            actual = review_gate_sha256(gate)
            expected = str(manifest["review_gate_sha256"])
            if actual == expected:
                ok.append("review_gate.py hash matches manifest")
            else:
                issues.append("review_gate.py hash mismatch vs omc-install.json")
        issues.extend(_check_secret_conflicts(secret_path))

    title = "omc doctor --security" if security else "omc doctor"
    print(title)
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
    secret_path = ensure_install_secret(cursor_dir, mode)
    backup, hooks_json = merge_hooks_json(cursor_dir, mode)
    write_install_manifest(cursor_dir, mode, gate_path, secret_path)

    print(f"oh-my-cursor install ({mode})")
    print(f"  OMC_ROOT:          {OMC_ROOT}")
    print(f"  Target .cursor:    {cursor_dir}")
    print(f"  review_gate.py:    {gate_path}")
    print(f"  secret:            {secret_path}")
    print(f"  hooks.json:        {hooks_json}")
    if backup != hooks_json:
        print(f"  hooks.json backup: {backup}")
    if project is not None:
        print(f"  .gitignore:        {project / '.gitignore'} (+ secret/MAP lines)")
    print()
    rc = run_doctor(cursor_dir)
    if rc != 0:
        raise InstallError("doctor failed")
    print(INSTALL_REMINDER)


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
    parser.add_argument(
        "--security",
        action="store_true",
        help="With --doctor: run secret trust and hash checks (Phase 2b)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.doctor:
            cursor = resolve_target_cursor(args.target, args.project if args.project else None)
            return run_doctor(
                cursor if args.target or args.project else None,
                security=args.security,
            )

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
