#!/usr/bin/env python3
"""DEF-09 dual-read / canonical-write tests (Phase 3)."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import SecretBootstrapMixin, load_review_gate

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"



def load_migrate():
    spec = importlib.util.spec_from_file_location("migrate_map_state", SCRIPTS_DIR / "migrate_map_state.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module



def write_marker_file(rg, head_dir: Path, subagent_type: str, branch: str, head_sha: str) -> None:
    head_dir.mkdir(parents=True, exist_ok=True)
    data = rg._seal_marker(
        {
            "type": subagent_type,
            "branch": branch,
            "head_sha": head_sha,
            "source": "cursor-subagentStop",
            "model": "test-model",
        }
    )
    path = head_dir / f"{subagent_type}-test.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class Def09PathTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._bootstrap_secret(self.rg)

    def tearDown(self):
        self._clear_secret_env()

    def test_read_prefers_canonical_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "abc123"
            legacy = self.rg._legacy_session_head_dir(root, branch, sha)
            canonical = self.rg._canonical_session_head_dir(root, branch, sha)
            write_marker_file(self.rg, legacy, "coder", branch, sha)
            write_marker_file(self.rg, canonical, "reviewer-grok", branch, sha)
            read_dir = self.rg._session_marker_read_dir(root, branch, sha)
            self.assertEqual(read_dir, canonical)
            types = {m["type"] for m in self.rg._marker_payloads(root, branch, sha)}
            self.assertEqual(types, {"reviewer-grok"})

    def test_read_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "feat", "def456"
            legacy = self.rg._legacy_session_head_dir(root, branch, sha)
            write_marker_file(self.rg, legacy, "coder", branch, sha)
            self.assertEqual(
                self.rg._session_marker_read_dir(root, branch, sha),
                legacy,
            )

    def test_write_dir_always_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "aaa"
            self.rg._write_marker(
                root,
                branch,
                sha,
                {
                    "type": "coder",
                    "branch": branch,
                    "head_sha": sha,
                    "source": "cursor-subagentStop",
                    "model": "m",
                },
            )
            canonical = self.rg._canonical_session_head_dir(root, branch, sha)
            self.assertTrue(any(canonical.glob("*.json")))
            legacy = self.rg._legacy_session_head_dir(root, branch, sha)
            self.assertFalse(legacy.exists())

    def test_verdict_read_write_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / self.rg.VERDICT_FILE
            legacy.write_text('{"tier":"hotfix"}\n', encoding="utf-8")
            self.assertEqual(self.rg._verdict_read_path(root), legacy)
            write_path = self.rg._verdict_write_path(root)
            self.rg._write_json_file(write_path, {"tier": "standard", "branch": "main"})
            self.assertEqual(self.rg._verdict_read_path(root), write_path)


class Def09ValidateTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        os.environ.pop("OMC_SECRET_FILE", None)

    def tearDown(self):
        self._clear_secret_env()

    def _hotfix_pass_fixtures(self, root: Path, branch: str, sha: str, marker_dir: Path, verdict_path: Path):
        write_marker_file(self.rg, marker_dir, "coder", branch, sha)
        write_marker_file(self.rg, marker_dir, "reviewer-grok", branch, sha)
        self.rg._write_json_file(
            verdict_path,
            {
                "tier": "hotfix",
                "branch": branch,
                "head_sha": sha,
                "tree_sha": "tree1",
                "p0": 0,
                "p1": 0,
                "reviewers": ["reviewer-grok"],
            },
        )

    def test_validate_legacy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "111"
            secret = root / "secret"
            self.rg.bootstrap_secret(secret)
            os.environ["OMC_SECRET_FILE"] = str(secret)
            legacy_dir = self.rg._legacy_session_head_dir(root, branch, sha)
            legacy_verdict = root / self.rg.VERDICT_FILE
            self._hotfix_pass_fixtures(root, branch, sha, legacy_dir, legacy_verdict)
            with patch.object(self.rg, "_git_tree", return_value="tree1"), patch.object(
                self.rg, "inferred_minimum_tier", return_value=("hotfix", [], "test")
            ):
                ok, msg, _ = self.rg.validate_review_state(root, branch, sha)
            self.assertTrue(ok, msg)

    def test_validate_canonical_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "222"
            secret = root / "secret"
            self.rg.bootstrap_secret(secret)
            os.environ["OMC_SECRET_FILE"] = str(secret)
            canonical_dir = self.rg._canonical_session_head_dir(root, branch, sha)
            canonical_verdict = self.rg._verdict_write_path(root)
            self._hotfix_pass_fixtures(root, branch, sha, canonical_dir, canonical_verdict)
            with patch.object(self.rg, "_git_tree", return_value="tree1"), patch.object(
                self.rg, "inferred_minimum_tier", return_value=("hotfix", [], "test")
            ):
                ok, msg, _ = self.rg.validate_review_state(root, branch, sha)
            self.assertTrue(ok, msg)


class MigrateMapStateTests(SecretBootstrapMixin, unittest.TestCase):
    def setUp(self):
        self.migrate = load_migrate()
        self.rg = load_review_gate()
        self._bootstrap_secret(self.rg)

    def tearDown(self):
        self._clear_secret_env()

    def test_dry_run_lists_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "abc"
            write_marker_file(
                self.rg,
                root / ".review-session" / branch / sha,
                "coder",
                branch,
                sha,
            )
            (root / ".review-verdict.json").write_text("{}\n", encoding="utf-8")
            manifest = self.migrate.migrate(root, apply=False, destructive=False, force=False, confirm=None)
            kinds = {a["kind"] for a in manifest["actions"]}
            self.assertIn("session_dir", kinds)
            self.assertIn("verdict", kinds)

    def test_apply_copy_leaves_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "abc"
            legacy_dir = root / ".review-session" / branch / sha
            write_marker_file(self.rg, legacy_dir, "coder", branch, sha)
            self.migrate.migrate(root, apply=True, destructive=False, force=False, confirm=None)
            self.assertTrue(legacy_dir.is_dir())
            self.assertTrue((root / ".review" / "session" / branch / sha).is_dir())

    def test_apply_aborts_if_canonical_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "abc"
            write_marker_file(
                self.rg,
                root / ".review-session" / branch / sha,
                "coder",
                branch,
                sha,
            )
            (root / ".review" / "session").mkdir(parents=True)
            with self.assertRaises(self.migrate.MigrateError):
                self.migrate.migrate(root, apply=True, destructive=False, force=False, confirm=None)

    def test_active_config_requires_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review" / "config.json").write_text('{"active": true}\n', encoding="utf-8")
            (root / ".review-session").mkdir()
            with self.assertRaises(self.migrate.MigrateError):
                self.migrate.migrate(root, apply=False, destructive=False, force=False, confirm=None)

    def test_rollback_after_apply_copy_removes_canonical_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            branch, sha = "main", "abc"
            legacy_dir = root / ".review-session" / branch / sha
            write_marker_file(self.rg, legacy_dir, "coder", branch, sha)
            self.migrate.migrate(
                root, apply=True, destructive=False, force=False, confirm=None
            )
            canonical = root / ".review" / "session" / branch / sha
            self.assertTrue(canonical.is_dir())
            manifest_path = root / ".review" / "migrate-manifest.json"
            self.assertTrue(manifest_path.is_file())
            self.migrate._rollback(manifest_path)
            self.assertFalse(canonical.exists())
            self.assertTrue(legacy_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
