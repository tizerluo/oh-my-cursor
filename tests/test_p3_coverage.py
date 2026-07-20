#!/usr/bin/env python3
"""Tests for session-resume, routing hints, tier inference, and related functions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


class MinReviewerCountTests(unittest.TestCase):
    def test_hotfix_returns_1(self):
        self.assertEqual(rg.min_reviewer_count_for_tier("hotfix"), 1)

    def test_standard_returns_3(self):
        self.assertEqual(rg.min_reviewer_count_for_tier("standard"), 3)

    def test_large_returns_3(self):
        self.assertEqual(rg.min_reviewer_count_for_tier("large"), 3)

    def test_empty_defaults_to_standard(self):
        self.assertEqual(rg.min_reviewer_count_for_tier(""), 3)

    def test_none_defaults_to_standard(self):
        self.assertEqual(rg.min_reviewer_count_for_tier(None), 3)

    def test_case_insensitive(self):
        self.assertEqual(rg.min_reviewer_count_for_tier("Hotfix"), 1)
        self.assertEqual(rg.min_reviewer_count_for_tier("STANDARD"), 3)


class InferredMinimumTierTests(unittest.TestCase):
    def test_no_changed_files_returns_standard(self):
        with patch.object(rg, "_git_diff_files", return_value=[]):
            tier, files, reason = rg.inferred_minimum_tier(Path("/repo"))
        self.assertEqual(tier, "standard")
        self.assertEqual(files, [])
        self.assertIn("diff base unavailable", reason)

    def test_few_non_risky_returns_hotfix(self):
        changed = ["src/foo.py", "src/bar.py"]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            tier, files, reason = rg.inferred_minimum_tier(Path("/repo"))
        self.assertEqual(tier, "hotfix")
        self.assertEqual(files, changed)

    def test_moderate_change_returns_standard(self):
        changed = [f"src/file{i}.py" for i in range(5)]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            tier, files, reason = rg.inferred_minimum_tier(Path("/repo"))
        self.assertEqual(tier, "standard")
        self.assertIn("5 files changed", reason)

    def test_many_files_returns_large(self):
        changed = [f"src/file{i}.py" for i in range(12)]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            tier, _, reason = rg.inferred_minimum_tier(Path("/repo"))
        self.assertEqual(tier, "large")

    def test_risky_path_returns_large(self):
        changed = ["src/auth/login.py", "src/auth/permissions.py"]
        with patch.object(rg, "_git_diff_files", return_value=changed):
            tier, _, reason = rg.inferred_minimum_tier(Path("/repo"))
        self.assertEqual(tier, "large")


class ParseSpecFrontmatterTests(unittest.TestCase):
    def test_valid_frontmatter(self):
        text = "---\ntitle: My Spec\nstatus: draft\n---\n# Content"
        result = rg.parse_spec_frontmatter(text)
        self.assertEqual(result["title"], "My Spec")
        self.assertEqual(result["status"], "draft")

    def test_no_frontmatter(self):
        text = "# Just a heading\nSome content"
        result = rg.parse_spec_frontmatter(text)
        self.assertEqual(result, {})

    def test_quoted_values(self):
        text = '---\nstatus: "draft"\ntitle: \'Hello\'\n---\n'
        result = rg.parse_spec_frontmatter(text)
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["title"], "Hello")


class DraftSpecsTests(unittest.TestCase):
    def test_no_specs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = rg._draft_specs(Path(tmp))
        self.assertEqual(result, [])

    def test_finds_draft_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / ".specs" / "features"
            specs.mkdir(parents=True)
            (specs / "auth.md").write_text(
                "---\ntitle: Auth\nstatus: draft\n---\n# Auth spec\n", encoding="utf-8",
            )
            (specs / "billing.md").write_text(
                "---\ntitle: Billing\nstatus: approved\n---\n# Billing\n", encoding="utf-8",
            )
            result = rg._draft_specs(root)
        self.assertEqual(len(result), 1)
        self.assertIn("auth.md", result[0])

    def test_fallback_regex_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / ".specs"
            specs.mkdir(parents=True)
            (specs / "plan.md").write_text(
                "# Plan\nstatus: draft\nSome content\n", encoding="utf-8",
            )
            result = rg._draft_specs(root)
        self.assertEqual(len(result), 1)


class MatchRoutingRulesTests(unittest.TestCase):
    def _rules_doc(self, rules):
        return {"schema_version": 1, "rules": rules, "conflict_resolution": "highest priority wins"}

    def test_no_match(self):
        doc = self._rules_doc([
            {"id": "r1", "signals": ["security audit"], "workflow": "map-security", "priority": 1},
        ])
        result = rg.match_routing_rules("fix a typo", doc)
        self.assertEqual(result, [])

    def test_single_match(self):
        doc = self._rules_doc([
            {"id": "r1", "signals": ["security audit"], "workflow": "map-security", "priority": 1},
        ])
        result = rg.match_routing_rules("run a security audit please", doc)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "r1")

    def test_highest_priority_wins(self):
        doc = self._rules_doc([
            {"id": "low", "signals": ["refactor"], "workflow": "map-refactor", "priority": 1},
            {"id": "high", "signals": ["refactor auth"], "workflow": "map-security", "priority": 5},
        ])
        result = rg.match_routing_rules("refactor auth module", doc)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "high")

    def test_tied_priority_returns_all(self):
        doc = self._rules_doc([
            {"id": "a", "signals": ["api"], "workflow": "multi-agent-pr", "priority": 3},
            {"id": "b", "signals": ["api"], "workflow": "map-refactor", "priority": 3},
        ])
        result = rg.match_routing_rules("update the api", doc)
        self.assertEqual(len(result), 2)

    def test_empty_text(self):
        doc = self._rules_doc([
            {"id": "r1", "signals": ["test"], "workflow": "multi-agent-pr", "priority": 1},
        ])
        result = rg.match_routing_rules("", doc)
        self.assertEqual(result, [])

    def test_case_insensitive_match(self):
        doc = self._rules_doc([
            {"id": "r1", "signals": ["Security"], "workflow": "map-security", "priority": 1},
        ])
        result = rg.match_routing_rules("SECURITY review needed", doc)
        self.assertEqual(len(result), 1)


class RoutingHintsTests(unittest.TestCase):
    def test_active_session_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"active": True, "workflow": "multi-agent-pr", "session_id": "s1"}
            hints = rg._routing_hints(Path(tmp), config, "")
        self.assertTrue(any("Active MAP session" in h for h in hints))

    def test_no_config_no_active_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            hints = rg._routing_hints(Path(tmp), None, "")
        self.assertFalse(any("Active MAP session" in h for h in hints))

    def test_hyperplan_keyword_triggers_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            hints = rg._routing_hints(Path(tmp), None, "let's do a hyperplan")
        self.assertTrue(any("Hyperplan detected" in h for h in hints))

    def test_hyperplan_suppressed_when_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"active": True, "workflow": "map-hyperplan", "session_id": "s2"}
            hints = rg._routing_hints(Path(tmp), config, "hyperplan this")
        self.assertFalse(any("Hyperplan detected without" in h for h in hints))

    def test_chinese_keywords_trigger_hyperplan(self):
        with tempfile.TemporaryDirectory() as tmp:
            hints = rg._routing_hints(Path(tmp), None, "帮我写个架构方案")
        self.assertTrue(any("Hyperplan detected" in h for h in hints))

    def test_draft_specs_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / ".specs"
            specs.mkdir()
            (specs / "feature.md").write_text(
                "---\nstatus: draft\n---\n# Feature\n", encoding="utf-8",
            )
            hints = rg._routing_hints(root, None, "")
        self.assertTrue(any("Draft specs" in h for h in hints))


class SessionResumeTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._bootstrap_secret(rg)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.branch, self.head = _init_git_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        self._clear_secret_env()

    def _write_review_state(self, *, active: bool = True, phase: str = "coding") -> None:
        review = self.root / ".review"
        review.mkdir(parents=True, exist_ok=True)
        config = {"active": active, "workflow": "multi-agent-pr", "session_id": "sess-resume"}
        (review / "config.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        progress = {"phase": phase, "branch": self.branch, "completed": ["coder-1"]}
        (review / "progress.json").write_text(json.dumps(progress) + "\n", encoding="utf-8")

    def test_no_context_returns_empty(self):
        result = rg.session_resume_from_hook({"cwd": "/nonexistent"})
        self.assertEqual(result, {})

    def test_no_active_config_returns_empty(self):
        result = rg.session_resume_from_hook({"cwd": str(self.root)})
        self.assertEqual(result, {})

    def test_active_session_with_progress(self):
        self._write_review_state()
        data = {"cwd": str(self.root)}
        result = rg.session_resume_from_hook(data)
        ctx = result.get("additional_context", "")
        self.assertIn("Interrupted MAP pipeline", ctx)
        self.assertIn("sess-resume", ctx)
        self.assertIn("coding", ctx)

    def test_branch_mismatch_no_resume_hint(self):
        review = self.root / ".review"
        review.mkdir(parents=True, exist_ok=True)
        config = {"active": True, "workflow": "multi-agent-pr", "session_id": "s-other"}
        (review / "config.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        progress = {"phase": "testing", "branch": "other-branch", "completed": []}
        (review / "progress.json").write_text(json.dumps(progress) + "\n", encoding="utf-8")
        data = {"cwd": str(self.root)}
        result = rg.session_resume_from_hook(data)
        ctx = result.get("additional_context", "")
        self.assertNotIn("Interrupted MAP pipeline", ctx)


if __name__ == "__main__":
    unittest.main()
