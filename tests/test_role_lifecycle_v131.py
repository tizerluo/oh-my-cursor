#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""v1.3.1：active 角色生命周期与无 subagent_id 时的权限解析。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import load_review_gate


class RoleLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rg = load_review_gate()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".review" / "roles").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_role(self, subagent_id: str, **fields: object) -> Path:
        payload = {
            "schema_version": 1,
            "subagent_id": subagent_id,
            "logical_role": fields.get("logical_role", "coder"),
            "role": fields.get("role", "generalPurpose"),
            "subagent_type": fields.get("subagent_type", "generalPurpose"),
            "model": fields.get("model", "cursor-grok-4.5-high-fast"),
            "session_id": fields.get("session_id", "sess-1"),
            "active": fields.get("active", True),
            "started_at": "2026-08-10T00:00:00Z",
            "capability_class": fields.get("capability_class", "implementer"),
        }
        path = self.root / ".review" / "roles" / f"{self.rg._slug(subagent_id)}.json"
        path.write_text(json.dumps(payload) + "\n")
        return path

    def test_set_role_writes_active_meta(self) -> None:
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
            "progress": {"session_id": "sess-1"},
        }
        data = {
            "subagent_type": "generalPurpose",
            "subagent_id": "call-coder-1",
            "model": "kimi-k3-max",
            "conversation_id": "parent-cid",
            "prompt": "logical_role: coder\nimplement the fix",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            self.rg.set_role_from_hook(data)
        role_path = self.root / ".review" / "roles" / f"{self.rg._slug('call-coder-1')}.json"
        payload = json.loads(role_path.read_text())
        self.assertTrue(payload.get("active"))
        self.assertEqual(payload.get("logical_role"), "coder")
        self.assertEqual(payload.get("model"), "kimi-k3-max")
        self.assertEqual(payload.get("session_id"), "sess-1")

    def test_explicit_coder_not_overwritten_by_reviewer_model(self) -> None:
        """显式 coder 不得被 reviewer 模型映射覆盖。"""
        ctx = {
            "git_root": self.root,
            "config": {
                "active": True,
                "session_id": "sess-1",
                "workflow": "multi-agent-pr",
                "roles": {"coder": "generalPurpose"},
            },
            "progress": {"session_id": "sess-1"},
        }
        # 使用 reviewer-grok 的默认模型名，若错误覆盖会变成 reviewer-grok
        data = {
            "subagent_type": "generalPurpose",
            "subagent_id": "call-coder-2",
            "model": "cursor-grok-4.5-high-fast",
            "prompt": "logical_role: coder\ndo the work",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(
                self.rg,
                "_build_reviewer_model_map",
                return_value={"cursor-grok-4.5-high-fast": "reviewer-grok"},
            ):
                self.rg.set_role_from_hook(data)
        payload = json.loads(
            (self.root / ".review" / "roles" / f"{self.rg._slug('call-coder-2')}.json").read_text()
        )
        self.assertEqual(payload.get("logical_role"), "coder")

    def test_active_scan_resolves_by_unique_model(self) -> None:
        self._write_role("call-a", logical_role="coder", model="kimi-k3-max")
        self._write_role(
            "call-b",
            logical_role="reviewer-grok",
            model="cursor-grok-4.5-high-fast",
            capability_class="readonly",
        )
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"model": "kimi-k3-max", "conversation_id": "agent-uuid-1"}
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "coder")
        self.assertEqual((role_data or {}).get("subagent_id"), "call-a")

    def test_active_scan_conflict_fail_closed(self) -> None:
        self._write_role("call-a", logical_role="coder", model="same-model")
        self._write_role("call-b", logical_role="tester-writer", model="same-model")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"model": "same-model"}
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "")
        self.assertIsNone(role_data)

    def test_commander_skips_active_scan(self) -> None:
        self._write_role("call-a", logical_role="coder", model="kimi-k3-max")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"model": "kimi-k3-max"}
        with patch.object(self.rg, "_is_commander_session", return_value=True):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "")
        self.assertIsNone(role_data)

    def test_deactivate_on_stop(self) -> None:
        self._write_role("call-a", logical_role="coder", model="kimi-k3-max")
        self.rg._deactivate_role_file(self.root, "call-a")
        payload = json.loads(
            (self.root / ".review" / "roles" / f"{self.rg._slug('call-a')}.json").read_text()
        )
        self.assertFalse(payload.get("active"))
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, _ = self.rg._role_for_permission(ctx, {"model": "kimi-k3-max"})
        self.assertEqual(role, "")

    def test_write_allowed_via_active_scan_without_subagent_id(self) -> None:
        self._write_role("call-a", logical_role="coder", model="kimi-k3-max")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {
            "tool_name": "StrReplace",
            "tool_input": {"path": "hooks/review_gate.py"},
            "model": "kimi-k3-max",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")

    def test_empty_role_shell_write_denied(self) -> None:
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {
            "tool_name": "Shell",
            "tool_input": {"command": "echo x > /tmp/out.txt"},
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                with patch.object(self.rg, "_role_for_permission", return_value=("", None)):
                    result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_commander_with_subagent_type_rejected(self) -> None:
        data = {
            "subagent_type": "generalPurpose",
            "transcript_path": (
                "/Users/u/.cursor/projects/p/agent-transcripts/abc/abc.jsonl"
            ),
        }
        self.assertFalse(self.rg._is_commander_session(data))

    def test_extract_transcript_path_accepts_agent_aliases(self) -> None:
        self.assertEqual(
            self.rg._extract_transcript_path({"agent_transcript_path": "/tmp/a.jsonl"}),
            "/tmp/a.jsonl",
        )
        self.assertEqual(
            self.rg._extract_transcript_path({"agentTranscriptPath": "/tmp/b.jsonl"}),
            "/tmp/b.jsonl",
        )


    def test_prompt_cannot_elevate_config_explore_to_coder(self) -> None:
        """config 已映射 explore 时，prompt 不得提权为 coder。"""
        ctx = {
            "git_root": self.root,
            "config": {
                "active": True,
                "session_id": "sess-1",
                "workflow": "multi-agent-pr",
                "roles": {"explore": "explore"},
            },
            "progress": {"session_id": "sess-1"},
        }
        data = {
            "subagent_type": "explore",
            "subagent_id": "call-explore-1",
            "model": "cursor-grok-4.5-high-fast",
            "prompt": "logical_role: coder\njust browse the repo",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            self.rg.set_role_from_hook(data)
        payload = json.loads(
            (self.root / ".review" / "roles" / f"{self.rg._slug('call-explore-1')}.json").read_text()
        )
        self.assertEqual(payload.get("logical_role"), "explore")

    def test_active_scan_session_mismatch_fail_closed(self) -> None:
        """session_id 不匹配时不得回退到旧会话 active 角色。"""
        self._write_role("call-old", logical_role="coder", model="kimi-k3-max", session_id="sess-old")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-NEW", "workflow": "multi-agent-pr"},
        }
        data = {"model": "kimi-k3-max"}
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "")
        self.assertIsNone(role_data)

    def test_active_scan_session_mismatch_write_denied(self) -> None:
        """session 不匹配时 Write 门 fail-closed。"""
        self._write_role("call-old", logical_role="coder", model="kimi-k3-max", session_id="sess-old")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-NEW", "workflow": "multi-agent-pr"},
        }
        data = {
            "tool_name": "StrReplace",
            "tool_input": {"path": "hooks/review_gate.py"},
            "model": "kimi-k3-max",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_inactive_subagent_id_blocks_active_scan_bind(self) -> None:
        """已停用的 subagent_id 不得串台到其他 active coder。"""
        self._write_role("call-stopped", logical_role="explore", model="same-model", active=False)
        self._write_role("call-live", logical_role="coder", model="same-model")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"subagent_id": "call-stopped", "model": "same-model"}
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "")
        self.assertIsNone(role_data)

    def test_inactive_subagent_id_write_denied_no_scan(self) -> None:
        """inactive subagent_id + 其他 active coder 时 Write 拒绝。"""
        self._write_role("call-stopped", logical_role="explore", model="same-model", active=False)
        self._write_role("call-live", logical_role="coder", model="same-model")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {
            "subagent_id": "call-stopped",
            "tool_name": "StrReplace",
            "tool_input": {"path": "hooks/review_gate.py"},
            "model": "same-model",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_missing_subagent_id_file_no_active_scan_bind(self) -> None:
        """幽灵 subagent_id（无角色文件）+ 同 model active coder 时不得 scan 串台。"""
        self._write_role("call-live", logical_role="coder", model="same-model")
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {
            "subagent_id": "call-ghost-missing",
            "tool_name": "StrReplace",
            "tool_input": {"path": "hooks/review_gate.py"},
            "model": "same-model",
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_transcript_encoder_does_not_infer_coder(self) -> None:
        """transcript 路径含 encoder 不得误推断为 coder。"""
        path = (
            "/Users/u/.cursor/projects/p/agent-transcripts/"
            "encoder-session/encoder-session.jsonl"
        )
        self.assertEqual(self.rg._infer_role_from_transcript(path), "")

    def test_active_scan_missing_session_id_strict_none(self) -> None:
        """active 角色缺 session_id 时，config 有 session_id 则 scan 返回 None。"""
        self._write_role(
            "call-no-sess",
            logical_role="coder",
            model="kimi-k3-max",
            session_id=None,
        )
        # 写入时 session_id=None 会被省略；重新写不含 session_id 的 payload
        role_path = self.root / ".review" / "roles" / f"{self.rg._slug('call-no-sess')}.json"
        payload = __import__("json").loads(role_path.read_text())
        payload.pop("session_id", None)
        role_path.write_text(__import__("json").dumps(payload) + "\n")

        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"model": "kimi-k3-max"}
        with patch.object(self.rg, "_is_commander_session", return_value=False):
            role, role_data = self.rg._role_for_permission(ctx, data)
        self.assertEqual(role, "")
        self.assertIsNone(role_data)
        scanned = self.rg._resolve_active_role_scan(self.root, data, ctx["config"])
        self.assertIsNone(scanned)

    def test_mcp_createpage_is_write_and_denied_for_reviewer(self) -> None:
        self.assertTrue(self.rg._mcp_tool_is_write("createpage"))
        self.assertFalse(self.rg._mcp_tool_is_write("settings_get"))
        self._write_role(
            "call-r",
            logical_role="reviewer-grok",
            model="cursor-grok-4.5-high-fast",
            capability_class="readonly",
        )
        ctx = {
            "git_root": self.root,
            "config": {"active": True, "session_id": "sess-1", "workflow": "multi-agent-pr"},
        }
        data = {"tool_name": "createpage", "model": "cursor-grok-4.5-high-fast"}
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_is_commander_session", return_value=False):
                result = self.rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")


if __name__ == "__main__":
    unittest.main()
