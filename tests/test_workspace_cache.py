#!/usr/bin/env python3
"""Workspace cache tests for Cursor 3.15+ subagent payload compatibility (F2)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


class WorkspaceCacheTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self._bootstrap_secret(self.rg)
        self.cache_file = self.tmp / "workspace-cache.json"
        os.environ["OMC_WORKSPACE_CACHE_FILE"] = str(self.cache_file)

    def tearDown(self):
        os.environ.pop("OMC_WORKSPACE_CACHE_FILE", None)
        self._clear_secret_env()
        self._tmpdir.cleanup()

    def _subagent_stop_payload(self, conversation_id: str, subagent_type: str = "coder") -> dict:
        """模拟 Cursor 3.15.6 subagentStop：空 workspace_roots、无 cwd。"""
        return {
            "conversation_id": conversation_id,
            "subagent_id": "Task:0-test",
            "subagent_type": subagent_type,
            "hook_event_name": "subagentStop",
            "status": "completed",
            "duration_ms": 1000,
            "workspace_roots": [],
            "transcript_path": "/tmp/transcript.jsonl",
            "agent_transcript_path": None,
        }

    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    def test_subagent_stop_uses_cache_for_marker_recording(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        branch, head = _init_git_repo(repo)
        conv_id = "93aeaa05-dead-beef-cafe-babe00000001"
        _prime_cache(self.rg, self.cache_file, conv_id, repo)

        data = self._subagent_stop_payload(conv_id)
        with patch.object(self.rg.os, "getcwd", return_value=str(self.tmp / "fake-cursor")):
            result = self.rg.record_subagent_from_hook(data, json.dumps(data))

        sess = repo / ".review" / "session" / self.rg._slug(branch) / head
        markers = list(sess.glob("*.json"))
        self.assertTrue(markers, "expected marker under .review/session/")
        marker = json.loads(markers[0].read_text(encoding="utf-8"))
        self.assertEqual(marker["type"], "coder")
        self.assertEqual(marker["branch"], branch)
        self.assertEqual(marker["head_sha"], head)
        self.assertIn("Coder recorded", result.get("followup_message", ""))

    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    def test_cache_miss_returns_no_op(self):
        data = self._subagent_stop_payload("missing-conversation-id")
        with patch.object(self.rg.os, "getcwd", return_value=str(self.tmp / "fake-cursor")):
            result = self.rg.record_subagent_from_hook(data, json.dumps(data))
        self.assertEqual(result.get("followup_message"), "Subagent completed but no git worktree found for marker recording.")

    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    def test_stale_cached_path_not_git_repo_no_op(self):
        conv_id = "93aeaa05-stale-path-no-git"
        not_git = self.tmp / "not-a-repo"
        not_git.mkdir()
        _prime_cache(self.rg, self.cache_file, conv_id, not_git)
        data = self._subagent_stop_payload(conv_id)
        with patch.object(self.rg.os, "getcwd", return_value=str(self.tmp / "fake-cursor")):
            result = self.rg.record_subagent_from_hook(data, json.dumps(data))
        self.assertIn("no git worktree", result.get("followup_message", ""))

    def test_ttl_prunes_entries_older_than_seven_days_on_write(self):
        repo = self.tmp / "repo_ttl"
        repo.mkdir()
        _init_git_repo(repo)
        conv_old = "old-conversation"
        conv_new = "new-conversation"
        stale_ts = (datetime.now(timezone.utc) - timedelta(days=8)).replace(microsecond=0).isoformat()
        self.cache_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        conv_old: {"root": str(repo.resolve()), "updated_at": stale_ts},
                        conv_new: {"root": str(repo.resolve()), "updated_at": self.rg._now_iso()},
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.rg._upsert_workspace_cache("fresh-write", repo)
        cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        entries = cache["entries"]
        self.assertNotIn(conv_old, entries)
        self.assertIn(conv_new, entries)
        self.assertIn("fresh-write", entries)

    def test_pre_tool_use_updates_cache_from_workspace_roots(self):
        repo = self.tmp / "repo_pretool"
        repo.mkdir()
        _init_git_repo(repo)
        conv_id = "93aeaa05-pretool-use-case"
        data = {
            "conversation_id": conv_id,
            "hook_event_name": "preToolUse",
            "workspace_roots": [str(repo.resolve())],
            "cwd": "",
        }
        ctx = self.rg.load_map_context(data)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["git_root"].resolve(), repo.resolve())
        cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertIn(conv_id, cache["entries"])
        self.assertEqual(cache["entries"][conv_id]["root"], str(repo.resolve()))


if __name__ == "__main__":
    unittest.main()
