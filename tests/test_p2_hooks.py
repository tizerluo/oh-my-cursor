#!/usr/bin/env python3
"""Tests for P2 features: compact_inject, check_mcp_permission, git caching."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import load_review_gate, SecretBootstrapMixin


rg = load_review_gate()


def _init_git_repo(root: Path, *, branch: str = "main") -> tuple[str, str]:
    subprocess.run(["git", "init", "-b", branch], cwd=root, check=True, capture_output=True)
    for key, value in (("user.email", "test@example.com"), ("user.name", "Test")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True, capture_output=True)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    return branch, head


class CompactInjectTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._bootstrap_secret(rg)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.branch, self.head = _init_git_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._clear_secret_env()

    def _write_config(self, **overrides) -> None:
        config = {
            "active": True,
            "workflow": "multi-agent-pr",
            "session_id": "sess-001",
        }
        config.update(overrides)
        review_dir = self.root / ".review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "config.json").write_text(
            json.dumps(config) + "\n", encoding="utf-8",
        )

    def test_returns_empty_when_no_context(self):
        result = rg.compact_inject({"cwd": "/nonexistent/path"})
        self.assertEqual(result, {})

    def test_returns_empty_when_inactive(self):
        self._write_config(active=False)
        data = {"cwd": str(self.root)}
        result = rg.compact_inject(data)
        self.assertEqual(result, {})

    def test_injects_state_summary(self):
        self._write_config()
        progress = {"phase": "coding", "completed": ["coder-1", "reviewer-grok-1"]}
        (self.root / ".review" / "progress.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".review" / "progress.json").write_text(
            json.dumps(progress) + "\n", encoding="utf-8",
        )
        data = {"cwd": str(self.root)}
        result = rg.compact_inject(data)
        ctx = result.get("additional_context", "")
        self.assertIn("[MAP]", ctx)
        self.assertIn("multi-agent-pr", ctx)
        self.assertIn("sess-001", ctx)
        self.assertIn("coding", ctx)
        self.assertIn("coder-1", ctx)

    def test_includes_queue_status(self):
        self._write_config()
        (self.root / ".review" / "fix-queue.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".review" / "fix-queue.json").write_text(
            json.dumps({"items": [{"id": "fix-1"}]}) + "\n", encoding="utf-8",
        )
        data = {"cwd": str(self.root)}
        result = rg.compact_inject(data)
        ctx = result.get("additional_context", "")
        self.assertIn("fix-queue", ctx)


class McpPermissionTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._bootstrap_secret(rg)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.branch, self.head = _init_git_repo(self.root)
        self._write_config()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._clear_secret_env()

    def _write_config(self) -> None:
        config = {"active": True, "workflow": "multi-agent-pr", "session_id": "sess-mcp"}
        review_dir = self.root / ".review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "config.json").write_text(
            json.dumps(config) + "\n", encoding="utf-8",
        )

    def _write_role(self, subagent_id: str, logical_role: str) -> None:
        roles_dir = self.root / ".review" / "roles"
        roles_dir.mkdir(parents=True, exist_ok=True)
        slug = rg._slug(subagent_id)
        (roles_dir / f"{slug}.json").write_text(
            json.dumps({"logical_role": logical_role, "subagent_id": subagent_id}) + "\n",
            encoding="utf-8",
        )

    def test_allows_when_no_context(self):
        result = rg.check_mcp_permission_from_hook({"cwd": "/nonexistent"})
        self.assertEqual(result["permission"], "allow")

    def test_allows_read_tool(self):
        self._write_role("agent-1", "reviewer-grok")
        data = {"cwd": str(self.root), "subagent_id": "agent-1", "tool_name": "search_files"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")

    def test_denies_write_tool_for_reviewer(self):
        self._write_role("agent-2", "reviewer-codex")
        data = {"cwd": str(self.root), "subagent_id": "agent-2", "tool_name": "write_file"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("read-only", result.get("user_message", ""))

    def test_denies_write_tool_for_explore(self):
        self._write_role("agent-3", "explore")
        data = {"cwd": str(self.root), "subagent_id": "agent-3", "tool_name": "create_document"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_allows_write_tool_for_coder(self):
        self._write_role("agent-4", "coder")
        data = {"cwd": str(self.root), "subagent_id": "agent-4", "tool_name": "write_file"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")


    def test_allows_write_when_config_inactive(self):
        """MAP 未激活时 MCP 写工具放行（与 Write 门一致）。"""
        config = {"active": False, "workflow": "multi-agent-pr", "session_id": "sess-mcp"}
        review_dir = self.root / ".review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "config.json").write_text(
            json.dumps(config) + "\n", encoding="utf-8",
        )
        data = {"cwd": str(self.root), "tool_name": "write_file"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")

    def test_denies_write_when_no_subagent_id_and_no_role(self):
        """v1.3.1：无角色时 MCP 写不再 fail-open（与 Write 门对齐）。"""
        data = {"cwd": str(self.root), "tool_name": "delete_file"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("no role", result.get("user_message", "").lower())

    def test_denies_createpage_alias_for_reviewer(self):
        self._write_role("agent-cp", "reviewer-grok")
        data = {
            "cwd": str(self.root),
            "subagent_id": "agent-cp",
            "tool_name": "createpage",
        }
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_allows_readonly_tools_with_write_substrings(self):
        """Regression: settings_get, address_lookup, addon_search must not be flagged."""
        self._write_role("agent-5", "reviewer-gemini")
        for tool in ("settings_get", "address_lookup", "addon_search", "dataset_list"):
            data = {"cwd": str(self.root), "subagent_id": "agent-5", "tool_name": tool}
            result = rg.check_mcp_permission_from_hook(data)
            self.assertEqual(result["permission"], "allow", f"{tool} should be allowed")

    def test_denies_actual_write_tools(self):
        self._write_role("agent-6", "reviewer-grok")
        for tool in ("write_file", "create_document", "delete_record", "update_entry"):
            data = {"cwd": str(self.root), "subagent_id": "agent-6", "tool_name": tool}
            result = rg.check_mcp_permission_from_hook(data)
            self.assertEqual(result["permission"], "deny", f"{tool} should be denied")


    def test_blocks_underscore_bypass_names(self):
        self._write_role("agent-7", "reviewer-codex")
        for tool in ("task_create", "notion_create_page", "mcp__notion__create_page", "createPage", "PutItem"):
            data = {"cwd": str(self.root), "subagent_id": "agent-7", "tool_name": tool}
            result = rg.check_mcp_permission_from_hook(data)
            self.assertEqual(result["permission"], "deny", tool)

    def test_camelcase_payload_denies_write(self):
        self._write_role("agent-8", "reviewer-grok")
        data = {"cwd": str(self.root), "subagentId": "agent-8", "toolName": "createPage"}
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

class GitCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        rg._clear_git_cache()

    def tearDown(self) -> None:
        rg._clear_git_cache()

    def test_cached_returns_same_value(self):
        call_count = 0
        original = rg._run_subprocess

        def counting_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(cmd, **kwargs)

        with patch.object(rg, "_run_subprocess", side_effect=counting_run):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)
                (root / "f.txt").write_text("x\n")
                subprocess.run(["git", "add", "f.txt"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

                r1 = rg._git_cached(["git", "-C", str(root), "rev-parse", "HEAD"])
                r2 = rg._git_cached(["git", "-C", str(root), "rev-parse", "HEAD"])
                self.assertEqual(r1, r2)
                self.assertEqual(call_count, 1)

    def test_clear_cache_forces_reexec(self):
        call_count = 0
        original = rg._run_subprocess

        def counting_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(cmd, **kwargs)

        with patch.object(rg, "_run_subprocess", side_effect=counting_run):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)
                (root / "f.txt").write_text("x\n")
                subprocess.run(["git", "add", "f.txt"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

                rg._git_cached(["git", "-C", str(root), "rev-parse", "HEAD"])
                rg._clear_git_cache()
                rg._git_cached(["git", "-C", str(root), "rev-parse", "HEAD"])
                self.assertEqual(call_count, 2)

    def test_git_functions_use_cache(self):
        call_count = 0
        original = rg._run_subprocess

        def counting_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            return original(cmd, **kwargs)

        with patch.object(rg, "_run_subprocess", side_effect=counting_run):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)
                (root / "f.txt").write_text("x\n")
                subprocess.run(["git", "add", "f.txt"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
                cwd = str(root)

                b1 = rg._git_branch(cwd)
                b2 = rg._git_branch(cwd)
                h1 = rg._git_head(cwd)
                h2 = rg._git_head(cwd)
                self.assertEqual(b1, b2)
                self.assertEqual(h1, h2)
                self.assertEqual(call_count, 2)

    def test_oserror_returns_empty(self):
        with patch.object(rg, "_run_subprocess", side_effect=OSError("git not found")):
            result = rg._git_cached(["git", "rev-parse", "HEAD"])
        self.assertEqual(result, "")

    def test_oserror_cached_as_empty(self):
        call_count = 0

        def failing_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            raise OSError("git not found")

        with patch.object(rg, "_run_subprocess", side_effect=failing_run):
            r1 = rg._git_cached(["git", "rev-parse", "HEAD"])
            r2 = rg._git_cached(["git", "rev-parse", "HEAD"])
        self.assertEqual(r1, "")
        self.assertEqual(r2, "")
        self.assertEqual(call_count, 1)


if __name__ == "__main__":
    unittest.main()
