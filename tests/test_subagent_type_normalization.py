#!/usr/bin/env python3
"""F12: Cursor 3.15 subagent_type platform alias normalization tests."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import SecretBootstrapMixin, load_review_gate


def _init_git_repo(root: Path, *, branch: str = "main") -> tuple[str, str]:
    """初始化临时 git 仓库并返回 (branch, head_sha)。"""
    subprocess.run(["git", "init", "-b", branch], cwd=root, check=True, capture_output=True)
    for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True, capture_output=True)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return branch, head


def _prime_cache(rg, cache_path: Path, conversation_id: str, git_root: Path) -> None:
    """写入 workspace cache 条目供 subagentStop 测试使用。"""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    conversation_id: {
                        "root": str(git_root.resolve()),
                        "updated_at": rg._now_iso(),
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class SubagentTypeNormalizationTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self._bootstrap_secret(self.rg)
        self.cache_file = self.tmp / "workspace-cache.json"
        # P2: patch.dict 自动保存/恢复宿主环境变量，避免测试间/外部环境串扰
        self._env_patch = patch.dict(
            os.environ, {"OMC_WORKSPACE_CACHE_FILE": str(self.cache_file)}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._clear_secret_env()
        self._tmpdir.cleanup()

    def test_normalize_general_purpose_aliases(self):
        """平台别名应映射到 canonical generalPurpose。"""
        for alias in ("general-purpose", "General-Purpose", "general_purpose", "GENERALPURPOSE"):
            self.assertEqual(self.rg._normalize_subagent_type(alias), "generalPurpose")

    def test_event_looks_like_subagent_stop_accepts_general_purpose(self):
        """_event_looks_like_subagent_stop 应接受 general-purpose。"""
        with patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False):
            data = {
                "subagent_type": "general-purpose",
                "subagent_id": "Task:0-test",
                "transcript_path": "/tmp/t.jsonl",
            }
            self.assertTrue(self.rg._event_looks_like_subagent_stop(data))

    def test_event_looks_like_subagent_stop_rejects_cursor_guide(self):
        """非 MAP 类型（cursor-guide）应被拒绝。"""
        with patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False):
            data = {
                "subagent_type": "cursor-guide",
                "subagent_id": "Task:0-test",
                "transcript_path": "/tmp/t.jsonl",
            }
            self.assertFalse(self.rg._event_looks_like_subagent_stop(data))

    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    def test_subagent_stop_general_purpose_records_marker(self):
        """general-purpose subagentStop 应写入 marker 文件。"""
        repo = self.tmp / "repo"
        repo.mkdir()
        branch, head = _init_git_repo(repo)
        conv_id = "f12-general-purpose-stop"
        _prime_cache(self.rg, self.cache_file, conv_id, repo)

        data = {
            "conversation_id": conv_id,
            "subagent_id": "Task:0-gp",
            "subagent_type": "general-purpose",
            "hook_event_name": "subagentStop",
            "status": "completed",
            "duration_ms": 500,
            "workspace_roots": [],
            "transcript_path": "/tmp/transcript.jsonl",
        }
        with patch.object(self.rg.os, "getcwd", return_value=str(self.tmp / "fake-cursor")):
            result = self.rg.record_subagent_from_hook(data, json.dumps(data))

        sess = repo / ".review" / "session" / self.rg._slug(branch) / head
        markers = list(sess.glob("*.json"))
        self.assertTrue(markers, "expected marker under .review/session/")
        marker = json.loads(markers[0].read_text(encoding="utf-8"))
        self.assertEqual(marker["type"], "generalPurpose")
        self.assertNotIn("not recorded", result.get("followup_message", "").lower())

    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    def test_subagent_stop_legacy_camelcase_still_records(self):
        """legacy camelCase generalPurpose 仍应正常写入 marker。"""
        repo = self.tmp / "repo_legacy"
        repo.mkdir()
        branch, head = _init_git_repo(repo)
        conv_id = "f12-generalPurpose-stop"
        _prime_cache(self.rg, self.cache_file, conv_id, repo)

        data = {
            "conversation_id": conv_id,
            "subagent_id": "Task:0-legacy",
            "subagent_type": "generalPurpose",
            "hook_event_name": "subagentStop",
            "status": "completed",
            "workspace_roots": [],
            "transcript_path": "/tmp/transcript.jsonl",
        }
        with patch.object(self.rg.os, "getcwd", return_value=str(self.tmp / "fake-cursor")):
            self.rg.record_subagent_from_hook(data, json.dumps(data))

        sess = repo / ".review" / "session" / self.rg._slug(branch) / head
        markers = list(sess.glob("*.json"))
        self.assertTrue(markers)
        marker = json.loads(markers[0].read_text(encoding="utf-8"))
        self.assertEqual(marker["type"], "generalPurpose")

    def test_set_role_resolves_coder_from_general_purpose(self):
        """subagentStart general-purpose + roles.coder=generalPurpose → logical_role=coder。"""
        repo = self.tmp / "repo_role"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / ".review").mkdir(parents=True)
        (repo / ".review" / "config.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "session_id": "s-f12",
                    "workflow": "multi-agent-pr",
                    "roles": {"coder": "generalPurpose"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (repo / ".review" / "progress.json").write_text(
            json.dumps({"session_id": "s-f12", "phase": "coding"}) + "\n",
            encoding="utf-8",
        )

        data = {
            "subagent_type": "general-purpose",
            "subagent_id": "coder-gp-1",
            "cwd": str(repo),
            "workspace_roots": [str(repo.resolve())],
        }
        self.rg.set_role_from_hook(data)

        role_file = repo / ".review" / "roles" / "coder-gp-1.json"
        self.assertTrue(role_file.is_file())
        role = json.loads(role_file.read_text(encoding="utf-8"))
        self.assertEqual(role["subagent_type"], "generalPurpose")
        self.assertEqual(role["logical_role"], "coder")


if __name__ == "__main__":
    unittest.main()
