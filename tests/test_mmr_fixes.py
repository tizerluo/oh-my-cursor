#!/usr/bin/env python3
"""Unit tests for MMR issues 1–8 fixes in review_gate.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import review_gate as rg  # noqa: E402


class SecurityCodeModifiedTests(unittest.TestCase):
    def test_in_scope_change_no_gate(self):
        config = {"scope_paths": ["src/auth/"]}
        changed = ["src/auth/login.py"]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            self.assertFalse(rg._security_code_modified(Path("/repo"), config))

    def test_out_of_scope_change_triggers_gate(self):
        config = {"scope_paths": ["src/auth/"]}
        changed = ["src/other/y.py"]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            self.assertTrue(rg._security_code_modified(Path("/repo"), config))

    def test_poc_only_no_gate(self):
        config = {"scope_paths": ["src/auth/"]}
        changed = [".review/poc/exploit.py"]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            self.assertFalse(rg._security_code_modified(Path("/repo"), config))


class TaskAlignmentTests(unittest.TestCase):
    def _payload(self, **kwargs):
        base = {
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "generalPurpose",
                "readonly": True,
                "prompt": "multi-model review",
            },
            "cwd": "/tmp",
        }
        base.update(kwargs)
        return base

    @patch.object(rg, "load_map_context")
    def test_no_active_session_allows_task(self, load_ctx):
        load_ctx.return_value = {"config": None, "progress": None}
        result = rg.check_task_alignment_from_hook(self._payload())
        self.assertEqual(result["permission"], "allow")

    @patch.object(rg, "load_map_context")
    def test_inactive_config_allows_task(self, load_ctx):
        load_ctx.return_value = {
            "config": {"active": False},
            "progress": None,
        }
        result = rg.check_task_alignment_from_hook(self._payload())
        self.assertEqual(result["permission"], "allow")

    @patch.object(rg, "load_map_context")
    def test_readonly_generalpurpose_exempt_when_active(self, load_ctx):
        load_ctx.return_value = {
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "multi-agent-pr",
                "roles": {},
            },
            "progress": {"session_id": "s1", "phase": "review-pending"},
        }
        result = rg.check_task_alignment_from_hook(self._payload())
        self.assertEqual(result["permission"], "allow")

    @patch.object(rg, "load_map_context")
    def test_hyperplan_denies_coder(self, load_ctx):
        load_ctx.return_value = {
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "map-hyperplan",
                "roles": {"coder": "coder"},
            },
            "progress": {"session_id": "s1", "phase": "draft"},
        }
        payload = self._payload(
            tool_input={"subagent_type": "coder", "prompt": "implement"},
        )
        result = rg.check_task_alignment_from_hook(payload)
        self.assertEqual(result["permission"], "deny")

    @patch.object(rg, "load_map_context")
    def test_coder_wrong_phase_denied(self, load_ctx):
        load_ctx.return_value = {
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "multi-agent-pr",
                "roles": {"coder": "coder"},
            },
            "progress": {"session_id": "s1", "phase": "alignment"},
        }
        payload = self._payload(
            tool_input={"subagent_type": "coder", "prompt": "implement"},
        )
        result = rg.check_task_alignment_from_hook(payload)
        self.assertEqual(result["permission"], "deny")

    @patch.object(rg, "load_map_context")
    def test_coder_allowed_phase(self, load_ctx):
        load_ctx.return_value = {
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "multi-agent-pr",
                "roles": {"coder": "coder"},
            },
            "progress": {"session_id": "s1", "phase": "coding"},
        }
        payload = self._payload(
            tool_input={"subagent_type": "coder", "prompt": "implement"},
        )
        result = rg.check_task_alignment_from_hook(payload)
        self.assertEqual(result["permission"], "allow")

    @patch.object(rg, "load_map_context")
    def test_task_spawn_reviewer_grok_denied_with_hint(self, load_ctx):
        load_ctx.return_value = {
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "multi-agent-pr",
                "roles": {},
            },
            "progress": {"session_id": "s1", "phase": "review-pending"},
        }
        payload = self._payload(
            tool_input={
                "subagent_type": "reviewer-grok",
                "model": "grok-build-0.1",
                "prompt": "review code",
            },
        )
        result = rg.check_task_alignment_from_hook(payload)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("generalPurpose", result["agent_message"])
        self.assertIn("readonly", result["agent_message"])
        self.assertIn("reviewer-grok", result["agent_message"])


class ReviewerLogicalRoleInferenceTests(unittest.TestCase):
    def test_infer_from_logical_role_prompt(self):
        role = rg._infer_reviewer_logical_role(
            "",
            "You are MAP. logical_role: reviewer-codex\nReview files.",
        )
        self.assertEqual(role, "reviewer-codex")

    def test_infer_from_reviewer_grok_label(self):
        role = rg._infer_reviewer_logical_role("", "You are Reviewer-Grok for PR 8.")
        self.assertEqual(role, "reviewer-grok")

    def test_infer_from_model_map(self):
        self.assertEqual(
            rg._infer_reviewer_logical_role("gemini-3.1-pro", ""),
            "reviewer-gemini",
        )

    @patch.object(rg, "load_map_context")
    @patch.object(rg, "_write_json_file")
    def test_set_role_infers_reviewer_from_general_purpose_prompt(self, write_json, load_ctx):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            load_ctx.return_value = {
                "git_root": root,
                "config": {"active": True, "session_id": "s1", "workflow": "multi-agent-pr"},
                "progress": {"session_id": "s1", "phase": "review-pending"},
            }
            rg.set_role_from_hook(
                {
                    "subagent_type": "generalPurpose",
                    "subagent_id": "rev-1",
                    "tool_input": {
                        "prompt": "MAP Reviewer-Grok. logical_role: reviewer-grok",
                        "model": "grok-build-0.1",
                    },
                }
            )
            role_writes = [
                call[0][1]
                for call in write_json.call_args_list
                if call[0][1].get("subagent_id") == "rev-1"
            ]
            self.assertEqual(role_writes[0]["logical_role"], "reviewer-grok")
            self.assertEqual(role_writes[0]["subagent_type"], "generalPurpose")


class RecordSubagentMarkerTests(unittest.TestCase):
    @patch.dict(os.environ, {"REVIEW_GATE_HOOK_MODE": "subagentStop"}, clear=False)
    @patch.object(rg, "load_map_context")
    def test_record_subagent_marker_type_from_logical_role(self, load_ctx):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / ".review"
            roles = review / "roles"
            roles.mkdir(parents=True)
            (roles / "agent-42.json").write_text(
                json.dumps(
                    {
                        "subagent_id": "agent-42",
                        "logical_role": "reviewer-grok",
                        "subagent_type": "generalPurpose",
                    }
                )
            )
            branch = "feat/x"
            head = "deadbeef"
            load_ctx.return_value = {
                "git_root": root,
                "branch": branch,
                "head_sha": head,
                "tree_sha": "",
                "config": {"workflow": "multi-agent-pr"},
            }
            secret = root / "secret"
            rg.bootstrap_secret(secret)
            with patch.dict(os.environ, {"OMC_SECRET_FILE": str(secret)}):
                data = {
                    "event": "subagentStop",
                    "subagent_type": "generalPurpose",
                    "subagent_id": "agent-42",
                    "model": "grok-build-0.1",
                    "cwd": str(root),
                }
                result = rg.record_subagent_from_hook(data, json.dumps(data))
            self.assertIn("reviewer-grok", result.get("followup_message", ""))
            sess = root / ".review" / "session" / rg._slug(branch) / head
            markers = list(sess.glob("*.json"))
            self.assertTrue(markers)
            marker = json.loads(markers[0].read_text())
            self.assertEqual(marker["type"], "reviewer-grok")


class MergeGateTests(unittest.TestCase):
    @patch.object(rg, "validate_review_state")
    @patch.object(rg, "load_map_context")
    def test_hyperplan_merge_denied_when_active(self, load_ctx, validate):
        load_ctx.return_value = {
            "git_root": Path("/repo"),
            "branch": "feat/x",
            "head_sha": "abc",
            "config": {"workflow": "map-hyperplan", "active": True},
        }
        data = {"command": "gh pr merge 1"}
        result = rg.check_merge_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        validate.assert_not_called()

    @patch.object(rg, "validate_review_state")
    @patch.object(rg, "load_map_context")
    def test_hyperplan_merge_fallthrough_when_inactive(self, load_ctx, validate):
        load_ctx.return_value = {
            "git_root": Path("/repo"),
            "branch": "feat/x",
            "head_sha": "abc",
            "config": {"workflow": "map-hyperplan", "active": False},
        }
        validate.return_value = (False, "missing markers", "BLOCKED: markers")
        data = {"command": "gh pr merge 1"}
        result = rg.check_merge_from_hook(data)
        validate.assert_called_once_with(Path("/repo"), "feat/x", "abc")
        self.assertEqual(result["permission"], "deny")
        self.assertEqual(result["user_message"], "missing markers")


class LogicalRoleTests(unittest.TestCase):
    def test_resolve_logical_role_from_config(self):
        config = {"roles": {"poc-exploit": "coder", "hunter": "explore"}}
        self.assertEqual(rg._resolve_logical_role(config, "coder"), "poc-exploit")
        self.assertEqual(rg._resolve_logical_role(config, "explore"), "hunter")

    @patch.object(rg, "load_map_context")
    @patch.object(rg, "_write_json_file")
    def test_set_role_writes_logical_role(self, write_json, load_ctx):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review" / "config.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "session_id": "s1",
                        "workflow": "map-security",
                        "roles": {"poc-exploit": "coder"},
                    }
                )
            )
            load_ctx.return_value = {
                "git_root": root,
                "config": json.loads((root / ".review/config.json").read_text()),
                "progress": {"session_id": "s1", "phase": "poc"},
            }
            rg.set_role_from_hook(
                {"subagent_type": "coder", "subagent_id": "agent-1", "model": "x"}
            )
            role_writes = [
                call[0][1]
                for call in write_json.call_args_list
                if call[0][1].get("subagent_id") == "agent-1"
            ]
            self.assertEqual(len(role_writes), 1)
            written = role_writes[0]
            self.assertEqual(written["logical_role"], "poc-exploit")
            self.assertEqual(written["subagent_type"], "coder")


class ShellWriteTests(unittest.TestCase):
    def test_echo_redirect_blocked_for_explore(self):
        self.assertTrue(
            rg._shell_write_blocked("echo x > /tmp/t", "explore"),
        )

    def test_git_diff_allowed(self):
        self.assertFalse(rg._shell_write_blocked("git diff HEAD~1", "explore"))

    def test_poc_redirect_allowed(self):
        self.assertFalse(
            rg._shell_write_blocked("echo poc > .review/poc/x.py", "poc-exploit"),
        )


class StopCheckTests(unittest.TestCase):
    @patch.object(rg, "_critic_queue_followup")
    @patch.object(rg, "_fix_queue_followup")
    @patch.object(rg, "load_map_context")
    def test_fix_round_phase_does_not_trigger_stop(self, load_ctx, fix_followup, critic_followup):
        load_ctx.return_value = {
            "git_root": Path("/repo"),
            "branch": "b",
            "head_sha": "sha",
            "config": {
                "active": True,
                "session_id": "s1",
                "workflow": "multi-agent-pr",
            },
            "progress": {"session_id": "s1", "phase": "fix-round-1"},
        }
        result = rg.stop_check_from_hook({})
        self.assertEqual(result, {})
        fix_followup.assert_not_called()
        critic_followup.assert_not_called()


class ValidatePhaseTests(unittest.TestCase):
    def test_planner_hyperplan_draft_ok(self):
        ok, _ = rg.validate_phase("map-hyperplan", "draft", "planner")
        self.assertTrue(ok)

    def test_planner_hyperplan_merge_ready_denied(self):
        ok, reason = rg.validate_phase("map-hyperplan", "accepted", "planner")
        self.assertFalse(ok)
        self.assertIn("accepted", reason)


if __name__ == "__main__":
    unittest.main()
