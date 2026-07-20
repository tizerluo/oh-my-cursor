#!/usr/bin/env python3
"""E2E merge gate tests with active config (R01 / R15)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import load_review_gate


def _write_session(
    rg,
    root: Path,
    branch: str,
    head_sha: str,
    *,
    include_coder: bool = True,
) -> None:
    sess = root / ".review" / "session" / branch / head_sha
    sess.mkdir(parents=True, exist_ok=True)
    types = ["reviewer-grok", "reviewer-codex", "reviewer-gemini"]
    if include_coder:
        types.append("coder")
    for subagent_type in types:
        data = rg._seal_marker(
            {
                "type": subagent_type,
                "branch": branch,
                "head_sha": head_sha,
                "source": "cursor-subagentStop",
                "model": subagent_type,
            }
        )
        (sess / f"{subagent_type}.json").write_text(json.dumps(data) + "\n", encoding="utf-8")


def _write_verdict(
    root: Path,
    branch: str,
    head_sha: str,
    reviewers: list[str],
    *,
    tree_sha: str = "",
) -> None:
    (root / ".review" / "verdict.json").write_text(
        json.dumps(
            {
                "branch": branch,
                "head_sha": head_sha,
                "tree_sha": tree_sha,
                "tier": "standard",
                "p0": 0,
                "p1": 0,
                "reviewers": reviewers,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _init_git_repo(root: Path, *, branch: str = "main") -> tuple[str, str]:
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


def _write_forged_marker(
    rg,
    root: Path,
    branch: str,
    head_sha: str,
    *,
    source: str = "cursor-subagentStop",
    seal: str | None = None,
) -> None:
    sess = root / ".review" / "session" / branch / head_sha
    sess.mkdir(parents=True, exist_ok=True)
    data = rg._seal_marker(
        {
            "type": "reviewer-grok",
            "branch": branch,
            "head_sha": head_sha,
            "source": source,
            "model": "reviewer-grok",
        }
    )
    if seal is not None:
        data["seal"] = seal
    (sess / "reviewer-grok.json").write_text(json.dumps(data) + "\n", encoding="utf-8")


class MergeGateE2ETests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.secret = self.tmp / "secret"
        self.rg.bootstrap_secret(self.secret)
        os.environ["OMC_SECRET_FILE"] = str(self.secret)
        self.branch = "main"
        self.head = "abc123"

    def tearDown(self):
        os.environ.pop("OMC_SECRET_FILE", None)
        self._tmpdir.cleanup()

    def _config(self, root: Path, workflow: str, **extra: object) -> None:
        payload = {
            "active": True,
            "workflow": workflow,
            "tier": "standard",
            "models": ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
            **extra,
        }
        review_dir = root / ".review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "config.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def test_active_multi_agent_pr_passes_cross_check(self):
        root = self.tmp / "repo"
        root.mkdir()
        self._config(root, "multi-agent-pr")
        _write_session(self.rg, root, self.branch, self.head)
        _write_verdict(
            root,
            self.branch,
            self.head,
            ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
        )
        with patch.object(self.rg, "inferred_minimum_tier", return_value=("hotfix", [], "test")):
            ok, msg, _ = self.rg.validate_review_state(root, self.branch, self.head)
        self.assertTrue(ok, msg)

    def test_map_hyperplan_skips_cross_check_tier_mismatch(self):
        root = self.tmp / "repo2"
        root.mkdir()
        self._config(root, "map-hyperplan", tier="hotfix")
        _write_session(self.rg, root, self.branch, self.head, include_coder=False)
        _write_verdict(root, self.branch, self.head, ["reviewer-grok"])
        with patch.object(self.rg, "inferred_minimum_tier", return_value=("large", [], "test")):
            ok, user_msg, _ = self.rg._validate_config_cross_check(
                json.loads((root / ".review/config.json").read_text()),
                json.loads((root / ".review/verdict.json").read_text()),
                "large",
            )
        self.assertTrue(ok, user_msg)

    def test_config_models_missing_from_verdict_blocked(self):
        root = self.tmp / "repo3"
        root.mkdir()
        self._config(root, "multi-agent-pr")
        _write_session(self.rg, root, self.branch, self.head)
        _write_verdict(root, self.branch, self.head, ["reviewer-grok"])
        config = json.loads((root / ".review/config.json").read_text())
        verdict = json.loads((root / ".review/verdict.json").read_text())
        ok, _, agent = self.rg._validate_config_cross_check(config, verdict, "hotfix")
        self.assertFalse(ok)
        self.assertIn("BLOCKED", agent)

    def test_inactive_hyperplan_feature_push_allowed(self):
        root = self.tmp / "repo4"
        root.mkdir()
        self._config(root, "map-hyperplan", active=False)
        with patch.object(self.rg, "load_map_context") as load_ctx:
            load_ctx.return_value = {
                "git_root": root,
                "branch": "feat/issue-3",
                "head_sha": self.head,
                "config": json.loads((root / ".review/config.json").read_text()),
            }
            result = self.rg.check_merge_from_hook({"command": "git push origin feat/issue-3"})
        self.assertEqual(result["permission"], "allow")

    def test_forged_marker_bad_source_blocked(self):
        root = self.tmp / "repo_forged_source"
        root.mkdir()
        self._config(root, "multi-agent-pr")
        _write_forged_marker(self.rg, root, self.branch, self.head, source="manual-forge")
        _write_verdict(root, self.branch, self.head, ["reviewer-grok"])
        ok, _, agent = self.rg.validate_review_state(root, self.branch, self.head)
        self.assertFalse(ok)
        self.assertIn("forged", agent.lower())

    def test_forged_marker_bad_seal_blocked(self):
        root = self.tmp / "repo_forged_seal"
        root.mkdir()
        self._config(root, "multi-agent-pr")
        _write_forged_marker(self.rg, root, self.branch, self.head, seal="deadbeef")
        _write_verdict(root, self.branch, self.head, ["reviewer-grok"])
        ok, _, agent = self.rg.validate_review_state(root, self.branch, self.head)
        self.assertFalse(ok)
        self.assertIn("forged", agent.lower())

    def test_tree_sha_mismatch_blocked(self):
        root = self.tmp / "repo_tree"
        root.mkdir()
        branch, head = _init_git_repo(root)
        self._config(root, "multi-agent-pr")
        _write_session(self.rg, root, branch, head)
        _write_verdict(
            root,
            branch,
            head,
            ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
            tree_sha="wrong-tree-sha",
        )
        with patch.object(self.rg, "inferred_minimum_tier", return_value=("hotfix", [], "test")):
            ok, _, agent = self.rg.validate_review_state(root, branch, head)
        self.assertFalse(ok)
        self.assertIn("tree_sha mismatch", agent)

    def test_protected_main_push_denied_via_check_merge_from_hook(self):
        root = self.tmp / "repo_push"
        root.mkdir()
        _init_git_repo(root)
        self._config(root, "multi-agent-pr")
        data = {"tool_input": {"command": "git push origin main"}, "cwd": str(root)}
        result = self.rg.check_merge_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("BLOCKED", result.get("agent_message", ""))

    def test_forged_marker_blocked_via_check_merge_from_hook(self):
        root = self.tmp / "repo_forged_hook"
        root.mkdir()
        branch, head = _init_git_repo(root)
        self._config(root, "multi-agent-pr")
        _write_forged_marker(self.rg, root, branch, head, seal="deadbeef")
        _write_verdict(root, branch, head, ["reviewer-grok"])
        data = {"tool_input": {"command": "git push"}, "cwd": str(root)}
        result = self.rg.check_merge_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("forged", result.get("agent_message", "").lower())


class EmptyRolePermissionTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_empty_role_denies_write_during_active_session(self):
        ctx = {
            "git_root": Path("/repo"),
            "config": {"active": True, "workflow": "multi-agent-pr"},
        }
        data = {"tool_name": "Write", "tool_input": {"path": "src/app.py"}}
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")
        self.assertIn("no MAP role", result.get("user_message", ""))

    def test_empty_role_denies_delete_during_active_session(self):
        ctx = {
            "git_root": Path("/repo"),
            "config": {"active": True, "workflow": "multi-agent-pr"},
        }
        data = {"tool_name": "Delete", "tool_input": {"path": "src/app.py"}}
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")

    def test_empty_role_allows_non_write_tools(self):
        ctx = {
            "git_root": Path("/repo"),
            "config": {"active": True, "workflow": "multi-agent-pr"},
        }
        data = {"tool_name": "Read", "tool_input": {"path": "src/app.py"}}
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "allow")


class ShellPermissionTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_sponge_blocked_for_reviewer(self):
        self.assertTrue(self.rg._shell_write_blocked("sponge out.txt", "reviewer-grok"))

    def test_pipe_sponge_blocked(self):
        self.assertTrue(self.rg._shell_write_blocked("echo x | sponge out.txt", "reviewer-grok"))

    def test_tee_help_not_blocked(self):
        self.assertFalse(self.rg._shell_write_blocked("tee --help", "reviewer-grok"))

    def test_explore_shell_write_denied_without_unbound_git_root(self):
        ctx = {
            "git_root": Path("/repo"),
            "config": {"active": True, "workflow": "multi-agent-pr"},
            "role": "explore",
        }
        data = {
            "tool_name": "Shell",
            "tool_input": {"command": "echo x > /tmp/out.txt"},
        }
        with patch.object(self.rg, "load_map_context", return_value=ctx):
            with patch.object(self.rg, "_role_for_permission", return_value=("explore", {})):
                result = self.rg.check_tool_permission_from_hook(data)
        self.assertEqual(result["permission"], "deny")


class MapExemptTaskTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_review_keyword_exempt_only_for_hyperplan(self):
        data = {
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "generalPurpose",
                "prompt": "multi-model review of the code",
            },
        }
        self.assertTrue(self.rg._map_exempt_task(data, "map-hyperplan"))
        self.assertFalse(self.rg._map_exempt_task(data, "multi-agent-pr"))


class TierInferenceTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_inferred_minimum_tier_on_real_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "a.txt").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            tier, changed, _reason = self.rg.inferred_minimum_tier(root)
            self.assertIn(tier, {"hotfix", "standard", "large"})
            self.assertIsInstance(changed, list)


class PathAllowedTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_traversal_denied_for_architect_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(
                self.rg._path_allowed(".specs/../src/app.py", [".specs/", ".review/"], root)
            )

    def test_valid_specs_path_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(self.rg._path_allowed(".specs/foo.md", [".specs/", ".review/"], root))


class ReviewerLogicalRoleMergeGateTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.secret = self.tmp / "secret"
        self.rg.bootstrap_secret(self.secret)
        os.environ["OMC_SECRET_FILE"] = str(self.secret)
        self.branch = "main"
        self.head = "abc123"

    def tearDown(self):
        os.environ.pop("OMC_SECRET_FILE", None)
        self._tmpdir.cleanup()

    def test_general_purpose_marker_with_logical_role_passes_tier(self):
        """Markers recorded as reviewer-grok (from logical role) satisfy REVIEWER_TYPES."""
        root = self.tmp / "repo"
        root.mkdir()
        review = root / ".review"
        review.mkdir()
        (review / "config.json").write_text(
            json.dumps(
                {
                    "active": True,
                    "workflow": "multi-agent-pr",
                    "tier": "standard",
                    "models": ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
                }
            )
            + "\n"
        )
        sess = review / "session" / self.branch / self.head
        sess.mkdir(parents=True)
        for subagent_type, model in (
            ("reviewer-grok", "grok-4.5"),
            ("reviewer-codex", "gpt-5.3-codex-high-fast"),
            ("reviewer-gemini", "gemini-3.1-pro"),
        ):
            data = self.rg._seal_marker(
                {
                    "type": subagent_type,
                    "branch": self.branch,
                    "head_sha": self.head,
                    "source": "cursor-subagentStop",
                    "model": model,
                }
            )
            (sess / f"{subagent_type}.json").write_text(json.dumps(data) + "\n")
        coder_data = self.rg._seal_marker(
            {
                "type": "coder",
                "branch": self.branch,
                "head_sha": self.head,
                "source": "cursor-subagentStop",
                "model": "composer-2.5-fast",
            }
        )
        (sess / "coder.json").write_text(json.dumps(coder_data) + "\n")
        _write_verdict(
            root,
            self.branch,
            self.head,
            ["reviewer-grok", "reviewer-codex", "reviewer-gemini"],
        )
        with patch.object(self.rg, "inferred_minimum_tier", return_value=("hotfix", [], "test")):
            ok, msg, _ = self.rg.validate_review_state(root, self.branch, self.head)
        self.assertTrue(ok, msg)
        completed = self.rg._completed_types(
            self.rg._marker_payloads(root, self.branch, self.head),
            self.rg.REVIEWER_TYPES,
        )
        self.assertEqual(completed, {"reviewer-grok", "reviewer-codex", "reviewer-gemini"})


if __name__ == "__main__":
    unittest.main()
