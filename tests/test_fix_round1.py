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

    def test_uses_fix_queue_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/fix-queue.json").write_text(
                json.dumps(
                    {
                        "p0_issues": [{"id": "a"}, {"id": "b"}],
                        "p1_issues": [],
                    }
                )
            )
            (root / ".review/config.json").write_text(json.dumps({"workflow": "multi-agent-pr"}))
            (root / ".review/progress.json").write_text(json.dumps({"phase": "fix-round-1"}))
            with patch.object(rg.FixQueue, "save", wraps=rg.FixQueue(root).save) as mock_save:
                rg.advance_fix_queue(root, mark_resolved_ids=["a"])
                self.assertEqual(mock_save.call_count, 1)


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

    def test_uses_critic_queue_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec"}]})
            )
            (root / ".review/progress.json").write_text(json.dumps({"phase": "debate"}))
            with patch.object(rg.CriticQueue, "save", wraps=rg.CriticQueue(root).save) as mock_save:
                rg.advance_critic_queue(root, mark_resolved_ids=["other"])
                self.assertEqual(mock_save.call_count, 1)


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

    def _commander_payload(self, path: str) -> dict:
        return {
            "cwd": str(self.root),
            "tool_name": "Write",
            "tool_input": {"path": path},
            "transcript_path": "/Users/me/.cursor/projects/foo/agent-transcripts/abc.jsonl",
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
