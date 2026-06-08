#!/usr/bin/env python3
"""Tests for scripts/install.py (Phase 2)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import install as omc_install  # noqa: E402


class MergeHooksTests(unittest.TestCase):
    def test_preserves_non_map_entries(self):
        existing = {
            "version": 1,
            "hooks": {
                "preToolUse": [
                    {"command": "rtk hook cursor", "matcher": "Shell"},
                    {
                        "command": "python3 /old/review_gate.py check-merge",
                        "matcher": "Shell",
                    },
                ]
            },
        }
        map_hooks = omc_install.render_map_hooks(Path("/new/review_gate.py"))
        merged = omc_install.merge_hooks(existing, map_hooks)
        omc_install.validate_non_map_preserved(existing, merged)
        pre = merged["hooks"]["preToolUse"]
        self.assertEqual(pre[0]["command"], "rtk hook cursor")
        self.assertTrue(all("review_gate.py" in e["command"] for e in pre[1:]))

    def test_idempotent_merge(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        map_hooks = omc_install.render_map_hooks(gate)
        existing = {"version": 1, "hooks": {}}
        first = omc_install.merge_hooks(existing, map_hooks)
        second = omc_install.merge_hooks(first, map_hooks)
        self.assertEqual(first, second)

    def test_refuses_non_map_modification(self):
        before = {
            "hooks": {
                "preToolUse": [{"command": "rtk hook cursor", "matcher": "Shell"}]
            }
        }
        after = {
            "hooks": {
                "preToolUse": [{"command": "rtk hook cursor", "matcher": "Task"}]
            }
        }
        with self.assertRaises(omc_install.InstallError):
            omc_install.validate_non_map_preserved(before, after)


class InstallIntegrationTests(unittest.TestCase):
    def test_copy_install_to_temp_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            cursor_dir.mkdir()
            hooks_json = cursor_dir / "hooks.json"
            hooks_json.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "hooks": {
                            "preToolUse": [
                                {"command": "rtk hook cursor", "matcher": "Shell"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            omc_install.install("copy", target=cursor_dir)
            data = json.loads(hooks_json.read_text(encoding="utf-8"))
            pre = data["hooks"]["preToolUse"]
            self.assertEqual(pre[0]["command"], "rtk hook cursor")
            map_cmds = [e for e in pre if omc_install.is_map_hook_entry(e)]
            self.assertGreaterEqual(len(map_cmds), 2)
            gate = cursor_dir / "hooks" / "review_gate.py"
            self.assertTrue(gate.is_file())
            self.assertTrue(all(str(gate) in e["command"] for e in map_cmds))
            manifest = json.loads((cursor_dir / "omc-install.json").read_text())
            self.assertEqual(manifest["mode"], "copy")

    def test_link_requires_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            cursor_dir.mkdir()
            with self.assertRaises(omc_install.InstallError):
                omc_install.install("link", target=cursor_dir, symlink_ack=False)

    def test_project_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "myrepo"
            project.mkdir()
            omc_install.install("copy", project=project)
            gitignore = (project / ".gitignore").read_text(encoding="utf-8")
            self.assertIn(".cursor/hooks/.review-gate-secret", gitignore)

    def test_uninstall_restores_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            cursor_dir.mkdir()
            hooks_json = cursor_dir / "hooks.json"
            original = {"version": 1, "hooks": {"stop": []}}
            hooks_json.write_text(json.dumps(original), encoding="utf-8")
            omc_install.install("copy", target=cursor_dir)
            self.assertNotEqual(json.loads(hooks_json.read_text()), original)
            backup = omc_install.uninstall_hooks_json(cursor_dir)
            self.assertTrue(backup.is_file())
            restored = json.loads(hooks_json.read_text(encoding="utf-8"))
            self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
