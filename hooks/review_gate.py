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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

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
# 平台编辑类工具（Cursor 3.15+ StrReplace 与 Write/Delete 同等受角色门约束）
EDIT_TOOLS = frozenset({"Write", "Delete", "StrReplace"})
# 小写连写 MCP 写动词别名（分词器无法从 createpage 拆出 create）
_MCP_WRITE_ALIASES = frozenset({"createpage", "updatepage", "deletepage", "insertpage"})
ROUTING_RULES_FILE = "routing-rules.json"
HOOKS_DIR = Path(__file__).resolve().parent
MODELS_CONFIG_FILE = HOOKS_DIR / "config" / "models.json"
LEGACY_SECRET_FILE = Path.home() / ".cursor" / "hooks" / ".review-gate-secret"
SPECS_DIR = ".specs"


def legacy_secret_file_path() -> Path:
    env = os.environ.get("OMC_LEGACY_SECRET_FILE")
    if env:
        return Path(env).expanduser().resolve()
    return LEGACY_SECRET_FILE
POC_DIR = ".review/poc"


class SecretError(RuntimeError):
    pass


def secret_file_path() -> Path:
    env = os.environ.get("OMC_SECRET_FILE")
    if env:
        return Path(env).expanduser().resolve()
    return HOOKS_DIR / ".review-gate-secret"


WORKSPACE_CACHE_TTL_DAYS = 7


def workspace_cache_file_path() -> Path:
    """Per-conversation git root hint for hooks with empty workspace context (Cursor 3.15+)."""
    env = os.environ.get("OMC_WORKSPACE_CACHE_FILE")
    if env:
        return Path(env).expanduser().resolve()
    return HOOKS_DIR / ".workspace-cache.json"


def _extract_conversation_id(data: dict[str, Any]) -> str:
    for key in ("conversation_id", "conversationId", "session_id", "sessionId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_iso8601(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _empty_workspace_cache() -> dict[str, Any]:
    return {"version": 1, "entries": {}}


def _read_workspace_cache() -> dict[str, Any]:
    try:
        raw = _read_json_file(workspace_cache_file_path())
        if not raw or raw.get("version") != 1:
            return _empty_workspace_cache()
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return _empty_workspace_cache()
        return raw
    except OSError:
        return _empty_workspace_cache()


def _lookup_workspace_cache(conversation_id: str) -> Path | None:
    # Cache is a hint only; markers stay HMAC-sealed and branch/head-scoped.
    if not conversation_id:
        return None
    try:
        entry = _read_workspace_cache().get("entries", {}).get(conversation_id)
        if not isinstance(entry, dict):
            return None
        # G2: 读取侧同样执行 7 天 TTL——过期或缺有效时间戳的条目在查找时直接忽略，
        # 不再等到写入时才 prune。
        updated_at = entry.get("updated_at")
        ts = _parse_iso8601(updated_at) if isinstance(updated_at, str) else None
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=WORKSPACE_CACHE_TTL_DAYS)
        if ts < cutoff:
            return None
        root = entry.get("root")
        if not isinstance(root, str) or not root.strip():
            return None
        cached_root = Path(root).expanduser().resolve()
        if _git_root(str(cached_root)) is None:
            return None
        return cached_root
    except OSError:
        return None


def _upsert_workspace_cache(conversation_id: str, git_root: Path) -> None:
    if not conversation_id:
        return
    try:
        cache = _read_workspace_cache()
        entries = cache.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=WORKSPACE_CACHE_TTL_DAYS)
        pruned: dict[str, Any] = {}
        for cid, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            updated_at = entry.get("updated_at")
            if not isinstance(updated_at, str):
                continue
            ts = _parse_iso8601(updated_at)
            if ts is not None and ts >= cutoff:
                pruned[cid] = entry
        pruned[conversation_id] = {
            "root": str(git_root.resolve()),
            "updated_at": _now_iso(),
        }
        cache["version"] = 1
        cache["entries"] = pruned
        _write_json_file(workspace_cache_file_path(), cache)
    except OSError:
        pass


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
    legacy = legacy_secret_file_path().resolve()
    if dest == legacy:
        return False
    if dest.exists():
        return False
    if not legacy.is_file() or legacy.is_symlink():
        return False
    _validate_secret_stat(legacy)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest.parent, 0o700)
    except OSError:
        pass
    data = _read_secret_bytes(legacy)
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
    legacy = legacy_secret_file_path()
    if legacy.is_file() and not legacy.is_symlink():
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
_SHELL_FD_REDIRECT = r"(?:\d{1,2})?>>?"
SHELL_WRITE_PATTERN = re.compile(
    rf"(?:^|[;&|]\s*)(?:[^\n]*\s)?{_SHELL_FD_REDIRECT}\s*(?!/dev/null\b)"
    r"|\btee\s+(?:-a\s+)?(?!-)\S"
    r"|\bsponge\s+"
    rf"|\b(?:cat|python3?|node|ruby|perl)\b[^\n]*{_SHELL_FD_REDIRECT}\s*"
    r"|<<-?\s*['\"]?\w+['\"]?\s*$"
    r"|\b(?:sed|awk)\b[^\n]*\s-i\b"
    r"|\b(?:cp|mv|install|touch|mkdir)\s+",
    re.I | re.M,
)
_SHELL_OUTPUT_FLAG = re.compile(
    r"(?:^|\s)(?:"
    r"--(?:output(?:File)?|junitxml|log-file|result-log|coverprofile|basetemp)(?:=\S+|\s+\S+)"
    r"|-coverprofile(?:=\S+|\s+\S+)"
    r"|-o\s+\S+"
    r")",
    re.I,
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


def _subagent_type_norm_key(value: str) -> str:
    """Lowercase and strip separators for platform alias comparison."""
    return re.sub(r"[-_]", "", value.lower())


# Cursor 3.15+ may deliver hyphenated/lowercased lifecycle values (e.g. general-purpose).
_SUBAGENT_TYPE_CANONICAL: dict[str, str] = {
    _subagent_type_norm_key(t): t
    for t in (*ALL_RECORDED_TYPES, *MAP_MANAGED_ROLES)
}


def _normalize_subagent_type(raw: str) -> str:
    """Map platform subagent_type aliases to canonical MAP forms."""
    stripped = raw.strip()
    if not stripped:
        return stripped
    return _SUBAGENT_TYPE_CANONICAL.get(_subagent_type_norm_key(stripped), stripped)


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

VALID_TRANSITIONS: dict[str, dict[str, list[str]]] = {
    "multi-agent-pr": {
        "config-confirmed": ["spec-writing"],
        "spec-writing": ["architect-review"],
        "architect-review": ["adjudication"],
        "adjudication": ["coding"],
        "coding": ["testing"],
        "testing": ["review-pending"],
        "review-pending": ["synthesis-complete"],
        "synthesis-complete": ["fix-round-*", "merge-ready", "synthesis-complete"],
        "fix-round-*": ["synthesis-complete", "review-pending"],
        "merge-ready": ["merged", "cleanup"],
    },
    "map-hyperplan": {
        "config-confirmed": ["draft"],
        "draft": ["critics"],
        "critics": ["debate"],
        "debate": ["revise", "accepted"],
        "revise": ["debate", "accepted"],
    },
    "map-security": {
        "config-confirmed": ["scope"],
        "scope": ["hunt"],
        "hunt": ["triage"],
        "triage": ["poc"],
        "poc": ["report"],
    },
    "map-refactor": {
        "config-confirmed": ["analysis"],
        "analysis": ["baseline"],
        "baseline": ["implement"],
        "implement": ["regression"],
        "regression": ["review-pending", "fix-round-*", "regression"],
        "fix-round-*": ["regression"],
        "review-pending": ["synthesis-complete"],
        "synthesis-complete": ["merge-ready", "fix-round-*", "synthesis-complete"],
        "merge-ready": ["merged", "cleanup"],
    },
}


class PhaseTransitionError(Exception):
    def __init__(self, workflow: str, current: str, target: str) -> None:
        self.workflow = workflow
        self.current = current
        self.target = target
        super().__init__(
            f"invalid phase transition: {current!r} -> {target!r} "
            f"in workflow {workflow!r}"
        )


def validate_phase_transition(workflow: str, current: str, target: str) -> bool:
    """Check if a phase transition is allowed. Returns True if valid."""
    transitions = VALID_TRANSITIONS.get(workflow)
    if transitions is None:
        return True
    if not current:
        return True
    allowed: list[str] = []
    matched_current = False
    for phase_key, targets in transitions.items():
        if fnmatch.fnmatch(current, phase_key):
            matched_current = True
            allowed.extend(targets)
    if not matched_current:
        return False
    return any(fnmatch.fnmatch(target, pattern) for pattern in allowed)


def _fix_queue_advance_target_phase(workflow: str) -> str:
    """Return the progress phase to set after advancing the fix queue."""
    if workflow == "map-refactor":
        return "regression"
    return "synthesis-complete"


def safe_transition_phase(
    git_root: Path,
    workflow: str,
    target_phase: str,
    *,
    force: bool = False,
) -> bool:
    """Validate and set phase in progress.json. Returns True if transitioned."""
    progress_path = _review_path(git_root, PROGRESS_FILE)
    progress = _read_json_file(progress_path) or {}
    current = str(progress.get("phase") or "")
    if not force and current and not validate_phase_transition(workflow, current, target_phase):
        raise PhaseTransitionError(workflow, current, target_phase)
    progress["phase"] = target_phase
    progress["updated_at"] = _now_iso()
    _write_json_file(progress_path, progress)
    return True


def repair_phase_state(
    git_root: Path,
    workflow: str,
    progress: dict[str, Any],
    markers: list[dict[str, Any]],
) -> str | None:
    """Detect inconsistent phase state and suggest repair. Returns new phase or None."""
    current = str(progress.get("phase") or "")
    completed = _completed_types(markers, ALL_RECORDED_TYPES)

    if workflow == "multi-agent-pr":
        if current == "coding" and "coder" in completed:
            return "testing"
        if current == "testing" and "tester-writer" in completed:
            return "review-pending"
        if current == "review-pending" and REVIEWER_TYPES.issubset(completed):
            return "synthesis-complete"

    elif workflow == "map-refactor":
        if current == "implement" and "coder" in completed:
            return "regression"
        if current == "regression" and "tester-writer" in completed:
            return "review-pending"

    return None

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
        print(
            f"REVIEW_GATE_TIMEOUT: {' '.join(cmd)} exceeded {_SUBPROCESS_TIMEOUT}s",
            file=sys.stderr,
        )
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


def _extract_explicit_cwd(data: dict[str, Any]) -> str:
    """Payload-carried workspace hint only; '' when the event has none.

    G2: Cursor 3.15+ 的 subagentStop 等事件常带空 workspace_roots 且无 cwd。
    只有事件显式携带的 cwd/roots 才允许回写 workspace cache，防止污染。
    """
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
    return ""


def _extract_cwd(data: dict[str, Any]) -> str:
    explicit = _extract_explicit_cwd(data)
    if explicit:
        return explicit
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

    if subagent_type:
        subagent_type = _normalize_subagent_type(subagent_type)

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
    root = _git_cached(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    return Path(root) if root else None


def _git_branch(cwd: str) -> str:
    return _git_cached(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"])


def _git_head(cwd: str) -> str:
    return _git_cached(["git", "-C", cwd, "rev-parse", "HEAD"])


def _git_tree(cwd: str) -> str:
    return _git_cached(["git", "-C", cwd, "rev-parse", "HEAD^{tree}"])


def _git_diff_files(cwd: str) -> list[str]:
    bases = ["origin/main", "origin/master", "main", "master", "HEAD~1"]
    for base in bases:
        merge_base = _run_subprocess(["git", "-C", cwd, "merge-base", "HEAD", base])
        if merge_base.returncode != 0 or not merge_base.stdout.strip():
            continue
        result = _run_subprocess(
            ["git", "-C", cwd, "diff", "--name-only", os.fsdecode(merge_base.stdout).strip(), "HEAD"]
        )
        if result.returncode == 0:
            return [line.strip() for line in os.fsdecode(result.stdout).splitlines() if line.strip()]
    result = _run_subprocess(["git", "-C", cwd, "diff", "--name-only", "HEAD~1", "HEAD"])
    if result.returncode == 0:
        return [line.strip() for line in os.fsdecode(result.stdout).splitlines() if line.strip()]
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
    conversation_id = _extract_conversation_id(data)
    explicit_cwd = _extract_explicit_cwd(data)
    git_root: Path | None = None
    effective_cwd = ""
    if explicit_cwd:
        # 事件自带 workspace 上下文：以它为准，且允许回写缓存。
        git_root = _git_root(explicit_cwd)
        effective_cwd = explicit_cwd
    if git_root is None:
        # 空 workspace_roots / 无 cwd 的事件：优先读缓存，避免 hook 进程
        # 自身 cwd 落在别的仓库时把错误 root 写进缓存（G2 防污染）。
        cached = _lookup_workspace_cache(conversation_id)
        if cached is not None:
            git_root = cached
            effective_cwd = str(cached)
    if git_root is None and not explicit_cwd:
        # 最后手段：hook 进程自身 cwd。只用于解析，绝不回写缓存。
        fallback_cwd = os.getcwd()
        git_root = _git_root(fallback_cwd)
        if git_root is not None:
            effective_cwd = fallback_cwd
    if git_root is None:
        return None
    if explicit_cwd:
        _upsert_workspace_cache(conversation_id, git_root)
    branch = _git_branch(str(git_root))
    head_sha = _git_head(str(git_root))
    if not branch or not head_sha:
        return None
    return {
        "cwd": effective_cwd,
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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
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
            return _normalize_subagent_type(value.strip())
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


def _validated_poc_sandbox(git_root: Path, config: dict[str, Any]) -> str:
    """Repo-relative POSIX prefix (trailing slash) for a validated PoC sandbox."""
    default = POC_DIR.rstrip("/") + "/"
    raw = config.get("poc_sandbox", POC_DIR)
    if not isinstance(raw, str) or not raw.strip():
        return default
    if ".." in raw.replace("\\", "/"):
        return default

    try:
        root = git_root.resolve()
        poc_base = (root / REVIEW_DIR / "poc").resolve()
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
            candidate = Path(normalized).resolve()
        else:
            candidate = (root / normalized).resolve()
        candidate.relative_to(poc_base)
        rel = candidate.relative_to(root)
    except (OSError, ValueError):
        return default

    posix = str(rel).replace("\\", "/")
    return posix.rstrip("/") + "/"


_SHELL_REDIRECT_TARGET = re.compile(rf"{_SHELL_FD_REDIRECT}\s*([^\s;|&]+)")


def _shell_redirect_target_unsafe(target: str) -> bool:
    if not target:
        return True
    if re.search(r"[\$`]", target):
        return True
    if ".." in target.replace("\\", "/"):
        return True
    if target.count('"') % 2 != 0 or target.count("'") % 2 != 0:
        return True
    return False


def _shell_redirects_to_poc(
    command: str, git_root: Path, config: dict[str, Any]
) -> bool:
    """True only when every redirect in command targets the validated PoC sandbox."""
    sandbox = _validated_poc_sandbox(git_root, config)
    root = git_root.resolve()
    sandbox_path = (root / sandbox.rstrip("/")).resolve()
    found = False
    for match in _SHELL_REDIRECT_TARGET.finditer(command):
        raw_target = match.group(1).strip()
        if _shell_redirect_target_unsafe(raw_target):
            return False
        target = raw_target.strip('"').strip("'")
        try:
            if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
                candidate = Path(target).resolve()
            else:
                candidate = (root / target).resolve()
            candidate.relative_to(sandbox_path)
            found = True
        except (OSError, ValueError):
            return False
    return found


def _strip_shell_redirects(command: str) -> str:
    return _SHELL_REDIRECT_TARGET.sub("", command)


_POC_SHELL_REDIRECT_ALLOWED = re.compile(r"^\s*(?:echo|printf)\b", re.I)


def _poc_redirect_remainder_allowed(remainder: str) -> bool:
    """Only echo/printf may precede a PoC sandbox redirect (fail-closed)."""
    if not remainder.strip():
        return False
    if SHELL_WRITE_PATTERN.search(remainder):
        return False
    if re.search(r"(?<!<)<(?!<)", remainder):
        return False
    return bool(_POC_SHELL_REDIRECT_ALLOWED.match(remainder))


def _poc_shell_has_expansion(command: str) -> bool:
    if re.search(r"\$\(", command):
        return True
    if "`" in command:
        return True
    if re.search(r"<\(", command):
        return True
    return False


def _poc_shell_redirect_only(
    command: str, git_root: Path, config: dict[str, Any]
) -> bool:
    """True only when PoC writes use redirects alone (no cp/mv/tee/heredoc/etc.)."""
    if _SHELL_COMPOUND_SEP.search(command):
        return False
    if not _shell_redirects_to_poc(command, git_root, config):
        return False
    if _poc_shell_has_expansion(command):
        return False
    remainder = _strip_shell_redirects(command)
    if not _poc_redirect_remainder_allowed(remainder):
        return False
    return True


_SHELL_COMPOUND_SEP = re.compile(
    r"(?:;|&&|\|\||(?<!\|)\|(?!\|)|(?<!&)&(?!&)|\n)"
)


def _shell_has_output_flag(command: str) -> bool:
    return bool(_SHELL_OUTPUT_FLAG.search(command))


def _shell_readonly_safe(command: str) -> bool:
    return (
        _shell_looks_readonly(command)
        and not SHELL_WRITE_PATTERN.search(command)
        and not _shell_has_output_flag(command)
        and not _SHELL_COMPOUND_SEP.search(command)
        and not _poc_shell_has_expansion(command)
    )


def _shell_write_blocked(
    command: str,
    logical_role: str,
    *,
    git_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    if not command.strip() or _shell_looks_readonly(command):
        return False
    if not SHELL_WRITE_PATTERN.search(command):
        return False
    root = git_root or Path.cwd()
    cfg = config if config is not None else {}
    if logical_role == "poc-exploit":
        if _SHELL_COMPOUND_SEP.search(command):
            return True
        if _poc_shell_redirect_only(command, root, cfg):
            return False
    return True


def _code_outside_allowed(
    changed: list[str], allowed_prefixes: list[str], git_root: Path | None = None
) -> list[str]:
    return [p for p in changed if not _path_allowed(p, allowed_prefixes, git_root)]


def _security_code_modified(git_root: Path, config: dict[str, Any]) -> bool:
    changed = _git_diff_files(str(git_root))
    sandbox = _validated_poc_sandbox(git_root, config)
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
            "BLOCKED: config tier mismatch with diff inference.",
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
            "BLOCKED: verdict tree_sha mismatch.",
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
                "BLOCKED: Standard/Large requires all three reviewers.",
            )

    completed_implementers = _completed_types(markers, IMPLEMENTER_TYPES)
    if requires_coder_for_tier(tier) and "coder" not in completed_implementers:
        return (
            False,
            f"{tier.title()} requires a coder subagent record.",
            "BLOCKED: use Task(subagent_type='coder').",
        )

    p0: Any = verdict.get("p0")
    p1: Any = verdict.get("p1")
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
    if workflow == "map-hyperplan" and config.get("active"):
        return {
            "permission": "deny",
            "user_message": "map-hyperplan session active; planning-only.",
            "agent_message": "BLOCKED: hyperplan session active; planning-only.",
        }

    ok, user_message, agent_message = validate_review_state(git_root, branch, head_sha)
    if ok:
        return {"permission": "allow"}
    return {"permission": "deny", "user_message": user_message, "agent_message": agent_message}


class QueueManager:
    """Unified queue interface for fix, critic, and security queues."""

    def __init__(self, git_root: Path, queue_file: str) -> None:
        self._path = _review_path(git_root, queue_file)
        self._git_root = git_root

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any] | None:
        return _read_json_file(self._path)

    def save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _now_iso()
        _write_json_file(self._path, data)

    def is_empty(self) -> bool:
        data = self.load()
        if data is None:
            return True
        return not any(self._item_keys_values(data))

    def delete(self) -> None:
        if self._path.is_file():
            self._path.unlink()

    def _item_keys_values(self, data: dict[str, Any]) -> list[list[dict[str, Any]]]:
        raise NotImplementedError

    def _item_count(self, data: dict[str, Any]) -> int:
        return sum(len(v) for v in self._item_keys_values(data))


class FixQueue(QueueManager):
    """Fix queue: tracks P0/P1 issues across fix-review rounds."""

    def __init__(self, git_root: Path) -> None:
        super().__init__(git_root, FIX_QUEUE_FILE)

    def _item_keys_values(self, data: dict[str, Any]) -> list[list[dict[str, Any]]]:
        return [_issue_list(data, "p0_issues"), _issue_list(data, "p1_issues")]

    def p0_count(self) -> int:
        data = self.load()
        if not data:
            return 0
        return len(_issue_list(data, "p0_issues"))

    def p1_count(self) -> int:
        data = self.load()
        if not data:
            return 0
        return len(_issue_list(data, "p1_issues"))

    def total_count(self) -> int:
        return self.p0_count() + self.p1_count()

    def resolve_ids(self, resolved_ids: set[str]) -> None:
        data = self.load()
        if data is None:
            return
        data["p0_issues"] = _filter_resolved_issues(_issue_list(data, "p0_issues"), resolved_ids)
        data["p1_issues"] = _filter_resolved_issues(_issue_list(data, "p1_issues"), resolved_ids)
        if not data["p0_issues"] and not data["p1_issues"]:
            self.delete()
        else:
            self.save(data)

    def increment_round(self) -> int:
        data = self.load() or {}
        new_round = int(data.get("round") or 0) + 1
        data["round"] = new_round
        self.save(data)
        return new_round

    def scope_check(self, branch: str, head_sha: str) -> bool:
        data = self.load()
        if not data:
            return False
        return data.get("branch") == branch and data.get("head_sha") == head_sha


class CriticQueue(QueueManager):
    """Critic queue: tracks pending critic items for hyperplan debate."""

    def __init__(self, git_root: Path) -> None:
        super().__init__(git_root, CRITIC_QUEUE_FILE)

    def _item_keys_values(self, data: dict[str, Any]) -> list[list[dict[str, Any]]]:
        items = data.get("pending_items") or []
        return [items] if isinstance(items, list) else []

    def pending_count(self) -> int:
        data = self.load()
        if not data:
            return 0
        items = data.get("pending_items") or []
        return len(items) if isinstance(items, list) else 0

    def resolve_ids(self, resolved_ids: set[str]) -> None:
        data = self.load()
        if data is None:
            return
        pending = data.get("pending_items") or []
        if not isinstance(pending, list):
            pending = []
        data["pending_items"] = _filter_critic_pending(pending, resolved_ids)
        if not data["pending_items"]:
            self.delete()
        else:
            self.save(data)

    def increment_round(self) -> int:
        data = self.load() or {}
        new_round = int(data.get("round") or 1) + 1
        data["round"] = new_round
        self.save(data)
        return new_round


class SecurityQueue(QueueManager):
    """Security queue: tracks unverified High+ findings with fingerprint dedup."""

    def __init__(self, git_root: Path) -> None:
        super().__init__(git_root, SECURITY_QUEUE_FILE)

    def _item_keys_values(self, data: dict[str, Any]) -> list[list[dict[str, Any]]]:
        items = data.get("pending_findings") or []
        return [items] if isinstance(items, list) else []

    def pending_count(self) -> int:
        data = self.load()
        if not data:
            return 0
        items = data.get("pending_findings") or []
        return len(items) if isinstance(items, list) else 0

    def existing_fingerprints(self) -> set[str]:
        data = self.load()
        if not data:
            return set()
        items = data.get("pending_findings") or []
        return cast(set[str], {item.get("fingerprint") for item in items if isinstance(item, dict)})

    def append_findings(self, findings: list[dict[str, Any]], *, session_id: str) -> int:
        data = self.load() or {
            "schema_version": 1,
            "session_id": session_id,
            "pending_findings": [],
        }
        pending = data.get("pending_findings") or []
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
        data["pending_findings"] = pending
        self.save(data)
        return added


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
    force_phase: bool = False,
) -> dict[str, Any]:
    """After a fix-review cycle: drop resolved issues, reset phase for stop hook."""
    fix_q = FixQueue(git_root)
    fix_queue = fix_q.load()
    if fix_queue is None:
        return {"ok": False, "reason": "fix-queue missing"}

    resolved = {str(i) for i in (mark_resolved_ids or [])}
    # 预演剩余条目以确定队列动作与返回计数；真正的过滤/轮次改写必须
    # 委托给 FixQueue 类方法，保证语义只有一处实现（G4）。
    p0 = _filter_resolved_issues(_issue_list(fix_queue, "p0_issues"), resolved)
    p1 = _filter_resolved_issues(_issue_list(fix_queue, "p1_issues"), resolved)
    queue_action = "updated" if (p0 or p1) else "deleted"

    progress_path = _review_path(git_root, PROGRESS_FILE)
    progress = _read_json_file(progress_path) or {}
    config_path = _review_path(git_root, CONFIG_FILE)
    config = _read_json_file(config_path) or {}
    workflow = str(config.get("workflow") or "multi-agent-pr")
    target_phase = _fix_queue_advance_target_phase(workflow)
    current_phase = str(progress.get("phase") or "")
    # G5: 先做阶段迁移校验/写入；一旦抛出 PhaseTransitionError，
    # 队列文件保持原样，不会出现"已解决条目被删但进度未迁移"的丢失。
    if current_phase != target_phase:
        if force_phase:
            print(
                f"WARNING: forcing phase transition {current_phase!r} -> {target_phase!r} "
                f"in workflow {workflow!r}",
                file=sys.stderr,
            )
            safe_transition_phase(git_root, workflow, target_phase, force=True)
        else:
            safe_transition_phase(git_root, workflow, target_phase)

    fix_q.resolve_ids(resolved)
    queue_round: Any = fix_queue.get("round")
    if queue_action == "updated":
        if increment_round:
            queue_round = fix_q.increment_round()
        if head_sha or branch:
            data = fix_q.load() or {}
            if head_sha:
                data["head_sha"] = head_sha
            if branch:
                data["branch"] = branch
            fix_q.save(data)

    progress = _read_json_file(progress_path) or {}
    if head_sha:
        progress["head_sha"] = head_sha
    if branch:
        progress["branch"] = branch
    if increment_round:
        progress["fix_round"] = int(progress.get("fix_round") or 0) + 1
    progress["updated_at"] = _now_iso()
    _write_json_file(progress_path, progress)

    if queue_action != "updated":
        queue_round = None
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


def _filter_critic_pending(
    pending: list[Any], resolved_ids: set[str]
) -> list[dict[str, Any]]:
    """过滤已解决的 critic 条目；CriticQueue 与 advance 预览共用同一实现。"""
    kept: list[dict[str, Any]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("dimension") or "")
        if item_id and item_id in resolved_ids:
            continue
        kept.append(item)
    return kept


def advance_critic_queue(
    git_root: Path,
    *,
    mark_resolved_ids: list[str] | None = None,
    increment_round: bool = False,
) -> dict[str, Any]:
    """Hyperplan: remove resolved critic items; optionally bump round."""
    critic_q = CriticQueue(git_root)
    critic_queue = critic_q.load()
    if critic_queue is None:
        return {"ok": False, "reason": "critic-queue missing"}

    resolved = {str(i) for i in (mark_resolved_ids or [])}
    # 预演 kept 以确定目标阶段与验收门槛；队列改写委托给 CriticQueue（G4）。
    pending = critic_queue.get("pending_items") or []
    if not isinstance(pending, list):
        pending = []
    kept = _filter_critic_pending(pending, resolved)
    queue_round = critic_queue.get("round")

    config_path = _review_path(git_root, CONFIG_FILE)
    config = _read_json_file(config_path) or {}

    if not kept and str(config.get("workflow") or "") == "map-hyperplan":
        try:
            debate_round = int(queue_round) if queue_round is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid critic queue round"}
        debate_path = _latest_debate_report(git_root, debate_round)
        if debate_path is None:
            return {"ok": False, "reason": "debate report missing for hyperplan acceptance"}
        debate_data = _read_json_file(debate_path)
        if debate_data is None:
            return {"ok": False, "reason": "debate report unreadable"}
        debate_ok, debate_msg = validate_debate_report(
            debate_data, require_nonempty_claims=True
        )
        if not debate_ok:
            return {"ok": False, "reason": f"debate report invalid: {debate_msg}"}

    progress_path = _review_path(git_root, PROGRESS_FILE)
    progress = _read_json_file(progress_path) or {}
    target_phase = "revise" if kept else "accepted"
    current_phase = str(progress.get("phase") or "")
    # G5: 阶段迁移先于队列落盘；迁移抛错时队列文件保持原样。
    if current_phase != target_phase:
        safe_transition_phase(git_root, "map-hyperplan", target_phase)

    critic_q.resolve_ids(resolved)
    action = "updated" if kept else "deleted"
    if increment_round and kept:
        critic_q.increment_round()

    progress = _read_json_file(progress_path) or {}
    result: dict[str, Any] = {
        "ok": True,
        "queue_action": action,
        "pending_remaining": len(kept),
        "phase": progress["phase"],
    }

    if not kept:
        fresh_config = _read_json_file(config_path)
        if fresh_config and str(fresh_config.get("workflow") or "") == "map-hyperplan":
            queue_session_id = critic_queue.get("session_id")
            config_session_id = fresh_config.get("session_id")
            if queue_session_id and queue_session_id != config_session_id:
                result["deactivate_skipped"] = "session_mismatch"
            else:
                fresh_config["active"] = False
                fresh_config["deactivated_at"] = _now_iso()
                _write_json_file(config_path, fresh_config)
                result["deactivated"] = True

    return result


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


def _latest_debate_report(git_root: Path, round: int | None = None) -> Path | None:
    reports_dir = _review_path(git_root, "reports")
    if not reports_dir.is_dir():
        return None
    if round is not None:
        preferred = reports_dir / f"debate-round-{round}.json"
        return preferred if preferred.is_file() else None
    candidates = sorted(
        reports_dir.glob("debate-round-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def validate_debate_report(
    data: dict[str, Any], *, require_nonempty_claims: bool = False
) -> tuple[bool, str]:
    required_lists = ("claims", "counterclaims", "evidence", "unresolved", "consensus_items")
    for key in required_lists:
        value = data.get(key)
        if not isinstance(value, list):
            return False, f"missing or invalid list field: {key}"
    for scalar in ("round", "session_id"):
        if not data.get(scalar):
            return False, f"missing field: {scalar}"
    if require_nonempty_claims and not data.get("claims"):
        return False, "claims must be non-empty for hyperplan acceptance"
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
        total = sum(int(line.split(":")[-1]) for line in os.fsdecode(result.stdout).splitlines() if ":" in line)
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
    hyperplan_signals = re.search(
        r"(hyperplan|map-hyperplan|写个\s*spec|架构|方案|debate)", user_text, re.I
    )
    if hyperplan_signals and not (config and config.get("active")):
        hints.append(
            "Hyperplan detected without active MAP session: run AskQuestion, "
            "write .review/config.json (workflow=map-hyperplan, active=true), "
            "phase=config-confirmed before spawning critics."
        )
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


_FALLBACK_REVIEWERS: dict[str, str] = {
    "reviewer-grok": "grok-4.5",
    "reviewer-codex": "gpt-5.3-codex-high-fast",
    "reviewer-gemini": "gemini-3.1-pro",
}

_models_cache: dict[str, Any] | None = None


def _load_models_config() -> dict[str, Any]:
    global _models_cache
    if _models_cache is not None:
        return _models_cache
    try:
        with open(MODELS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _models_cache = data
    except (OSError, json.JSONDecodeError):
        _models_cache = {"reviewers": {k: {"model": v} for k, v in _FALLBACK_REVIEWERS.items()}, "roles": {}}
    return _models_cache


def _reset_models_cache() -> None:
    global _models_cache
    _models_cache = None


def _build_reviewer_model_map() -> dict[str, str]:
    spawn = _build_reviewer_spawn_models()
    return {model: role for role, model in spawn.items()}


def _build_reviewer_spawn_models() -> dict[str, str]:
    cfg = _load_models_config()
    reviewers = cfg.get("reviewers", {})
    if not isinstance(reviewers, dict):
        reviewers = {}
    result: dict[str, str] = {}
    for role, fallback_model in _FALLBACK_REVIEWERS.items():
        info = reviewers.get(role, {})
        model = info.get("model", "") if isinstance(info, dict) else ""
        result[role] = model if model else fallback_model
    return result


def _infer_reviewer_logical_role(model: str, prompt: str) -> str | None:
    """Infer MAP reviewer logical_role from generalPurpose spawn prompt and/or model."""
    text = prompt or ""
    match = re.search(r"logical_role:\s*reviewer-(\w+)", text, re.I)
    if match:
        return f"reviewer-{match.group(1).lower()}"
    match = re.search(r"Reviewer-(Grok|Codex|Gemini)", text, re.I)
    if match:
        return f"reviewer-{match.group(1).lower()}"
    if model in _build_reviewer_model_map():
        return _build_reviewer_model_map()[model]
    return None


def _subagent_start_prompt(data: dict[str, Any]) -> str:
    for key in ("prompt", "user_message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return _task_prompt(data)


def _subagent_start_model(data: dict[str, Any], fallback: str = "") -> str:
    tool_input = _extract_task_tool_input(data)
    for key in ("model", "agent_model"):
        for source in (tool_input, data):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _reviewer_spawn_template(logical_role: str) -> str:
    model = _build_reviewer_spawn_models().get(logical_role, "<reviewer-model>")
    engine = logical_role.removeprefix("reviewer-").capitalize()
    return (
        f'Task(subagent_type="generalPurpose", model="{model}", readonly=true, '
        f'prompt="You are MAP Reviewer-{engine}. logical_role: {logical_role}\\n...")'
    )


def _infer_role_from_transcript(transcript_path: str) -> str:
    """从 transcript 路径推断 logical_role；词边界匹配，避免 encoder 误命中 coder。"""
    lowered = transcript_path.lower().replace("\\", "/")
    normalized = lowered.replace("-", "")
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
        token = role.replace("-", "")
        # 非字母数字视为边界，防止子串误匹配（如 encoder 含 coder）
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized):
            return role if role != "generalpurpose" else "generalPurpose"
    return ""



def _infer_logical_role_from_prompt(prompt: str) -> str:
    """从 spawn prompt 解析 `logical_role: <role>`（含 coder / reviewer-*）。"""
    if not prompt:
        return ""
    match = re.search(r"logical_role:\s*([A-Za-z0-9_-]+)", prompt, re.I)
    if not match:
        return ""
    role = match.group(1).strip()
    if role.lower() == "generalpurpose":
        return "generalPurpose"
    return role

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

    spawn_prompt = _subagent_start_prompt(data)
    prompt_role = _infer_logical_role_from_prompt(spawn_prompt)
    # prompt 仅可填充空角色或 generalPurpose；不得覆盖 config/payload 已定的具体角色
    if prompt_role and (not logical_role or logical_role == "generalPurpose"):
        logical_role = prompt_role

    if subagent_type == "generalPurpose":
        spawn_model = _subagent_start_model(data, model)
        inferred_reviewer = _infer_reviewer_logical_role(spawn_model, spawn_prompt)
        # 显式/配置/prompt 的 logical_role 优先；仅空或仍为 generalPurpose 时才用模型推断
        if inferred_reviewer and (not logical_role or logical_role == "generalPurpose"):
            logical_role = inferred_reviewer
            if spawn_model and not model:
                model = spawn_model
        elif spawn_model and not model:
            model = spawn_model

    progress = ctx["progress"] or {}
    config_session = str(config.get("session_id") or "") if config else ""
    conv_id = _extract_conversation_id(data)

    role_file = _review_path(ctx["git_root"], ROLES_DIR) / f"{_slug(subagent_id)}.json"
    payload = {
        "schema_version": 1,
        "subagent_id": subagent_id,
        "role": subagent_type,
        "logical_role": logical_role or subagent_type,
        "subagent_type": subagent_type,
        "model": model or None,
        "workflow": config.get("workflow"),
        "session_id": config_session or None,
        "conversation_id": conv_id or None,
        "active": True,
        "started_at": _now_iso(),
        "capability_class": _role_capability_class(logical_role or subagent_type),
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


def _role_capability_class(logical_role: str) -> str:
    """将 logical_role 归为权限类别，供 active-scan 冲突检测。"""
    role = (logical_role or "").strip()
    if role.startswith("reviewer-") or role in {"explore", "planner"}:
        return "readonly"
    if role in {"coder", "tester-writer", "poc-exploit"}:
        return "implementer"
    if role in {"architect"}:
        return "spec"
    if role == "generalPurpose":
        return "general"
    return "other"


def _iter_active_role_files(git_root: Path) -> list[dict[str, Any]]:
    """读取 .review/roles 下仍标记 active 的角色文件。"""
    roles_dir = _review_path(git_root, ROLES_DIR)
    if not roles_dir.is_dir():
        return []
    active: list[dict[str, Any]] = []
    for path in sorted(roles_dir.glob("*.json")):
        data = _read_json_file(path)
        if not isinstance(data, dict):
            continue
        if data.get("active") is False:
            continue
        # 旧文件无 active 字段：视为 active（兼容升级前角色文件）
        active.append(data)
    return active


def _resolve_active_role_scan(
    git_root: Path, data: dict[str, Any], config: dict[str, Any] | None
) -> dict[str, Any] | None:
    """无 subagent_id 时扫描 active 角色；仅唯一可解析时返回。

    Cursor 3.15：子代理 preToolUse 不带 subagent_id，但带独立 model /
    conversation_id。优先按 model 唯一匹配；否则要求单一 active 或
    同一 logical_role；冲突则 fail-closed（返回 None）。
    """
    actives = _iter_active_role_files(git_root)
    if not actives:
        return None
    session_id = str((config or {}).get("session_id") or "")
    # 有 session_id 时始终按会话过滤；无匹配则 fail-closed，不回退到全量 actives
    if session_id:
        # 配置有 session_id 时严格匹配；缺 session_id 的旧角色不得参与 scan
        actives = [
            r
            for r in actives
            if str(r.get("session_id") or "") == session_id
        ]

    event_model = ""
    for key in ("model", "agent_model", "subagent_model"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            event_model = value.strip()
            break

    if event_model:
        by_model = [r for r in actives if str(r.get("model") or "") == event_model]
        if len(by_model) == 1:
            return by_model[0]
        if len(by_model) > 1:
            return None  # 同 model 并行冲突

    if len(actives) == 1:
        return actives[0]

    roles = {
        str(r.get("logical_role") or r.get("role") or "")
        for r in actives
    }
    roles.discard("")
    if len(roles) == 1:
        return actives[0]
    return None


def _deactivate_role_file(git_root: Path, subagent_id: str) -> None:
    """subagentStop 时按 id 将角色标记为 inactive。"""
    if not subagent_id:
        return
    role_file = _review_path(git_root, ROLES_DIR) / f"{_slug(subagent_id)}.json"
    role_data = _read_json_file(role_file)
    if not isinstance(role_data, dict):
        return
    role_data = dict(role_data)
    role_data["active"] = False
    role_data["stopped_at"] = _now_iso()
    _write_json_file(role_file, role_data)


def _role_for_permission(ctx: dict[str, Any], data: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    git_root = ctx["git_root"]
    config = ctx.get("config") if isinstance(ctx.get("config"), dict) else None
    # Commander-first：父会话绝不走 active-scan，避免继承子代理角色
    if _is_commander_session(data):
        return "", None

    subagent_id = _extract_subagent_fields(data)[2]
    role_data: dict[str, Any] | None = None
    if subagent_id:
        role_data = _read_json_file(_review_path(git_root, ROLES_DIR) / f"{_slug(subagent_id)}.json")
        if isinstance(role_data, dict):
            # 已停用的 subagent_id 不得串台到 active-scan 上的其他角色
            if role_data.get("active") is False:
                return "", None
            # active（或缺 active 字段视为 active）：直接使用该文件
        else:
            # 角色文件缺失或非 dict：fail-closed，禁止回落到 scan / transcript
            return "", None
    else:
        # 仅无 subagent_id 时才允许 active-scan 与 transcript 推断
        if config and config.get("active"):
            role_data = _resolve_active_role_scan(git_root, data, config)
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


def _is_root_transcript_path(transcript: str) -> bool:
    """True only for the genuine Cursor root-conversation transcript shape.

    G3: 根会话 transcript 形态为
    ``<Cursor projects 目录>/agent-transcripts/<id>/<id>.jsonl``
    （文件名去掉 .jsonl 必须与父目录同名，且位于 .cursor/projects 之下）；
    含 /subagents/ 的是子代理，/tmp 之类的任意路径一律不算 Commander。
    """
    norm = transcript.replace("\\", "/")
    if "/subagents/" in norm:
        return False
    parts = [p for p in norm.split("/") if p]
    if len(parts) < 6 or parts[-3] != "agent-transcripts":
        return False
    filename = parts[-1]
    if not filename.endswith(".jsonl"):
        return False
    if filename[: -len(".jsonl")] != parts[-2]:
        return False
    prefix = parts[:-3]
    return any(
        prefix[i] == ".cursor" and prefix[i + 1] == "projects"
        for i in range(len(prefix) - 1)
    )


def _is_commander_session(data: dict[str, Any]) -> bool:
    """True for the parent Commander session (not a subagent transcript)."""
    subagent_type, _, subagent_id = _extract_subagent_fields(data)
    if subagent_id:
        return False
    # 防御纵深：若载荷带非空 subagent_type，绝不可能是 Commander
    if isinstance(subagent_type, str) and subagent_type.strip():
        return False
    transcript = _extract_transcript_path(data)
    if not transcript:
        return False
    return _is_root_transcript_path(transcript)


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
    git_root = ctx["git_root"]

    if tool_name == "Shell":
        if role == "poc-exploit":
            if DANGEROUS_SHELL_PATTERN.search(command):
                return {
                    "permission": "deny",
                    "user_message": "Dangerous shell command blocked for security hunter/PoC role.",
                    "agent_message": "BLOCKED: use .review/poc/ sandbox only.",
                }
            if not (
                _poc_shell_redirect_only(command, git_root, config)
                or _shell_readonly_safe(command)
            ):
                return {
                    "permission": "deny",
                    "user_message": "PoC shell limited to read-only commands or echo/printf redirects into .review/poc/.",
                    "agent_message": "BLOCKED: poc-exploit shell allowlist.",
                }
        elif role.startswith("reviewer-") or role in {
            "explore",
            "planner",
            "generalPurpose",
        }:
            if not _shell_readonly_safe(command):
                return {
                    "permission": "deny",
                    "user_message": "Shell file-write pattern blocked for read-only MAP role.",
                    "agent_message": "BLOCKED: use allowed Write tool paths instead of shell redirects.",
                }
        if role == "explore" and DANGEROUS_SHELL_PATTERN.search(command):
            return {
                "permission": "deny",
                "user_message": "Dangerous shell command blocked for security hunter/PoC role.",
                "agent_message": "BLOCKED: use .review/poc/ sandbox only.",
            }
        elif role == "" and not _is_commander_session(data):
            # 未识别角色：Shell 写路径 fail-closed（与 #22 空角色逃逸对齐）
            if not _shell_readonly_safe(command):
                return {
                    "permission": "deny",
                    "user_message": "Shell file-write blocked: no MAP role assigned.",
                    "agent_message": (
                        "BLOCKED: unidentified session cannot write via Shell; "
                        "spawn a coder subagent or use a read-only command."
                    ),
                }

    if tool_name not in EDIT_TOOLS:
        return {"permission": "allow"}

    norm_path = path.replace("\\", "/")

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

    if role == "":
        if _is_commander_session(data):
            if _path_allowed(norm_path, [".review/", ".specs/"], git_root):
                return {"permission": "allow"}
            return {
                "permission": "deny",
                "user_message": "Commander may only Write/Delete under .review/ and .specs/.",
                "agent_message": (
                    "BLOCKED: Commander cannot edit implementation files; "
                    "spawn a coder subagent for hooks/, scripts/, tests/, etc."
                ),
            }
        return {
            "permission": "deny",
            "user_message": "Write/Delete/StrReplace blocked: no MAP role assigned.",
            "agent_message": (
                "BLOCKED: subagent without role file cannot Write/Delete/StrReplace. "
                "Commander may write .review/ and .specs/ only."
            ),
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
        sandbox = _validated_poc_sandbox(git_root, config)
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

    subagent_type = _task_subagent_type(data)
    if subagent_type.startswith("reviewer-"):
        template = _reviewer_spawn_template(subagent_type)
        return {
            "permission": "deny",
            "user_message": (
                f"Cursor Task does not support subagent_type={subagent_type!r}. "
                f"Use generalPurpose with readonly=true instead."
            ),
            "agent_message": (
                f"BLOCKED: Task(subagent_type={subagent_type!r}) is invalid. "
                f"Spawn reviewers via platform type generalPurpose + readonly + prompt seat. "
                f"Example: {template}"
            ),
        }

    if _map_exempt_task(data, workflow):
        return {"permission": "allow"}
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

    subagent_type, model, subagent_id = _extract_subagent_fields(data)
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

    logical_role = subagent_type
    if subagent_id:
        role_data = _read_json_file(
            _review_path(git_root, ROLES_DIR) / f"{_slug(subagent_id)}.json"
        )
        if role_data:
            logical_role = str(
                role_data.get("logical_role") or role_data.get("role") or subagent_type
            )

    marker_type = (
        logical_role
        if logical_role in ALL_RECORDED_TYPES or logical_role.startswith("reviewer-")
        else subagent_type
    )

    payload_fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    marker = {
        "branch": branch,
        "head_sha": head_sha,
        "tree_sha": ctx["tree_sha"],
        "type": marker_type,
        "model": model or None,
        "completed_at": _now_iso(),
        "source": "cursor-subagentStop",
        "payload_fingerprint": payload_fingerprint,
        "event_keys": sorted(data.keys()),
        "workflow": (ctx["config"] or {}).get("workflow"),
    }
    _write_marker(git_root, branch, head_sha, marker)
    _write_session_summary(git_root, branch, head_sha)
    # 按 subagent_id 失活角色，避免 stop 后 active-scan 仍命中陈旧角色
    if subagent_id:
        _deactivate_role_file(git_root, subagent_id)

    if marker_type == "coder":
        return {
            "followup_message": (
                f"Coder recorded under {REVIEW_DIR}/{CANONICAL_SESSION_SUBDIR}/. "
                f"Launch required Reviewer subagent(s) before merge."
            )
        }

    if marker_type in REVIEWER_TYPES:
        completed = sorted(_completed_types(_marker_payloads(git_root, branch, head_sha), REVIEWER_TYPES))
        return {
            "followup_message": (
                f"Reviewer {marker_type} recorded. Completed reviewers: {', '.join(completed)}."
            )
        }

    return {}


def compact_inject(data: dict[str, Any]) -> dict[str, Any]:
    """preCompact hook: inject MAP state summary before context compaction."""
    ctx = load_map_context(data)
    if ctx is None:
        return {}
    git_root = ctx["git_root"]
    config = ctx["config"]
    progress = ctx["progress"]
    if not config or not config.get("active"):
        return {}
    parts: list[str] = []
    workflow = config.get("workflow", "multi-agent-pr")
    session_id = config.get("session_id", "?")
    phase = progress.get("phase") if progress else "?"
    completed = progress.get("completed") if progress else []
    parts.append(f"[MAP] workflow={workflow} session={session_id} phase={phase}")
    if completed:
        parts.append(f"  completed subagents: {', '.join(str(c) for c in completed)}")
    for qfile, label in [
        (FIX_QUEUE_FILE, "fix-queue"),
        (CRITIC_QUEUE_FILE, "critic-queue"),
        (SECURITY_QUEUE_FILE, "security-queue"),
    ]:
        qdata = _read_json_file(_review_path(git_root, qfile))
        if qdata is not None:
            parts.append(f"  {label}: active (see .review/{qfile})")
    return {"additional_context": "\n".join(parts)}


_MCP_WRITE_VERBS = frozenset({
    "write",
    "create",
    "update",
    "delete",
    "insert",
    "remove",
    "edit",
    "modify",
    "put",
    "patch",
    "append",
    "set",
    "add",
})


def _tokenize_mcp_tool_name(tool_name: str) -> list[str]:
    """Split MCP tool names on separators and camelCase boundaries."""
    if not tool_name:
        return []
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tool_name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    tokens: list[str] = []
    for chunk in re.split(r"[_\-.]+", spaced):
        for part in chunk.split():
            if part:
                tokens.append(part.lower())
    return tokens


def _mcp_tool_is_write(tool_name: str) -> bool:
    # 全小写连写动词靠显式别名表；其余按分隔符/camelCase 分词命中动词表。
    lowered = (tool_name or "").lower()
    if lowered in _MCP_WRITE_ALIASES:
        return True
    return any(token in _MCP_WRITE_VERBS for token in _tokenize_mcp_tool_name(tool_name))


def check_mcp_permission_from_hook(data: dict[str, Any]) -> dict[str, Any]:
    """beforeMCPExecution hook: block write MCP calls for read-only roles."""
    ctx = load_map_context(data)
    if ctx is None:
        return {"permission": "allow"}
    config = ctx["config"]
    # 与 Write/Shell 门一致：MAP 未激活时不拦截 MCP
    if not config or not config.get("active"):
        return {"permission": "allow"}
    tool_name = _extract_tool_name(data)
    if not _mcp_tool_is_write(tool_name):
        return {"permission": "allow"}
    # 与 Write 门共用角色解析（含 active-scan）；外部 MCP 不会再经本地 Write 兜底
    role, _role_data = _role_for_permission(ctx, data)
    if role.startswith("reviewer-") or role in ("explore", "planner"):
        return {
            "permission": "deny",
            "user_message": f"MAP: role {role!r} is read-only. MCP tool {tool_name!r} would write data.",
        }
    if role == "" and not _is_commander_session(data):
        return {
            "permission": "deny",
            "user_message": f"MAP: no role assigned. MCP tool {tool_name!r} would write data.",
        }
    return {"permission": "allow"}


_git_cache: dict[str, str] = {}


def _git_cached(cmd: list[str], cwd: str | None = None) -> str:
    cache_key = json.dumps(cmd) + "|" + str(cwd or "")
    if cache_key in _git_cache:
        return _git_cache[cache_key]
    try:
        result = _run_subprocess(cmd, cwd=cwd)
        value = os.fsdecode(result.stdout).strip() if result.returncode == 0 else ""
    except OSError:
        value = ""
    _git_cache[cache_key] = value
    return value


def _clear_git_cache() -> None:
    _git_cache.clear()


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
        "compact-inject": lambda: compact_inject(data),
        "check-mcp-permission": lambda: check_mcp_permission_from_hook(data),
    }

    handler = handlers.get(mode)
    if handler is None:
        print(json.dumps({"permission": "deny", "user_message": f"Unknown review gate mode: {mode}"}))
        return 1

    result = handler()
    _clear_git_cache()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
