#!/usr/bin/env python3
"""Shared multi-agent-pr review gate logic for Cursor hooks (MAP V1.1-V2.0)."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

SESSION_DIR = ".review-session"
SESSION_FILE = ".review-session.json"
VERDICT_FILE = ".review-verdict.json"
REVIEW_DIR = ".review"
CANONICAL_SESSION_SUBDIR = "session"
CANONICAL_SESSION_SUMMARY = "session-summary.json"
CANONICAL_VERDICT = "verdict.json"
DEF09_SUNSET_VERSION = "v2.0.0"
CONFIG_FILE = "config.json"
PROGRESS_FILE = "progress.json"
FIX_QUEUE_FILE = "fix-queue.json"
CRITIC_QUEUE_FILE = "critic-queue.json"
SECURITY_QUEUE_FILE = "security-queue.json"
ROLES_DIR = "roles"
ROUTING_RULES_FILE = "routing-rules.json"
HOOKS_DIR = Path(__file__).resolve().parent
LEGACY_SECRET_FILE = Path.home() / ".cursor" / "hooks" / ".review-gate-secret"
SPECS_DIR = ".specs"
POC_DIR = ".review/poc"


class SecretError(RuntimeError):
    pass


def secret_file_path() -> Path:
    env = os.environ.get("OMC_SECRET_FILE")
    if env:
        return Path(env).expanduser().resolve()
    return HOOKS_DIR / ".review-gate-secret"


def _secret_fail(message: str) -> NoReturn:
    print(f"REVIEW_GATE_SECRET_ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def _validate_secret_stat(path: Path) -> None:
    if path.is_symlink():
        raise SecretError(f"secret file must not be a symlink: {path}")
    try:
        st = path.lstat()
    except OSError as exc:
        raise SecretError(f"cannot stat secret file {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise SecretError(f"secret file must be a regular file: {path}")
    if st.st_uid != os.geteuid():
        raise SecretError(f"secret file owner mismatch: {path}")
    mode = st.st_mode & 0o777
    if mode != 0o600:
        raise SecretError(f"secret file must be mode 0600, got {oct(mode)}: {path}")


def _read_secret_bytes(path: Path) -> bytes:
    _validate_secret_stat(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SecretError(f"cannot open secret file {path}: {exc}") from exc
    try:
        data = os.read(fd, 512)
    finally:
        os.close(fd)
    value = data.strip()
    if not value:
        raise SecretError(f"secret file is empty: {path}")
    return value


def migrate_legacy_secret_if_needed(target: Path | None = None) -> bool:
    """Copy legacy secret to target if needed. Returns True if copied."""
    dest = (target or secret_file_path()).resolve()
    legacy = LEGACY_SECRET_FILE.resolve()
    if dest == legacy:
        return False
    if dest.exists():
        return False
    if not LEGACY_SECRET_FILE.is_file() or LEGACY_SECRET_FILE.is_symlink():
        return False
    _validate_secret_stat(LEGACY_SECRET_FILE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest.parent, 0o700)
    except OSError:
        pass
    data = _read_secret_bytes(LEGACY_SECRET_FILE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dest, flags, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise SecretError(f"cannot create secret file {dest}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data + b"\n")
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise SecretError(f"cannot write secret file {dest}: {exc}") from exc
    return True


def _create_secret_file(path: Path) -> bytes:
    if LEGACY_SECRET_FILE.is_file() and not LEGACY_SECRET_FILE.is_symlink():
        migrate_legacy_secret_if_needed(path)
        if path.is_file():
            return _read_secret_bytes(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    value = secrets.token_hex(32).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_secret_bytes(path)
    except OSError as exc:
        raise SecretError(f"cannot create secret file {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(value + b"\n")
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise SecretError(f"cannot write secret file {path}: {exc}") from exc
    return value


def bootstrap_secret(target: Path) -> Path:
    """Ensure secret exists at target with trust contract (install / doctor)."""
    target = target.resolve()
    migrate_legacy_secret_if_needed(target)
    if target.is_file():
        _read_secret_bytes(target)
    else:
        _create_secret_file(target)
    return target


def _secret() -> bytes:
    path = secret_file_path()
    try:
        migrate_legacy_secret_if_needed(path)
        if path.is_file():
            return _read_secret_bytes(path)
        _secret_fail(f"secret file missing: {path}")
    except SecretError as exc:
        _secret_fail(str(exc))
    except OSError as exc:
        _secret_fail(f"secret I/O failed for {path}: {exc}")
    raise AssertionError("unreachable")


REVIEWER_TYPES = {"reviewer-grok", "reviewer-codex", "reviewer-gemini"}
IMPLEMENTER_TYPES = {"coder"}
ALL_RECORDED_TYPES = REVIEWER_TYPES | IMPLEMENTER_TYPES | {
    "architect",
    "tester-writer",
    "explore",
    "generalPurpose",
}

MERGE_COMMAND_PATTERNS = (
    re.compile(r"gh\s+pr\s+merge\b", re.I),
    re.compile(r"gh\s+api\b[^\n]*/merge\b", re.I),
    re.compile(r"(?:curl|wget|http)\b[^\n]*api\.github\.com[^\n]*/merge\b", re.I),
)
PROTECTED_REF_PATTERN = re.compile(r"(?:^|[:/\s])(?:main|master)\b|refs/heads/(?:main|master)\b", re.I)
RISKY_PATH_PATTERN = re.compile(
    r"(^|/)(?:orchestrator\.py|reviewer\.py|config\.py|hooks?\.json|review_gate\.py)$|"
    r"(^|/)(?:\.cursor/hooks|\.cursor/skills/multi-agent-pr|\.github/workflows|profiles|pipelines|roles)/|"
    r"(?:auth|push|merge|scheduler|daemon|trusted|security|credential|token)",
    re.I,
)
DANGEROUS_SHELL_PATTERN = re.compile(
    r"\b(rm\s+-rf|curl\s+|wget\s+|chmod\s+777|>\s*/dev/|mkfs\b|dd\s+if=)",
    re.I,
)
SHELL_WRITE_PATTERN = re.compile(
    r"(?:^|[;&|]\s*)(?:[^\n]*\s)?(?:>|>>)\s*(?!/dev/null\b)"
    r"|\btee\s+(?:-a\s+)?(?!-)\S"
    r"|\bsponge\s+"
    r"|\b(?:cat|python3?|node|ruby|perl)\b[^\n]*(?:>|>>)\s*"
    r"|<<-?\s*['\"]?\w+['\"]?\s*$"
    r"|\b(?:sed|awk)\b[^\n]*\s-i\b"
    r"|\b(?:cp|mv|install|touch|mkdir)\s+",
    re.I | re.M,
)
SHELL_READONLY_PREFIXES = (
    re.compile(r"^\s*git\s+(diff|log|show|status|branch|rev-parse)\b", re.I),
    re.compile(r"^\s*(?:rtk\s+)?(?:pytest|cargo\s+test|go\s+test|jest|vitest)\b", re.I),
    re.compile(r"^\s*(?:rtk\s+)?grep\b", re.I),
)

MAP_MANAGED_ROLES = {
    "coder",
    "architect",
    "reviewer-codex",
    "reviewer-gemini",
    "reviewer-grok",
    "tester-writer",
    "planner",
    "poc-exploit",
    "explore",
    "generalPurpose",
}

SPAWN_PHASE_RULES: dict[str, dict[str, list[str]]] = {
    "multi-agent-pr": {
        "coder": ["adjudication", "coding", "testing", "review-pending", "fix-round-*"],
        "architect": ["spec-writing", "architect-review"],
        "tester-writer": ["coding", "testing"],
        "reviewer-*": ["review-pending", "synthesis-complete"],
    },
    "map-hyperplan": {
        "planner": ["config-confirmed", "draft", "critics", "debate", "revise"],
        "architect": ["draft", "critics", "debate", "revise"],
        "generalPurpose": ["critics", "debate"],
    },
    "map-security": {
        "explore": ["scope", "hunt", "triage"],
        "coder": ["poc", "report"],
        "poc-exploit": ["poc"],
    },
    "map-refactor": {
        "coder": ["implement", "regression", "review-pending", "fix-round-*"],
        "architect": ["analysis", "baseline"],
        "tester-writer": ["implement", "regression"],
        "reviewer-*": ["review-pending", "synthesis-complete"],
    },
}

WORKFLOW_GATE_PROFILES: dict[str, dict[str, Any]] = {
    "multi-agent-pr": {
        "merge_gate_required": True,
        "tier_required": True,
        "forbidden_marker_roles": [],
        "allowed_write_paths": None,
    },
    "map-hyperplan": {
        "merge_gate_required": False,
        "tier_required": False,
        "forbidden_marker_roles": ["coder"],
        "allowed_write_paths": [".specs/", ".review/"],
    },
    "map-security": {
        "merge_gate_required": "conditional",
        "tier_required": False,
        "forbidden_marker_roles": [],
        "allowed_write_paths": None,
        "poc_sandbox": POC_DIR,
    },
    "map-refactor": {
        "merge_gate_required": True,
        "tier_required": True,
        "forbidden_marker_roles": [],
        "allowed_write_paths": None,
        "regression_required": True,
    },
}

WORKFLOW_PHASES: dict[str, list[str]] = {
    "multi-agent-pr": [
        "config-confirmed",
        "spec-writing",
        "architect-review",
        "adjudication",
        "coding",
        "testing",
        "review-pending",
        "synthesis-complete",
        "merge-ready",
        "merged",
        "cleanup",
    ],
    "map-hyperplan": [
        "config-confirmed",
        "draft",
        "critics",
        "debate",
        "revise",
        "accepted",
    ],
    "map-security": [
        "config-confirmed",
        "scope",
        "hunt",
        "triage",
        "poc",
        "report",
    ],
    "map-refactor": [
        "config-confirmed",
        "analysis",
        "baseline",
        "implement",
        "regression",
        "review-pending",
        "synthesis-complete",
        "merge-ready",
    ],
}

WORKFLOW_STOP_PHASES: dict[str, str | None] = {
    "multi-agent-pr": "synthesis-complete",
    "map-refactor": "synthesis-complete",
    "map-hyperplan": "revise",
    "map-security": "report",
}

ROUTING_RULES_FALLBACK = (
    HOOKS_DIR.parent / "skills" / "multi-agent-pr" / "routing-rules.example.json"
)
_SUBPROCESS_TIMEOUT = 10


def _run_subprocess(
    cmd: list[str],
    *,
    cwd: str | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=text,
            cwd=cwd,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="")


QUARANTINE_FILE = "quarantine-tests.json"
ROUTING_THRESHOLDS = {
    "todo_fixme_count": 20,
    "min_security_issues": 1,
}
VALID_SPEC_STATUSES = frozenset({"draft", "in-review", "accepted", "superseded"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _canonical_without_seal(data: dict[str, Any]) -> bytes:
    copy = dict(data)
    copy.pop("seal", None)
    return json.dumps(copy, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _seal_marker(data: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(data)
    sealed["seal"] = hmac.new(_secret(), _canonical_without_seal(sealed), hashlib.sha256).hexdigest()
    return sealed


def _valid_marker_seal(data: dict[str, Any]) -> bool:
    seal = data.get("seal")
    if not isinstance(seal, str) or not seal:
        return False
    expected = hmac.new(_secret(), _canonical_without_seal(data), hashlib.sha256).hexdigest()
    return hmac.compare_digest(seal, expected)


def _extract_command(data: dict[str, Any]) -> str:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    for key in ("command", "cmd"):
        value = data.get(key) or tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_cwd(data: dict[str, Any]) -> str:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    for key in (
        "cwd",
        "workingDirectory",
        "working_directory",
        "workspaceRoot",
        "workspace_root",
    ):
        value = data.get(key) or tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    roots = data.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0].strip():
        return roots[0]
    return os.getcwd()


def _extract_subagent_fields(data: dict[str, Any]) -> tuple[str, str, str]:
    subagent_type = ""
    for key in ("subagent_type", "subagentType", "agent_type", "agentType", "type"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            subagent_type = value.strip()
            break

    model = ""
    for key in ("model", "agent_model"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break

    subagent_id = ""
    for key in ("subagent_id", "subagentId", "agent_id", "agentId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            subagent_id = value.strip()
            break

    return subagent_type, model, subagent_id


def _extract_tool_name(data: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "tool"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_tool_input_path(data: dict[str, Any]) -> str:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for key in ("path", "file_path", "target_file"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_transcript_path(data: dict[str, Any]) -> str:
    for key in ("transcript_path", "transcriptPath", "output_file", "outputFile"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_looks_like_subagent_stop(data: dict[str, Any]) -> bool:
    if os.environ.get("REVIEW_GATE_HOOK_MODE") != "subagentStop":
        return False
    subagent_type, _, _ = _extract_subagent_fields(data)
    if subagent_type not in ALL_RECORDED_TYPES:
        return False
    cursorish_keys = {
        "event",
        "eventName",
        "hook_event",
        "hookEvent",
        "subagent",
        "subagent_id",
        "subagentId",
        "agent_id",
        "agentId",
        "output_file",
        "outputFile",
        "transcript_path",
        "transcriptPath",
        "cwd",
        "workspaceRoot",
        "tool_input",
    }
    return bool(cursorish_keys & set(data.keys()))


def _git_root(cwd: str) -> Path | None:
    try:
        result = _run_subprocess(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            return None
        root = result.stdout.strip()
        return Path(root) if root else None
    except OSError:
        return None


def _git_branch(cwd: str) -> str:
    try:
        result = _run_subprocess(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except OSError:
        return ""


def _git_head(cwd: str) -> str:
    try:
        result = _run_subprocess(["git", "-C", cwd, "rev-parse", "HEAD"])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except OSError:
        return ""


def _git_tree(cwd: str) -> str:
    try:
        result = _run_subprocess(["git", "-C", cwd, "rev-parse", "HEAD^{tree}"])
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except OSError:
        return ""


def _git_diff_files(cwd: str) -> list[str]:
    bases = ["origin/main", "origin/master", "main", "master", "HEAD~1"]
    for base in bases:
        merge_base = _run_subprocess(["git", "-C", cwd, "merge-base", "HEAD", base])
        if merge_base.returncode != 0 or not merge_base.stdout.strip():
            continue
        result = _run_subprocess(
            ["git", "-C", cwd, "diff", "--name-only", merge_base.stdout.strip(), "HEAD"]
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    result = _run_subprocess(["git", "-C", cwd, "diff", "--name-only", "HEAD~1", "HEAD"])
    if result.returncode == 0:
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return []


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _review_path(git_root: Path, name: str) -> Path:
    return git_root / REVIEW_DIR / name


def load_config(git_root: Path) -> dict[str, Any] | None:
    return _read_json_file(_review_path(git_root, CONFIG_FILE))


def load_map_context(data: dict[str, Any]) -> dict[str, Any] | None:
    cwd = _extract_cwd(data)
    git_root = _git_root(cwd)
    if git_root is None:
        return None
    branch = _git_branch(str(git_root))
    head_sha = _git_head(str(git_root))
    if not branch or not head_sha:
        return None
    return {
        "cwd": cwd,
        "git_root": git_root,
        "branch": branch,
        "head_sha": head_sha,
        "tree_sha": _git_tree(str(git_root)),
        "config": load_config(git_root),
        "progress": _read_json_file(_review_path(git_root, PROGRESS_FILE)),
    }


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def _canonical_session_head_dir(git_root: Path, branch: str, head_sha: str) -> Path:
    return git_root / REVIEW_DIR / CANONICAL_SESSION_SUBDIR / _slug(branch) / head_sha


def _legacy_session_head_dir(git_root: Path, branch: str, head_sha: str) -> Path:
    return git_root / SESSION_DIR / _slug(branch) / head_sha


def _session_dir_has_markers(head_dir: Path) -> bool:
    return head_dir.is_dir() and any(head_dir.glob("*.json"))


def _session_marker_read_dir(git_root: Path, branch: str, head_sha: str) -> Path:
    canonical = _canonical_session_head_dir(git_root, branch, head_sha)
    if _session_dir_has_markers(canonical):
        return canonical
    return _legacy_session_head_dir(git_root, branch, head_sha)


def _session_marker_write_dir(git_root: Path, branch: str, head_sha: str) -> Path:
    return _canonical_session_head_dir(git_root, branch, head_sha)


def _verdict_read_path(git_root: Path) -> Path:
    canonical = git_root / REVIEW_DIR / CANONICAL_VERDICT
    if canonical.is_file():
        return canonical
    return git_root / VERDICT_FILE


def _verdict_write_path(git_root: Path) -> Path:
    return git_root / REVIEW_DIR / CANONICAL_VERDICT


def _session_summary_read_path(git_root: Path) -> Path:
    canonical = git_root / REVIEW_DIR / CANONICAL_SESSION_SUMMARY
    if canonical.is_file():
        return canonical
    return git_root / SESSION_FILE


def _session_summary_write_path(git_root: Path) -> Path:
    return git_root / REVIEW_DIR / CANONICAL_SESSION_SUMMARY


def _warn_legacy_def09_paths(git_root: Path, branch: str, head_sha: str) -> None:
    legacy_dir = _legacy_session_head_dir(git_root, branch, head_sha)
    canonical_dir = _canonical_session_head_dir(git_root, branch, head_sha)
    legacy_verdict = git_root / VERDICT_FILE
    canonical_verdict = git_root / REVIEW_DIR / CANONICAL_VERDICT
    legacy_only = (
        _session_dir_has_markers(legacy_dir)
        and not _session_dir_has_markers(canonical_dir)
    ) or (legacy_verdict.is_file() and not canonical_verdict.is_file())
    if legacy_only:
        print(
            f"MAP_DEF09: legacy review paths detected under {git_root}; "
            f"migrate with scripts/migrate_map_state.py (removal planned {DEF09_SUNSET_VERSION})",
            file=sys.stderr,
        )


def _marker_payloads(git_root: Path, branch: str, head_sha: str) -> list[dict[str, Any]]:
    head_dir = _session_marker_read_dir(git_root, branch, head_sha)
    if not head_dir.is_dir():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(head_dir.glob("*.json")):
        data = _read_json_file(path)
        if data is not None:
            payloads.append(data)
    return payloads


def _write_marker(git_root: Path, branch: str, head_sha: str, data: dict[str, Any]) -> None:
    head_dir = _session_marker_write_dir(git_root, branch, head_sha)
    head_dir.mkdir(parents=True, exist_ok=True)
    data = _seal_marker(data)
    raw = json.dumps(data, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    marker = head_dir / f"{_slug(str(data.get('type', 'unknown')))}-{digest}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        print("MAP_GATE: marker already exists (idempotent skip)", file=sys.stderr)
        return
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _write_session_summary(git_root: Path, branch: str, head_sha: str) -> None:
    markers = _marker_payloads(git_root, branch, head_sha)
    summary = {
        "authoritative": False,
        "note": (
            f"Derived from {REVIEW_DIR}/{CANONICAL_SESSION_SUBDIR}/; "
            "merge gate validates marker files, not this summary."
        ),
        "branch": branch,
        "head_sha": head_sha,
        "subagents": markers,
    }
    _write_json_file(_session_summary_write_path(git_root), summary)


def _completed_types(markers: list[dict[str, Any]], allowed: set[str]) -> set[str]:
    completed: set[str] = set()
    for entry in markers:
        subagent_type = entry.get("type")
        if isinstance(subagent_type, str) and subagent_type in allowed:
            completed.add(subagent_type)
    return completed


def _recorded_models_for_types(markers: list[dict[str, Any]], types: set[str]) -> dict[str, str]:
    models: dict[str, str] = {}
    for entry in markers:
        subagent_type = entry.get("type")
        model = entry.get("model")
        if isinstance(subagent_type, str) and subagent_type in types and isinstance(model, str):
            models.setdefault(subagent_type, model)
    return models


def _is_git_push(command: str) -> bool:
    return bool(re.search(r"\bgit\s+push\b", command, re.I))


def _is_protected_push(command: str, branch: str) -> bool:
    if not _is_git_push(command):
        return False
    if PROTECTED_REF_PATTERN.search(command):
        return True
    tokens = command.strip().split()
    if len(tokens) <= 2 and branch in {"main", "master"}:
        return True
    if len(tokens) == 3 and tokens[2] in {"origin", "upstream"} and branch in {"main", "master"}:
        return True
    return False


def is_merge_command(command: str, branch: str = "") -> bool:
    if not command.strip():
        return False
    return any(pattern.search(command) for pattern in MERGE_COMMAND_PATTERNS) or _is_protected_push(
        command, branch
    )


def required_reviewers_for_tier(tier: str) -> set[str]:
    tier = (tier or "standard").lower()
    if tier == "hotfix":
        return set()
    return set(REVIEWER_TYPES)


def min_reviewer_count_for_tier(tier: str) -> int:
    tier = (tier or "standard").lower()
    if tier == "hotfix":
        return 1
    return 3


def requires_coder_for_tier(tier: str) -> bool:
    return (tier or "standard").lower() in {"hotfix", "standard", "large"}


def inferred_minimum_tier(git_root: Path) -> tuple[str, list[str], str]:
    changed = _git_diff_files(str(git_root))
    if not changed:
        return "standard", changed, "diff base unavailable; defaulting to Standard"
    risky = [path for path in changed if RISKY_PATH_PATTERN.search(path)]
    if len(changed) > 10 or risky:
        return "large", changed, f"risky or large change set: {', '.join(risky[:5]) or len(changed)}"
    if len(changed) > 3:
        return "standard", changed, f"{len(changed)} files changed"
    return "hotfix", changed, "small non-risky change set"


def _tier_rank(tier: str) -> int:
    return {"hotfix": 0, "standard": 1, "large": 2}.get(tier, 1)


def _path_allowed(
    path: str,
    allowed_prefixes: list[str] | None,
    git_root: Path | None = None,
) -> bool:
    if allowed_prefixes is None:
        return True
    normalized = path.replace("\\", "/")
    if git_root is not None:
        try:
            if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
                candidate = Path(normalized).resolve()
            else:
                candidate = (git_root / normalized).resolve()
            rel = candidate.relative_to(git_root.resolve())
            normalized = str(rel).replace("\\", "/")
        except (ValueError, OSError):
            return False
    return any(
        normalized.startswith(prefix) or normalized == prefix.rstrip("/")
        for prefix in allowed_prefixes
    )


def _path_in_scope(path: str, scope_paths: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    for scope in scope_paths:
        prefix = scope.rstrip("/") + "/"
        bare = scope.rstrip("/")
        if normalized.startswith(prefix) or normalized == bare:
            return True
    return False


def _phase_matches(phase: str, patterns: list[str]) -> bool:
    if not isinstance(phase, str) or not phase:
        return False
    return any(fnmatch.fnmatch(phase, pattern) for pattern in patterns)


def validate_phase(workflow: str, current_phase: str, subagent_type: str) -> tuple[bool, str]:
    """Return (allowed, reason) for spawning subagent_type at current_phase."""
    rules = SPAWN_PHASE_RULES.get(workflow, SPAWN_PHASE_RULES["multi-agent-pr"])
    patterns: list[str] | None = None
    if subagent_type in rules:
        patterns = rules[subagent_type]
    else:
        for pattern, allowed in rules.items():
            if pattern.endswith("*") and fnmatch.fnmatch(subagent_type, pattern):
                patterns = allowed
                break
    if patterns is None:
        return True, ""
    if _phase_matches(current_phase, patterns):
        return True, ""
    return (
        False,
        f"phase={current_phase!r} does not allow spawn of {subagent_type!r} "
        f"for workflow={workflow!r} (allowed: {patterns}).",
    )


def _resolve_logical_role(config: dict[str, Any], subagent_type: str) -> str:
    roles = config.get("roles")
    if isinstance(roles, dict):
        for logical, mapped in roles.items():
            if mapped == subagent_type:
                return str(logical)
    return subagent_type


def _extract_task_tool_input(data: dict[str, Any]) -> dict[str, Any]:
    tool_input = data.get("tool_input")
    return tool_input if isinstance(tool_input, dict) else {}


def _task_subagent_type(data: dict[str, Any]) -> str:
    tool_input = _extract_task_tool_input(data)
    for key in ("subagent_type", "subagentType"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _extract_subagent_fields(data)[0]


def _task_readonly(data: dict[str, Any]) -> bool:
    return bool(_extract_task_tool_input(data).get("readonly"))


def _task_prompt(data: dict[str, Any]) -> str:
    prompt = _extract_task_tool_input(data).get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _map_exempt_task(data: dict[str, Any], workflow: str = "") -> bool:
    if _task_readonly(data):
        return True
    subagent_type = _task_subagent_type(data)
    if (
        workflow == "map-hyperplan"
        and subagent_type == "generalPurpose"
        and re.search(r"\b(review|audit|mmr|multi-model)\b", _task_prompt(data), re.I)
    ):
        return True
    return False


def _is_map_managed_role(subagent_type: str, workflow: str) -> bool:
    if subagent_type in MAP_MANAGED_ROLES:
        return True
    if subagent_type.startswith("reviewer-"):
        return True
    if workflow == "map-hyperplan" and subagent_type == "generalPurpose":
        return True
    return False


def _role_aligned_with_config(config: dict[str, Any], subagent_type: str) -> bool:
    roles = config.get("roles")
    if isinstance(roles, dict) and roles:
        if subagent_type in roles.values() or subagent_type in roles:
            return True
        return False
    return subagent_type in MAP_MANAGED_ROLES or subagent_type.startswith("reviewer-")


def _shell_looks_readonly(command: str) -> bool:
    return any(pattern.search(command) for pattern in SHELL_READONLY_PREFIXES)


def _shell_redirects_to_poc(command: str) -> bool:
    return bool(re.search(r"(?:>|>>)\s*[^\s;|&]*\.review/poc/", command, re.I))


def _shell_write_blocked(command: str, logical_role: str) -> bool:
    if not command.strip() or _shell_looks_readonly(command):
        return False
    if logical_role == "poc-exploit" and _shell_redirects_to_poc(command):
        return False
    return bool(SHELL_WRITE_PATTERN.search(command))


def _code_outside_allowed(
    changed: list[str], allowed_prefixes: list[str], git_root: Path | None = None
) -> list[str]:
    return [p for p in changed if not _path_allowed(p, allowed_prefixes, git_root)]


def _security_code_modified(git_root: Path, config: dict[str, Any]) -> bool:
    changed = _git_diff_files(str(git_root))
    sandbox = str(config.get("poc_sandbox", POC_DIR)).rstrip("/") + "/"
    poc_prefixes = (sandbox, ".review/poc/", ".review/reports/")
    scope_paths = config.get("scope_paths") or []

    for path in changed:
        norm = path.replace("\\", "/")
        if any(norm.startswith(prefix) for prefix in poc_prefixes):
            continue
        if scope_paths:
            if not _path_in_scope(norm, scope_paths):
                return True
        else:
            return True
    return False


def _validate_config_cross_check(
    config: dict[str, Any] | None,
    verdict: dict[str, Any],
    minimum_tier: str,
) -> tuple[bool, str, str]:
    if config is None or not config.get("active"):
        return True, "", ""

    workflow = str(config.get("workflow") or "multi-agent-pr")
    if workflow not in WORKFLOW_GATE_PROFILES:
        return False, "Invalid workflow in .review/config.json.", f"BLOCKED: unknown workflow={workflow!r}."

    profile = WORKFLOW_GATE_PROFILES[workflow]
    if profile.get("merge_gate_required") is False:
        return True, "", ""

    config_tier = str(config.get("tier") or verdict.get("tier") or "standard").lower()
    verdict_tier = str(verdict.get("tier") or "standard").lower()
    if _tier_rank(config_tier) < _tier_rank(minimum_tier):
        return (
            False,
            f"Config tier {config_tier} below enforced minimum {minimum_tier}.",
            f"BLOCKED: config tier mismatch with diff inference.",
        )
    if _tier_rank(verdict_tier) < _tier_rank(config_tier):
        return (
            False,
            "Verdict tier below config tier.",
            f"BLOCKED: verdict tier={verdict_tier} config tier={config_tier}.",
        )

    config_models = config.get("models") or config.get("reviewers") or []
    if isinstance(config_models, list):
        verdict_reviewers = verdict.get("reviewers") or []
        if isinstance(verdict_reviewers, list):
            reviewer_claims: set[str] = set()
            for entry in verdict_reviewers:
                if isinstance(entry, str):
                    reviewer_claims.add(entry)
                elif isinstance(entry, dict):
                    for key in ("type", "model"):
                        value = entry.get(key)
                        if isinstance(value, str):
                            reviewer_claims.add(value)
            missing = [
                m
                for m in verdict_reviewers
                if isinstance(m, str) and m not in config_models
            ]
            if missing and config.get("active"):
                return (
                    False,
                    "Verdict reviewers not covered by config.models.",
                    f"BLOCKED: verdict lists {missing} not in config.",
                )
            missing_from_verdict = [
                m
                for m in config_models
                if isinstance(m, str) and m not in reviewer_claims
            ]
            if missing_from_verdict and config.get("active"):
                return (
                    False,
                    "Config models missing from verdict reviewers.",
                    f"BLOCKED: config requires {missing_from_verdict}.",
                )
    return True, "", ""


def _validate_planning_only_session(
    git_root: Path, branch: str, head_sha: str, profile: dict[str, Any]
) -> tuple[bool, str, str]:
    markers = _marker_payloads(git_root, branch, head_sha)
    forbidden = set(profile.get("forbidden_marker_roles") or [])
    bad = [m.get("type") for m in markers if m.get("type") in forbidden]
    if bad:
        return (
            False,
            "Planning-only workflow cannot include coder markers.",
            f"BLOCKED: forbidden markers {bad} for map-hyperplan.",
        )
    allowed = profile.get("allowed_write_paths") or [".specs/", ".review/"]
    changed = _git_diff_files(str(git_root))
    outside = _code_outside_allowed(changed, allowed, git_root)
    if outside:
        return (
            False,
            "Planning-only workflow modified code outside allowed paths.",
            f"BLOCKED: changes outside {allowed}: {outside[:10]}.",
        )
    return True, "Planning-only session valid.", "Planning-only session valid."


def _validate_security_session(
    git_root: Path,
    branch: str,
    head_sha: str,
    config: dict[str, Any],
) -> tuple[bool, str, str]:
    if not _security_code_modified(git_root, config):
        return True, "Security audit report-only.", "Security audit report-only."
    return validate_review_state(git_root, branch, head_sha, skip_workflow_branch=True)


def validate_review_state(
    git_root: Path,
    branch: str,
    head_sha: str,
    *,
    skip_workflow_branch: bool = False,
) -> tuple[bool, str, str]:
    config = load_config(git_root)
    workflow = str((config or {}).get("workflow") or "multi-agent-pr")
    profile = WORKFLOW_GATE_PROFILES.get(workflow, WORKFLOW_GATE_PROFILES["multi-agent-pr"])

    if not skip_workflow_branch:
        merge_required = profile.get("merge_gate_required")
        if merge_required == "conditional" and config:
            if not _security_code_modified(git_root, config):
                return True, "Security audit report-only.", "Security audit report-only."

    _warn_legacy_def09_paths(git_root, branch, head_sha)

    verdict_path = _verdict_read_path(git_root)
    verdict = _read_json_file(verdict_path)
    markers = _marker_payloads(git_root, branch, head_sha)
    invalid_markers = [
        marker
        for marker in markers
        if marker.get("branch") != branch
        or marker.get("head_sha") != head_sha
        or marker.get("source") != "cursor-subagentStop"
        or not _valid_marker_seal(marker)
    ]
    if invalid_markers:
        return (
            False,
            "Review session contains invalid marker files.",
            "BLOCKED: delete forged/stale review markers and re-run required subagents.",
        )

    if profile.get("forbidden_marker_roles"):
        bad = [m.get("type") for m in markers if m.get("type") in profile["forbidden_marker_roles"]]
        if bad:
            return (
                False,
                "Forbidden subagent markers for this workflow.",
                f"BLOCKED: {bad} not allowed for workflow={workflow}.",
            )

    if not markers:
        return (
            False,
            f"Multi-model review required before merge. Missing review markers under "
            f"{REVIEW_DIR}/{CANONICAL_SESSION_SUBDIR}/ or {SESSION_DIR}.",
            "BLOCKED: Launch required subagents first.",
        )

    if verdict is None:
        return (
            False,
            "Multi-model review required before merge. Missing review verdict "
            f"({REVIEW_DIR}/{CANONICAL_VERDICT} or {VERDICT_FILE}).",
            f"BLOCKED: After review synthesis, write {REVIEW_DIR}/{CANONICAL_VERDICT}.",
        )

    if verdict.get("branch") != branch:
        return (
            False,
            "Review verdict branch mismatch.",
            f"BLOCKED: verdict branch={verdict.get('branch')!r} current={branch!r}.",
        )

    if verdict.get("head_sha") != head_sha:
        return (
            False,
            "Review verdict is stale.",
            f"BLOCKED: verdict head_sha={verdict.get('head_sha')!r} current={head_sha!r}.",
        )

    tree_sha = _git_tree(str(git_root))
    if verdict.get("tree_sha") and verdict.get("tree_sha") != tree_sha:
        return (
            False,
            "Review verdict tree mismatch.",
            f"BLOCKED: verdict tree_sha mismatch.",
        )

    tier = str(verdict.get("tier") or "standard").lower()
    if tier not in {"hotfix", "standard", "large"}:
        return False, "Invalid review tier.", "BLOCKED: tier must be hotfix, standard, or large."

    minimum_tier, changed_files, tier_reason = inferred_minimum_tier(git_root)
    if _tier_rank(tier) < _tier_rank(minimum_tier):
        return (
            False,
            f"Declared tier {tier} is below the enforced minimum {minimum_tier}.",
            f"BLOCKED: {tier_reason}. Changed files={changed_files[:20]}.",
        )

    ok_cfg, user_cfg, agent_cfg = _validate_config_cross_check(config, verdict, minimum_tier)
    if not ok_cfg:
        return ok_cfg, user_cfg, agent_cfg

    completed_reviewers = _completed_types(markers, REVIEWER_TYPES)
    min_reviewers = min_reviewer_count_for_tier(tier)
    if len(completed_reviewers) < min_reviewers:
        return (
            False,
            f"Insufficient reviewer subagents for tier {tier}.",
            f"BLOCKED: need {min_reviewers}; recorded={sorted(completed_reviewers)}.",
        )

    if tier in {"standard", "large"}:
        missing = sorted(required_reviewers_for_tier(tier) - completed_reviewers)
        if missing:
            return (
                False,
                f"Missing required reviewer subagents: {', '.join(missing)}.",
                f"BLOCKED: Standard/Large requires all three reviewers.",
            )

    completed_implementers = _completed_types(markers, IMPLEMENTER_TYPES)
    if requires_coder_for_tier(tier) and "coder" not in completed_implementers:
        return (
            False,
            f"{tier.title()} requires a coder subagent record.",
            "BLOCKED: use Task(subagent_type='coder').",
        )

    p0 = verdict.get("p0")
    p1 = verdict.get("p1")
    try:
        if int(p0) > 0 or int(p1) > 0:
            return (
                False,
                "Review verdict reports unresolved P0/P1 issues.",
                f"BLOCKED: verdict p0={p0}, p1={p1}.",
            )
    except (TypeError, ValueError):
        return False, "Review verdict must include numeric p0 and p1.", "BLOCKED: invalid p0/p1."

    verdict_reviewers = verdict.get("reviewers")
    if not isinstance(verdict_reviewers, list) or not verdict_reviewers:
        return False, "Review verdict must list reviewer models.", "BLOCKED: empty reviewers."

    reviewer_claims: set[str] = set()
    for entry in verdict_reviewers:
        if isinstance(entry, str):
            reviewer_claims.add(entry)
        elif isinstance(entry, dict):
            for key in ("type", "model"):
                value = entry.get(key)
                if isinstance(value, str):
                    reviewer_claims.add(value)

    recorded_models = _recorded_models_for_types(markers, completed_reviewers)
    missing_claims = [
        reviewer_type
        for reviewer_type in completed_reviewers
        if reviewer_type not in reviewer_claims and recorded_models.get(reviewer_type) not in reviewer_claims
    ]
    if missing_claims:
        return (
            False,
            "Review verdict reviewers do not match recorded subagents.",
            f"BLOCKED: missing claims for {missing_claims}.",
        )

    return True, "Review gate passed.", "Review gate passed."


def check_merge_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    command = _extract_command(data)
    ctx = load_map_context(data)
    if ctx is None:
        if is_merge_command(command):
            return {
                "permission": "deny",
                "user_message": "Multi-model review gate could not locate the git worktree.",
                "agent_message": "BLOCKED: run reviewers in the target git repo.",
            }
        return {"permission": "allow"}

    git_root = ctx["git_root"]
    branch = ctx["branch"]
    head_sha = ctx["head_sha"]

    if not is_merge_command(command, branch):
        return {"permission": "allow"}

    config = ctx.get("config") or {}
    workflow = str(config.get("workflow") or "multi-agent-pr")
    if workflow == "map-hyperplan":
        return {
            "permission": "deny",
            "user_message": "map-hyperplan is planning-only; merge/push is permanently blocked.",
            "agent_message": "BLOCKED: map-hyperplan never passes merge gate.",
        }

    ok, user_message, agent_message = validate_review_state(git_root, branch, head_sha)
    if ok:
        return {"permission": "allow"}
    return {"permission": "deny", "user_message": user_message, "agent_message": agent_message}


def _issue_list(fix_queue: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = fix_queue.get(key) or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _filter_resolved_issues(
    issues: list[dict[str, Any]], resolved_ids: set[str]
) -> list[dict[str, Any]]:
    if not resolved_ids:
        return issues
    kept: list[dict[str, Any]] = []
    for item in issues:
        issue_id = str(item.get("id") or item.get("issue_id") or "")
        if issue_id and issue_id in resolved_ids:
            continue
        kept.append(item)
    return kept


def advance_fix_queue(
    git_root: Path,
    *,
    mark_resolved_ids: list[str] | None = None,
    increment_round: bool = False,
    head_sha: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """After a fix-review cycle: drop resolved issues, reset phase for stop hook."""
    fix_path = _review_path(git_root, FIX_QUEUE_FILE)
    fix_queue = _read_json_file(fix_path)
    if fix_queue is None:
        return {"ok": False, "reason": "fix-queue missing"}

    resolved = {str(i) for i in (mark_resolved_ids or [])}
    p0 = _filter_resolved_issues(_issue_list(fix_queue, "p0_issues"), resolved)
    p1 = _filter_resolved_issues(_issue_list(fix_queue, "p1_issues"), resolved)
    fix_queue["p0_issues"] = p0
    fix_queue["p1_issues"] = p1

    if increment_round:
        fix_queue["round"] = int(fix_queue.get("round") or 0) + 1
    if head_sha:
        fix_queue["head_sha"] = head_sha
    if branch:
        fix_queue["branch"] = branch
    fix_queue["updated_at"] = _now_iso()

    if not p0 and not p1:
        if fix_path.is_file():
            fix_path.unlink()
        queue_action = "deleted"
    else:
        _write_json_file(fix_path, fix_queue)
        queue_action = "updated"

    progress_path = _review_path(git_root, PROGRESS_FILE)
    progress = _read_json_file(progress_path) or {}
    progress["phase"] = "synthesis-complete"
    if head_sha:
        progress["head_sha"] = head_sha
    if branch:
        progress["branch"] = branch
    if increment_round:
        progress["fix_round"] = int(progress.get("fix_round") or 0) + 1
    progress["updated_at"] = _now_iso()
    _write_json_file(progress_path, progress)

    queue_round = fix_queue.get("round") if queue_action == "updated" else None
    progress_fix_round = int(progress.get("fix_round") or 0)
    return {
        "ok": True,
        "queue_action": queue_action,
        "p0_remaining": len(p0),
        "p1_remaining": len(p1),
        "phase": progress["phase"],
        "queue_round": queue_round,
        "progress_fix_round": progress_fix_round,
        "round": int(queue_round or 0) if queue_action == "updated" else progress_fix_round,
    }


def advance_critic_queue(
    git_root: Path,
    *,
    mark_resolved_ids: list[str] | None = None,
    increment_round: bool = False,
) -> dict[str, Any]:
    """Hyperplan: remove resolved critic items; optionally bump round."""
    critic_path = _review_path(git_root, CRITIC_QUEUE_FILE)
    critic_queue = _read_json_file(critic_path)
    if critic_queue is None:
        return {"ok": False, "reason": "critic-queue missing"}

    resolved = {str(i) for i in (mark_resolved_ids or [])}
    pending = critic_queue.get("pending_items") or []
    if not isinstance(pending, list):
        pending = []
    kept = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("dimension") or "")
        if item_id and item_id in resolved:
            continue
        kept.append(item)
    critic_queue["pending_items"] = kept
    if increment_round:
        critic_queue["round"] = int(critic_queue.get("round") or 1) + 1
    critic_queue["updated_at"] = _now_iso()

    if not kept:
        if critic_path.is_file():
            critic_path.unlink()
        action = "deleted"
    else:
        _write_json_file(critic_path, critic_queue)
        action = "updated"

    progress_path = _review_path(git_root, PROGRESS_FILE)
    progress = _read_json_file(progress_path) or {}
    if kept:
        progress["phase"] = "revise"
    else:
        progress["phase"] = "accepted"
    progress["updated_at"] = _now_iso()
    _write_json_file(progress_path, progress)

    return {"ok": True, "queue_action": action, "pending_remaining": len(kept), "phase": progress["phase"]}


def parse_spec_frontmatter(text: str) -> dict[str, str]:
    """Parse YAML-like frontmatter between --- markers (minimal, no PyYAML dep)."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip().strip('"').strip("'")
    return fields


def load_routing_rules(git_root: Path) -> dict[str, Any]:
    candidates = [
        _review_path(git_root, ROUTING_RULES_FILE),
        git_root / ROUTING_RULES_FILE,
        ROUTING_RULES_FALLBACK,
    ]
    for path in candidates:
        data = _read_json_file(path)
        if data and isinstance(data.get("rules"), list):
            return data
    return {"schema_version": 1, "rules": [], "conflict_resolution": "highest priority wins"}


def match_routing_rules(text: str, rules_doc: dict[str, Any]) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    lowered = text.lower()
    matches: list[dict[str, Any]] = []
    for rule in rules_doc.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        signals = rule.get("signals") or []
        if not isinstance(signals, list):
            continue
        if any(isinstance(s, str) and s.lower() in lowered for s in signals):
            matches.append(rule)
    if not matches:
        return []
    max_priority = max(int(m.get("priority") or 0) for m in matches)
    top = [m for m in matches if int(m.get("priority") or 0) == max_priority]
    return top


def routing_hints_from_rules(text: str, git_root: Path) -> list[str]:
    rules_doc = load_routing_rules(git_root)
    matched = match_routing_rules(text, rules_doc)
    if not matched:
        return []
    if len(matched) == 1:
        rule = matched[0]
        return [f"Routing rule {rule.get('id')}: consider workflow={rule.get('workflow')}."]
    workflows = sorted({str(m.get("workflow")) for m in matched if m.get("workflow")})
    return [f"Routing tie: consider workflows {', '.join(workflows)} (AskQuestion to pick)."]


def validate_debate_report(data: dict[str, Any]) -> tuple[bool, str]:
    required_lists = ("claims", "counterclaims", "evidence", "unresolved", "consensus_items")
    for key in required_lists:
        value = data.get(key)
        if not isinstance(value, list):
            return False, f"missing or invalid list field: {key}"
    for scalar in ("round", "session_id"):
        if not data.get(scalar):
            return False, f"missing field: {scalar}"
    return True, ""


def security_fingerprint(finding: dict[str, Any]) -> str:
    parts = [
        str(finding.get("asset") or ""),
        str(finding.get("vuln_class") or finding.get("vulnerability_class") or ""),
        str(finding.get("sink") or ""),
        str(finding.get("exploitability") or ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def append_unverified_findings_to_security_queue(
    git_root: Path, findings: list[dict[str, Any]], *, session_id: str
) -> int:
    """Append unverified High+ findings; returns count added."""
    queue_path = _review_path(git_root, SECURITY_QUEUE_FILE)
    queue = _read_json_file(queue_path) or {
        "schema_version": 1,
        "session_id": session_id,
        "pending_findings": [],
    }
    pending = queue.get("pending_findings") or []
    if not isinstance(pending, list):
        pending = []
    existing = {item.get("fingerprint") for item in pending if isinstance(item, dict)}
    added = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").lower()
        verified = finding.get("verified") is True
        if verified or severity not in {"high", "critical", "p0", "p1"}:
            continue
        fp = security_fingerprint(finding)
        if fp in existing:
            continue
        pending.append({**finding, "fingerprint": fp, "verified": False})
        existing.add(fp)
        added += 1
    queue["pending_findings"] = pending
    queue["updated_at"] = _now_iso()
    _write_json_file(queue_path, queue)
    return added


def load_quarantine_tests(git_root: Path) -> set[str]:
    data = _read_json_file(_review_path(git_root, QUARANTINE_FILE))
    if not data:
        return set()
    tests = data.get("tests") or data.get("quarantined") or []
    if not isinstance(tests, list):
        return set()
    return {str(t) for t in tests}


def validate_regression_result(
    git_root: Path,
    *,
    failed_tests: list[str],
    retry_count: int = 0,
    max_retries: int = 2,
) -> tuple[bool, str, str]:
    quarantine = load_quarantine_tests(git_root)
    actionable = [t for t in failed_tests if t not in quarantine]
    if not actionable:
        return True, "All failures quarantined.", "pass"
    if retry_count < max_retries:
        return True, f"Retry regression ({retry_count + 1}/{max_retries}).", "retry"
    return False, f"Regression failures: {actionable[:5]}", "fail"


def validate_knowledge_artifacts(
    git_root: Path, pr_number: int | str
) -> tuple[bool, list[str]]:
    """Soft validation warnings for learnings/decisions."""
    warnings: list[str] = []
    base = git_root / REVIEW_DIR / "knowledge" / f"pr{pr_number}"
    learnings = base / "learnings.md"
    decisions = base / "decisions.md"
    if learnings.is_file():
        text = learnings.read_text(encoding="utf-8", errors="ignore")
        if "sources:" not in text.lower() and "confidence:" not in text.lower():
            warnings.append("learnings.md: missing sources or confidence tags")
    if decisions.is_file():
        text = decisions.read_text(encoding="utf-8", errors="ignore")
        for token in ("decision", "rationale"):
            if token not in text.lower():
                warnings.append(f"decisions.md: missing {token}")
    return len(warnings) == 0, warnings


def _routing_thresholds(config: dict[str, Any] | None) -> dict[str, int]:
    merged = dict(ROUTING_THRESHOLDS)
    if config and isinstance(config.get("routing_thresholds"), dict):
        for key, value in config["routing_thresholds"].items():
            if isinstance(value, int):
                merged[key] = value
    return merged


def _count_todo_fixme_threshold(git_root: Path, threshold: int) -> bool:
    try:
        result = _run_subprocess(["rg", "-c", r"TODO|FIXME", str(git_root)])
        if result.returncode not in (0, 1):
            if result.returncode == 124:
                print("MAP_GATE: rg timed out for TODO/FIXME count", file=sys.stderr)
            elif shutil.which("rg") is None:
                print("MAP_GATE: rg not found; skipping TODO/FIXME routing hint", file=sys.stderr)
            return False
        total = sum(int(line.split(":")[-1]) for line in result.stdout.splitlines() if ":" in line)
        return total >= threshold
    except OSError:
        print("MAP_GATE: rg failed for TODO/FIXME count", file=sys.stderr)
        return False


def _security_issue_count(git_root: Path, min_count: int) -> bool:
    try:
        result = _run_subprocess(
            ["gh", "issue", "list", "--label", "security", "--limit", "10", "--json", "number"],
            cwd=str(git_root),
        )
        if result.returncode != 0:
            if result.returncode == 124:
                print("MAP_GATE: gh issue list timed out", file=sys.stderr)
            elif shutil.which("gh") is None:
                print("MAP_GATE: gh not found; skipping security issue routing hint", file=sys.stderr)
            return False
        items = json.loads(result.stdout or "[]")
        return isinstance(items, list) and len(items) >= min_count
    except (OSError, json.JSONDecodeError):
        return False


def _fix_queue_followup(
    ctx: dict[str, Any], git_root: Path, config: dict[str, Any], progress: dict[str, Any]
) -> dict[str, Any]:
    fix_queue = _read_json_file(_review_path(git_root, FIX_QUEUE_FILE))
    if fix_queue is None:
        return {}

    if fix_queue.get("head_sha") != ctx["head_sha"]:
        return {}
    if fix_queue.get("branch") != ctx["branch"]:
        return {}

    current_round = int(fix_queue.get("round") or 0)
    max_rounds = int(config.get("max_rounds") or config.get("max_fix_rounds") or 2)
    p0_issues = fix_queue.get("p0_issues") or []
    p1_issues = fix_queue.get("p1_issues") or []
    p0_count = len(p0_issues) if isinstance(p0_issues, list) else 0
    p1_count = len(p1_issues) if isinstance(p1_issues, list) else 0

    if p0_count == 0 and p1_count == 0:
        return {}

    if current_round >= max_rounds:
        return {
            "followup_message": (
                f"Reached max fix rounds ({max_rounds}/{max_rounds}). "
                f"{p0_count} P0 and {p1_count} P1 remain. "
                "Use AskQuestion: continue 1 round / stop for human review / abort PR."
            )
        }

    next_round = current_round + 1
    return {
        "followup_message": (
            f"Fix-queue has {p0_count} P0 and {p1_count} P1 issues. "
            f"Starting Fix Round {next_round}/{max_rounds}. "
            "Read .review/fix-queue.json, spawn Coder to fix, then re-run reviewers. "
            "Commander drives fix-round manually; do not rely on stop hook to advance phase."
        )
    }


def _critic_queue_followup(git_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    critic_queue = _read_json_file(_review_path(git_root, CRITIC_QUEUE_FILE))
    if critic_queue is None:
        return {}

    session_id = config.get("session_id")
    if critic_queue.get("session_id") and critic_queue.get("session_id") != session_id:
        return {}

    pending = critic_queue.get("pending_items") or []
    if not isinstance(pending, list) or not pending:
        return {}

    current_round = int(critic_queue.get("round") or 1)
    return {
        "followup_message": (
            f"Critic-queue has {len(pending)} pending item(s) at round {current_round}. "
            "Revise spec and re-run critics per map-hyperplan SKILL."
        )
    }


def _security_queue_followup(git_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    security_queue = _read_json_file(_review_path(git_root, SECURITY_QUEUE_FILE))
    if security_queue is None:
        return {}

    session_id = config.get("session_id")
    if security_queue.get("session_id") and security_queue.get("session_id") != session_id:
        return {}

    pending = security_queue.get("pending_findings") or security_queue.get("pending_items") or []
    if not isinstance(pending, list) or not pending:
        return {}

    summaries = []
    for item in pending[:3]:
        if isinstance(item, dict):
            summaries.append(
                f"{item.get('asset', '?')}/{item.get('vuln_class', '?')} ({item.get('severity', '?')})"
            )
    detail = "; ".join(summaries) if summaries else "see security-queue.json"
    hunters = config.get("roles") or {}
    hunter_hint = ", ".join(k for k in hunters if str(k).startswith("hunter")) or "explore"

    return {
        "followup_message": (
            f"Security-queue has {len(pending)} unverified finding(s): {detail}. "
            f"Run optional second hunt ({hunter_hint}) per map-security SKILL."
        )
    }


def stop_check_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    ctx = load_map_context(data)
    if ctx is None:
        return {}

    git_root = ctx["git_root"]
    config = ctx["config"]
    if config is None or not config.get("active"):
        return {}

    workflow = str(config.get("workflow") or "multi-agent-pr")
    stop_phase = WORKFLOW_STOP_PHASES.get(workflow)
    if stop_phase is None:
        return {}

    session_id = config.get("session_id")
    progress = ctx["progress"] or {}
    if progress.get("session_id") != session_id:
        return {}
    phase = progress.get("phase")
    if phase != stop_phase:
        return {}

    fix_result = _fix_queue_followup(ctx, git_root, config, progress)
    if fix_result:
        return fix_result

    if workflow == "map-security":
        security_result = _security_queue_followup(git_root, config)
        if security_result:
            return security_result

    return _critic_queue_followup(git_root, config)


def _draft_specs(git_root: Path) -> list[str]:
    specs_root = git_root / SPECS_DIR
    if not specs_root.is_dir():
        return []
    drafts: list[str] = []
    for path in sorted(specs_root.glob("**/*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        frontmatter = parse_spec_frontmatter(text)
        status = frontmatter.get("status", "").lower()
        if status == "draft" or (
            not status and re.search(r"status:\s*draft", text, re.I)
        ):
            drafts.append(str(path.relative_to(git_root)))
    return drafts


def _security_issue_hint(git_root: Path, min_count: int = 1) -> bool:
    return _security_issue_count(git_root, min_count)


def _routing_hints(
    git_root: Path, config: dict[str, Any] | None, user_text: str = ""
) -> list[str]:
    hints: list[str] = []
    thresholds = _routing_thresholds(config)
    if config and config.get("active"):
        wf = config.get("workflow", "multi-agent-pr")
        hints.append(f"⚠️ Active MAP session workflow={wf} session={config.get('session_id')}.")
    drafts = _draft_specs(git_root)
    if drafts:
        hints.append(f"Draft specs detected ({', '.join(drafts[:3])}); consider map-hyperplan.")
    if _security_issue_hint(git_root, thresholds["min_security_issues"]):
        hints.append("Open security-labeled GitHub issues; consider map-security.")
    if _count_todo_fixme_threshold(git_root, thresholds["todo_fixme_count"]):
        hints.append("High TODO/FIXME count; consider map-refactor.")
    hints.extend(routing_hints_from_rules(user_text, git_root))
    return hints


def session_resume_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    ctx = load_map_context(data)
    if ctx is None:
        return {}

    git_root = ctx["git_root"]
    progress = ctx["progress"]
    config = ctx["config"]
    parts: list[str] = []

    if progress and config and config.get("active"):
        if progress.get("branch") == ctx["branch"]:
            parts.append(
                f"Interrupted MAP pipeline detected (session {config.get('session_id')}). "
                f"workflow={config.get('workflow', 'multi-agent-pr')}. "
                f"phase={progress.get('phase')}. "
                f"completed={progress.get('completed')}. Resume from the current phase."
            )

    hints = _routing_hints(git_root, config, str(data.get("user_message") or data.get("prompt") or ""))
    parts.extend(hints)

    if not parts:
        return {}
    return {"additional_context": "\n".join(parts)}


def _infer_role_from_transcript(transcript_path: str) -> str:
    lowered = transcript_path.lower()
    for role in (
        "reviewer-grok",
        "reviewer-codex",
        "reviewer-gemini",
        "tester-writer",
        "architect",
        "coder",
        "explore",
        "generalpurpose",
    ):
        if role.replace("-", "") in lowered.replace("-", ""):
            return role if role != "generalpurpose" else "generalPurpose"
    return ""


def set_role_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    ctx = load_map_context(data)
    if ctx is None:
        return {}

    subagent_type, model, subagent_id = _extract_subagent_fields(data)
    if not subagent_id:
        subagent_id = _slug(subagent_type or "unknown") + "-" + secrets.token_hex(4)

    config = ctx["config"] or {}
    logical_role = ""
    for key in ("logical_role", "logicalRole"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            logical_role = value.strip()
            break
    if not logical_role and config:
        logical_role = _resolve_logical_role(config, subagent_type)

    workflow = str(config.get("workflow") or "multi-agent-pr")
    progress = ctx["progress"] or {}

    role_file = _review_path(ctx["git_root"], ROLES_DIR) / f"{_slug(subagent_id)}.json"
    payload = {
        "subagent_id": subagent_id,
        "role": subagent_type,
        "logical_role": logical_role or subagent_type,
        "subagent_type": subagent_type,
        "model": model or None,
        "workflow": config.get("workflow"),
        "started_at": _now_iso(),
    }
    _write_json_file(role_file, payload)

    if config.get("active") and progress.get("session_id") == config.get("session_id"):
        progress = dict(progress)
        progress["last_spawn"] = {
            "subagent_id": subagent_id,
            "subagent_type": subagent_type,
            "logical_role": payload["logical_role"],
            "at": _now_iso(),
        }
        progress["updated_at"] = _now_iso()
        _write_json_file(_review_path(ctx["git_root"], PROGRESS_FILE), progress)

    return {}


def _role_for_permission(ctx: dict[str, Any], data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    git_root = ctx["git_root"]
    subagent_id = _extract_subagent_fields(data)[2]
    role_data: dict[str, Any] | None = None
    if subagent_id:
        role_data = _read_json_file(_review_path(git_root, ROLES_DIR) / f"{_slug(subagent_id)}.json")
    if role_data is None:
        transcript = _extract_transcript_path(data)
        inferred = _infer_role_from_transcript(transcript) if transcript else ""
        if inferred:
            role_data = {"role": inferred}
    role = str(
        (role_data or {}).get("logical_role")
        or (role_data or {}).get("role")
        or ""
    )
    return role, role_data


def check_tool_permission_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    ctx = load_map_context(data)
    if ctx is None:
        return {"permission": "allow"}

    config = ctx["config"]
    if not config or not config.get("active"):
        return {"permission": "allow"}

    tool_name = _extract_tool_name(data)
    path = _extract_tool_input_path(data)
    command = _extract_command(data)
    role, role_data = _role_for_permission(ctx, data)
    workflow = str(config.get("workflow") or "multi-agent-pr")

    if tool_name == "Shell":
        restricted_shell_roles = {
            "explore",
            "poc-exploit",
            "planner",
            "generalPurpose",
        }
        if role.startswith("reviewer-") or role in restricted_shell_roles:
            if _shell_write_blocked(command, role):
                return {
                    "permission": "deny",
                    "user_message": "Shell file-write pattern blocked for read-only MAP role.",
                    "agent_message": "BLOCKED: use allowed Write tool paths instead of shell redirects.",
                }
        if role in {"poc-exploit", "explore"} and DANGEROUS_SHELL_PATTERN.search(command):
            return {
                "permission": "deny",
                "user_message": "Dangerous shell command blocked for security hunter/PoC role.",
                "agent_message": "BLOCKED: use .review/poc/ sandbox only.",
            }

    if tool_name not in {"Write", "Delete"}:
        return {"permission": "allow"}

    norm_path = path.replace("\\", "/")
    git_root = ctx["git_root"]

    if role.startswith("reviewer-"):
        return {
            "permission": "deny",
            "user_message": "Reviewers cannot write or delete files.",
            "agent_message": "BLOCKED: reviewer role is read-only.",
        }

    if role == "explore":
        return {
            "permission": "deny",
            "user_message": "Hunter/explore role is read-only.",
            "agent_message": "BLOCKED: explore cannot Write/Delete.",
        }

    if role == "generalPurpose" and workflow == "map-hyperplan":
        if not _path_allowed(norm_path, [".review/reports/", ".review/"], git_root):
            return {
                "permission": "deny",
                "user_message": "Critics may only write under .review/reports/.",
                "agent_message": "BLOCKED: critic write path.",
            }

    if role in {"architect", "planner"}:
        if not _path_allowed(norm_path, [".specs/", ".review/", SPECS_DIR + "/"], git_root):
            return {
                "permission": "deny",
                "user_message": "Architect/planner may only write under .specs/ and .review/.",
                "agent_message": "BLOCKED: architect/planner write path.",
            }

    if role == "tester-writer":
        if not _path_allowed(norm_path, ["tests/", "test/"], git_root):
            return {
                "permission": "deny",
                "user_message": "Tester may only write under tests/.",
                "agent_message": "BLOCKED: tester write path.",
            }

    if role == "poc-exploit" or (role == "coder" and workflow == "map-security"):
        sandbox = str(config.get("poc_sandbox", POC_DIR)).rstrip("/") + "/"
        if role == "poc-exploit" and not _path_allowed(norm_path, [sandbox, ".review/poc/"], git_root):
            return {
                "permission": "deny",
                "user_message": f"PoC exploit writes limited to {sandbox}.",
                "agent_message": "BLOCKED: poc-exploit sandbox.",
            }

    return {"permission": "allow"}


def check_task_alignment_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    """DEF-02: enforce Task alignment only inside active MAP sessions for managed roles."""
    tool_name = _extract_tool_name(data)
    if tool_name != "Task":
        return {"permission": "allow"}

    ctx = load_map_context(data)
    if ctx is None:
        return {"permission": "allow"}

    config = ctx["config"]
    if not config or not config.get("active") or not config.get("session_id"):
        return {"permission": "allow"}

    workflow = str(config.get("workflow") or "multi-agent-pr")

    if _map_exempt_task(data, workflow):
        return {"permission": "allow"}

    subagent_type = _task_subagent_type(data)
    if not subagent_type:
        return {"permission": "allow"}
    if not _is_map_managed_role(subagent_type, workflow):
        return {"permission": "allow"}

    if subagent_type == "coder" and workflow == "map-hyperplan":
        return {
            "permission": "deny",
            "user_message": "map-hyperplan does not spawn coder.",
            "agent_message": "BLOCKED: map-hyperplan is planning-only; coder forbidden.",
        }

    if subagent_type == "planner" and workflow != "map-hyperplan":
        return {
            "permission": "deny",
            "user_message": "planner agent is only valid for map-hyperplan workflow.",
            "agent_message": "BLOCKED: planner requires workflow=map-hyperplan.",
        }

    progress = ctx["progress"] or {}
    if progress.get("session_id") != config.get("session_id"):
        return {
            "permission": "deny",
            "user_message": "MAP progress session_id mismatch.",
            "agent_message": "BLOCKED: progress.json session_id does not match config.",
        }

    phase = str(progress.get("phase") or "")
    allowed, reason = validate_phase(workflow, phase, subagent_type)
    if not allowed:
        return {
            "permission": "deny",
            "user_message": f"Task spawn blocked: {reason}",
            "agent_message": f"BLOCKED: {reason}",
        }

    if not _role_aligned_with_config(config, subagent_type):
        return {
            "permission": "deny",
            "user_message": "Subagent type not aligned with config.roles.",
            "agent_message": (
                f"BLOCKED: {subagent_type!r} not listed in config.roles for workflow={workflow!r}."
            ),
        }

    return {"permission": "allow"}


def record_subagent_from_hook(data: dict[str, Any], raw: str) -> dict[str, Any]:
    if not _event_looks_like_subagent_stop(data):
        return {
            "followup_message": (
                "Review session was not recorded: record-subagent only accepts Cursor subagentStop payloads."
            )
        }

    subagent_type, model, _ = _extract_subagent_fields(data)
    if not subagent_type:
        return {}

    ctx = load_map_context(data)
    if ctx is None:
        return {
            "followup_message": "Subagent completed but no git worktree found for marker recording."
        }

    git_root = ctx["git_root"]
    branch = ctx["branch"]
    head_sha = ctx["head_sha"]

    payload_fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    marker = {
        "branch": branch,
        "head_sha": head_sha,
        "tree_sha": ctx["tree_sha"],
        "type": subagent_type,
        "model": model or None,
        "completed_at": _now_iso(),
        "source": "cursor-subagentStop",
        "payload_fingerprint": payload_fingerprint,
        "event_keys": sorted(data.keys()),
        "workflow": (ctx["config"] or {}).get("workflow"),
    }
    _write_marker(git_root, branch, head_sha, marker)
    _write_session_summary(git_root, branch, head_sha)

    if subagent_type == "coder":
        return {
            "followup_message": (
                f"Coder recorded under {REVIEW_DIR}/{CANONICAL_SESSION_SUBDIR}/. "
                f"Launch required Reviewer subagent(s) before merge."
            )
        }

    if subagent_type in REVIEWER_TYPES:
        completed = sorted(_completed_types(_marker_payloads(git_root, branch, head_sha), REVIEWER_TYPES))
        return {
            "followup_message": (
                f"Reviewer {subagent_type} recorded. Completed reviewers: {', '.join(completed)}."
            )
        }

    return {}


def _cli_advance_fix_queue() -> int:
    git_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
    resolved = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else None
    increment = "--increment-round" in sys.argv
    head = _git_head(str(git_root)) if git_root.is_dir() else ""
    branch = _git_branch(str(git_root)) if git_root.is_dir() else ""
    result = advance_fix_queue(
        git_root,
        mark_resolved_ids=resolved,
        increment_round=increment,
        head_sha=head or None,
        branch=branch or None,
    )
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def _cli_advance_critic_queue() -> int:
    git_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
    resolved = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else None
    increment = "--increment-round" in sys.argv
    result = advance_critic_queue(
        git_root, mark_resolved_ids=resolved, increment_round=increment
    )
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "advance-fix-queue":
        return _cli_advance_fix_queue()
    if len(sys.argv) > 1 and sys.argv[1] == "advance-critic-queue":
        return _cli_advance_critic_queue()

    mode = sys.argv[1] if len(sys.argv) > 1 else "check-merge"
    raw = sys.stdin.read()
    data = _load_json(raw)

    handlers = {
        "check-merge": lambda: check_merge_from_hook(data),
        "record-subagent": lambda: record_subagent_from_hook(data, raw),
        "stop-check": lambda: stop_check_from_hook(data),
        "session-resume": lambda: session_resume_from_hook(data),
        "set-role": lambda: set_role_from_hook(data),
        "check-tool-permission": lambda: check_tool_permission_from_hook(data),
        "check-task-alignment": lambda: check_task_alignment_from_hook(data),
    }

    handler = handlers.get(mode)
    if handler is None:
        print(json.dumps({"permission": "deny", "user_message": f"Unknown review gate mode: {mode}"}))
        return 1

    print(json.dumps(handler()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
