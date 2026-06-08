#!/usr/bin/env python3
"""E2E merge gate tests with active config (R01 / R15)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"


def load_review_gate():
    spec = importlib.util.spec_from_file_location("review_gate", HOOKS_DIR / "review_gate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def _write_verdict(root: Path, branch: str, head_sha: str, reviewers: list[str]) -> None:
    (root / ".review" / "verdict.json").write_text(
        json.dumps(
            {
                "branch": branch,
                "head_sha": head_sha,
                "tree_sha": "",
                "tier": "standard",
                "p0": 0,
                "p1": 0,
                "reviewers": reviewers,
            }
        )
        + "\n",
        encoding="utf-8",
    )


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


class ShellPermissionTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_sponge_blocked_for_reviewer(self):
        self.assertTrue(self.rg._shell_write_blocked("sponge out.txt", "reviewer-grok"))

    def test_pipe_sponge_blocked(self):
        self.assertTrue(self.rg._shell_write_blocked("echo x | sponge out.txt", "reviewer-grok"))

    def test_tee_help_not_blocked(self):
        self.assertFalse(self.rg._shell_write_blocked("tee --help", "reviewer-grok"))


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


if __name__ == "__main__":
    unittest.main()
