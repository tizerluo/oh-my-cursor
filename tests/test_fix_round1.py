#!/usr/bin/env python3
"""Tests for MAP fix-round-1 review items (F1, F3, F4, F5, F7, F8, F10)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import SecretBootstrapMixin, load_review_gate

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


class AdvanceFixQueuePhaseTests(unittest.TestCase):
    def test_multi_agent_pr_transitions_to_synthesis_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/fix-queue.json").write_text(
                json.dumps({"p0_issues": [{"id": "a"}], "p1_issues": []})
            )
            (root / ".review/config.json").write_text(
                json.dumps({"workflow": "multi-agent-pr"})
            )
            (root / ".review/progress.json").write_text(
                json.dumps({"phase": "fix-round-1"})
            )
            result = rg.advance_fix_queue(root, mark_resolved_ids=["a"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["phase"], "synthesis-complete")

    def test_map_refactor_transitions_to_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/fix-queue.json").write_text(
                json.dumps({"p0_issues": [{"id": "a"}], "p1_issues": []})
            )
            (root / ".review/config.json").write_text(
                json.dumps({"workflow": "map-refactor"})
            )
            (root / ".review/progress.json").write_text(
                json.dumps({"phase": "fix-round-1"})
            )
            result = rg.advance_fix_queue(root, mark_resolved_ids=["a"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["phase"], "regression")

    def test_skips_phase_write_when_current_equals_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/fix-queue.json").write_text(
                json.dumps({"p0_issues": [{"id": "a"}], "p1_issues": []})
            )
            (root / ".review/config.json").write_text(
                json.dumps({"workflow": "multi-agent-pr"})
            )
            (root / ".review/progress.json").write_text(
                json.dumps({"phase": "synthesis-complete"})
            )
            with patch.object(rg, "safe_transition_phase") as mock_transition:
                rg.advance_fix_queue(root, mark_resolved_ids=["a"])
                mock_transition.assert_not_called()

    def _make_fix_root(self, tmp, phase: str = "synthesis-complete") -> Path:
        """构造 fix queue 夹具；默认阶段已在目标相位，隔离阶段迁移干扰。"""
        root = Path(tmp)
        (root / ".review").mkdir()
        (root / ".review/fix-queue.json").write_text(
            json.dumps(
                {
                    "p0_issues": [{"id": "a"}, {"id": "b"}],
                    "p1_issues": [],
                    "round": 1,
                }
            )
        )
        (root / ".review/config.json").write_text(json.dumps({"workflow": "multi-agent-pr"}))
        (root / ".review/progress.json").write_text(json.dumps({"phase": phase}))
        return root

    def test_fix_advance_delegates_resolve_to_queue_class(self):
        """G4: 若生产路径绕开 FixQueue.resolve_ids 内联过滤，本测试必须失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_fix_root(tmp)

            def _boom(self_q, resolved_ids):
                raise AssertionError("FixQueue.resolve_ids was bypassed")

            with patch.object(rg.FixQueue, "resolve_ids", _boom):
                with self.assertRaises(AssertionError):
                    rg.advance_fix_queue(root, mark_resolved_ids=["a"])

    def test_fix_advance_delegates_round_to_queue_class(self):
        """G4: 轮次自增必须使用 FixQueue.increment_round 的返回值。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_fix_root(tmp)
            with patch.object(rg.FixQueue, "increment_round", return_value=99) as mock_inc:
                result = rg.advance_fix_queue(root, mark_resolved_ids=["a"], increment_round=True)
            mock_inc.assert_called_once()
            self.assertEqual(result["queue_round"], 99)
            self.assertEqual(result["round"], 99)


class AdvanceCriticQueuePhaseTests(unittest.TestCase):
    def test_skips_phase_write_when_already_at_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec"}]})
            )
            (root / ".review/progress.json").write_text(json.dumps({"phase": "revise"}))
            with patch.object(rg, "safe_transition_phase") as mock_transition:
                rg.advance_critic_queue(root, mark_resolved_ids=["other"])
                mock_transition.assert_not_called()

    def test_critic_advance_delegates_resolve_to_queue_class(self):
        """G4: 若生产路径绕开 CriticQueue.resolve_ids 内联过滤，本测试必须失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec"}, {"id": "perf"}]})
            )
            # 阶段已在目标相位（kept 非空 → revise），隔离阶段迁移干扰
            (root / ".review/progress.json").write_text(json.dumps({"phase": "revise"}))
            with patch.object(
                rg.CriticQueue, "resolve_ids", side_effect=AssertionError("bypassed")
            ):
                with self.assertRaises(AssertionError):
                    rg.advance_critic_queue(root, mark_resolved_ids=["sec"])

    def test_critic_advance_delegates_round_to_queue_class(self):
        """G4: critic 轮次自增必须走 CriticQueue.increment_round。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec"}, {"id": "perf"}], "round": 1})
            )
            (root / ".review/progress.json").write_text(json.dumps({"phase": "revise"}))
            with patch.object(rg.CriticQueue, "increment_round", return_value=42) as mock_inc:
                result = rg.advance_critic_queue(
                    root, mark_resolved_ids=["sec"], increment_round=True
                )
            mock_inc.assert_called_once()
            self.assertTrue(result["ok"])
            self.assertEqual(result["pending_remaining"], 1)


class AdvanceQueueOrderingTests(unittest.TestCase):
    """G5: safe_transition_phase 抛错时，队列文件必须保持原样（先迁移后落盘）。"""

    def test_fix_queue_intact_when_phase_transition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            queue_payload = {"p0_issues": [{"id": "a"}], "p1_issues": [], "round": 1}
            (root / ".review/fix-queue.json").write_text(json.dumps(queue_payload))
            (root / ".review/config.json").write_text(json.dumps({"workflow": "multi-agent-pr"}))
            # coding -> synthesis-complete 不是合法迁移，必然抛 PhaseTransitionError
            (root / ".review/progress.json").write_text(json.dumps({"phase": "coding"}))
            before = (root / ".review/fix-queue.json").read_text(encoding="utf-8")
            with self.assertRaises(rg.PhaseTransitionError):
                rg.advance_fix_queue(root, mark_resolved_ids=["a"])
            after = (root / ".review/fix-queue.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_critic_queue_intact_when_phase_transition_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec"}]})
            )
            # accepted 没有出边；kept 非空时目标相位是 revise，必然抛错
            (root / ".review/progress.json").write_text(json.dumps({"phase": "accepted"}))
            before = (root / ".review/critic-queue.json").read_text(encoding="utf-8")
            with self.assertRaises(rg.PhaseTransitionError):
                rg.advance_critic_queue(root, mark_resolved_ids=[])
            after = (root / ".review/critic-queue.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)


class McpTokenWriteTests(unittest.TestCase):
    def test_blocks_underscore_and_camelcase_names(self):
        blocked = (
            "task_create",
            "notion_create_page",
            "mcp__notion__create_page",
            "createPage",
            "PutItem",
            "write_file",
            "create-page",
        )
        for name in blocked:
            self.assertTrue(rg._mcp_tool_is_write(name), name)

    def test_allows_false_positive_names(self):
        allowed = ("settings_get", "address_lookup", "addon_search", "dataset_list")
        for name in allowed:
            self.assertFalse(rg._mcp_tool_is_write(name), name)


class McpCamelCasePermissionTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._bootstrap_secret(rg)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _init_git_repo(self.root)
        review = self.root / ".review"
        review.mkdir(parents=True, exist_ok=True)
        (review / "config.json").write_text(
            json.dumps({"active": True, "workflow": "multi-agent-pr", "session_id": "s1"})
        )
        roles = review / "roles"
        roles.mkdir(parents=True, exist_ok=True)
        (roles / "agent1.json").write_text(
            json.dumps({"logical_role": "reviewer-grok", "subagent_id": "agent1"})
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._clear_secret_env()

    def test_camelcase_write_tool_denied(self):
        data = {
            "cwd": str(self.root),
            "subagentId": "agent1",
            "toolName": "task_create",
        }
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_snake_case_write_tool_denied(self):
        data = {
            "cwd": str(self.root),
            "subagent_id": "agent1",
            "tool_name": "write_file",
        }
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_camelcase_read_tool_allowed(self):
        data = {
            "cwd": str(self.root),
            "subagentId": "agent1",
            "toolName": "settings_get",
        }
        result = rg.check_mcp_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")


class ReviewerModelsFallbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        rg._reset_models_cache()

    def test_partial_config_falls_back_per_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "config"
            cfg_dir.mkdir()
            (cfg_dir / "models.json").write_text(
                json.dumps(
                    {
                        "reviewers": {
                            "reviewer-grok": {"model": "custom-grok"},
                        }
                    }
                )
            )
            with patch.object(rg, "MODELS_CONFIG_FILE", cfg_dir / "models.json"):
                rg._reset_models_cache()
                models = rg._build_reviewer_spawn_models()
            self.assertEqual(models["reviewer-grok"], "custom-grok")
            self.assertEqual(models["reviewer-codex"], rg._FALLBACK_REVIEWERS["reviewer-codex"])
            self.assertEqual(models["reviewer-gemini"], rg._FALLBACK_REVIEWERS["reviewer-gemini"])

    def test_missing_file_uses_full_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config" / "models.json"
            with patch.object(rg, "MODELS_CONFIG_FILE", missing):
                rg._reset_models_cache()
                models = rg._build_reviewer_spawn_models()
            self.assertEqual(models, dict(rg._FALLBACK_REVIEWERS))


class CommanderPermissionTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._bootstrap_secret(rg)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _init_git_repo(self.root)
        (self.root / ".review").mkdir(exist_ok=True)
        (self.root / ".review/config.json").write_text(
            json.dumps({"active": True, "workflow": "multi-agent-pr", "session_id": "s1"})
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._clear_secret_env()

    def _commander_payload(self, path: str, transcript: str | None = None) -> dict:
        # 真实根会话 transcript 形状：.../.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl
        if transcript is None:
            transcript = (
                "/Users/me/.cursor/projects/foo/agent-transcripts/abc/abc.jsonl"
            )
        return {
            "cwd": str(self.root),
            "tool_name": "Write",
            "tool_input": {"path": path},
            "transcript_path": transcript,
        }

    def test_commander_allowed_review_write(self):
        result = rg.check_tool_permission_from_hook(
            self._commander_payload(".review/verdict.json")
        )
        self.assertEqual(result["permission"], "allow")

    def test_commander_denied_implementation_write(self):
        result = rg.check_tool_permission_from_hook(
            self._commander_payload("hooks/review_gate.py")
        )
        self.assertEqual(result["permission"], "deny")
        self.assertIn("Commander", result.get("user_message", ""))

    def test_tmp_transcript_not_commander_denied_review_write(self):
        """G3: /tmp 下的任意 transcript 不得被当作 Commander（.review/ 写拒绝）。"""
        result = rg.check_tool_permission_from_hook(
            self._commander_payload(
                ".review/verdict.json", transcript="/tmp/transcript.jsonl"
            )
        )
        self.assertEqual(result["permission"], "deny")

    def test_mismatched_stem_transcript_denied_review_write(self):
        """G3: 文件名与父目录不同名的 transcript 不算根会话。"""
        result = rg.check_tool_permission_from_hook(
            self._commander_payload(
                ".review/verdict.json",
                transcript="/Users/me/.cursor/projects/foo/agent-transcripts/abc/def.jsonl",
            )
        )
        self.assertEqual(result["permission"], "deny")

    def test_subagent_without_role_denied_everywhere(self):
        data = {
            "cwd": str(self.root),
            "tool_name": "Write",
            "tool_input": {"path": ".review/verdict.json"},
            "subagent_id": "orphan-1",
            "transcript_path": "/Users/me/.cursor/projects/foo/agent-transcripts/subagents/x.jsonl",
        }
        result = rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")


if __name__ == "__main__":
    unittest.main()
