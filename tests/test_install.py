#!/usr/bin/env python3
"""Tests for scripts/install.py (Phase 2)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        gate = Path("/new/review_gate.py")
        map_hooks = omc_install.render_map_hooks(gate)
        merged = omc_install.merge_hooks(existing, map_hooks, review_gate_path=gate)
        omc_install.validate_non_map_preserved(existing, merged, review_gate_path=gate)
        pre = merged["hooks"]["preToolUse"]
        self.assertEqual(pre[0]["command"], "rtk hook cursor")
        self.assertTrue(all("review_gate.py" in e["command"] for e in pre[1:]))

    def test_idempotent_merge(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        map_hooks = omc_install.render_map_hooks(gate)
        existing = {"version": 1, "hooks": {}}
        first = omc_install.merge_hooks(existing, map_hooks, review_gate_path=gate)
        second = omc_install.merge_hooks(first, map_hooks, review_gate_path=gate)
        self.assertEqual(first, second)

    def test_substring_hook_not_treated_as_map(self):
        gate = Path("/new/review_gate.py")
        existing = {
            "hooks": {
                "preToolUse": [
                    {"command": "test -f /path/review_gate.py && echo ok", "matcher": "Shell"}
                ]
            }
        }
        map_hooks = omc_install.render_map_hooks(gate)
        merged = omc_install.merge_hooks(existing, map_hooks, review_gate_path=gate)
        pre = merged["hooks"]["preToolUse"]
        self.assertEqual(pre[0]["command"], "test -f /path/review_gate.py && echo ok")

    def test_subagent_stop_env_prefix_treated_as_map(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        legacy_stop = {
            "command": (
                f"REVIEW_GATE_HOOK_MODE=subagentStop python3 {gate} record-subagent"
            ),
            "matcher": "coder",
        }
        self.assertTrue(omc_install.is_map_hook_entry(legacy_stop, review_gate_path=gate))
        existing = {"hooks": {"subagentStop": [legacy_stop]}}
        map_hooks = omc_install.render_map_hooks(gate)
        merged = omc_install.merge_hooks(existing, map_hooks, review_gate_path=gate)
        stop = merged["hooks"]["subagentStop"]
        self.assertEqual(len(stop), 1)
        self.assertTrue(all(omc_install.is_map_hook_entry(e, gate) for e in stop))
        self.assertTrue(all(e.get("omc") is True for e in stop))

    def test_expected_map_hook_count_matches_template_dedupe(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        rendered = omc_install.render_map_hooks(gate)
        expected = omc_install.expected_map_hook_count(gate)
        raw = sum(
            len(entries)
            for entries in rendered.get("hooks", {}).values()
            if isinstance(entries, list)
        )
        deduped = sum(
            len(omc_install._dedupe_hook_entries(entries, gate))
            for entries in rendered.get("hooks", {}).values()
            if isinstance(entries, list)
        )
        self.assertEqual(raw, deduped)
        self.assertEqual(expected, 11)

    def test_rendered_commands_are_quoted(self):
        gate = Path("/opt/with space/review_gate.py")
        rendered = omc_install.render_map_hooks(gate)
        shell_cmds = [
            e["command"]
            for e in rendered["hooks"]["preToolUse"]
            if e.get("matcher") == "Shell"
        ]
        self.assertTrue(any("with space" in cmd for cmd in shell_cmds))
        self.assertTrue(all("python3 " in cmd for cmd in shell_cmds))

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

    def test_normalizes_supported_hook_versions(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        map_hooks = omc_install.render_map_hooks(gate)
        for existing in ({"hooks": {}}, {"version": "1", "hooks": {}}, {"version": 1, "hooks": {}}):
            with self.subTest(existing=existing):
                merged = omc_install.merge_hooks(existing, map_hooks, review_gate_path=gate)
                self.assertIs(type(merged["version"]), int)
                self.assertEqual(merged["version"], 1)

    def test_rejects_unsupported_hook_versions(self):
        gate = Path("/opt/omc/hooks/review_gate.py")
        map_hooks = omc_install.render_map_hooks(gate)
        for version in (True, 1.0, 2, "2", None):
            with self.subTest(version=version):
                with self.assertRaises(omc_install.InstallError):
                    omc_install.merge_hooks(
                        {"version": version, "hooks": {}},
                        map_hooks,
                        review_gate_path=gate,
                    )


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
            self.assertIn(omc_install.GITIGNORE_MAP_MARKER, gitignore)
            self.assertIn(".review/", gitignore)

    def test_ensure_project_gitignore_map_block_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "myrepo"
            project.mkdir()
            gitignore = project / ".gitignore"
            gitignore.write_text(f"{omc_install.GITIGNORE_SECRET_LINE}\n", encoding="utf-8")
            omc_install.ensure_project_gitignore(project)
            content = gitignore.read_text(encoding="utf-8")
            self.assertIn(omc_install.GITIGNORE_MAP_MARKER, content)
            self.assertEqual(content.count(omc_install.GITIGNORE_MAP_MARKER), 1)
            omc_install.ensure_project_gitignore(project)
            self.assertEqual(
                gitignore.read_text(encoding="utf-8").count(omc_install.GITIGNORE_MAP_MARKER),
                1,
            )

    @patch.object(omc_install, "run_doctor", return_value=1)
    def test_install_fails_when_doctor_fails(self, _doctor):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            cursor_dir.mkdir()
            with self.assertRaises(omc_install.InstallError) as ctx:
                omc_install.install("copy", target=cursor_dir)
            self.assertIn("doctor failed", str(ctx.exception))

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



class DoctorModelsConfigTests(unittest.TestCase):
    def test_doctor_passes_with_valid_models_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            hooks = cursor_dir / "hooks"
            hooks.mkdir(parents=True)
            gate = hooks / "review_gate.py"
            gate.write_text("# stub\n", encoding="utf-8")
            config_dir = hooks / "config"
            config_dir.mkdir()
            (config_dir / "models.json").write_text(
                json.dumps(
                    {
                        "reviewers": {
                            "reviewer-grok": {"model": "grok-4.5"},
                            "reviewer-codex": {"model": "gpt-5.3-codex-high-fast"},
                            "reviewer-gemini": {"model": "gemini-3.1-pro"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (cursor_dir / "hooks.json").write_text(
                json.dumps({"version": 1, "hooks": {}}), encoding="utf-8"
            )
            (cursor_dir / "omc-install.json").write_text(
                json.dumps(
                    {
                        "mode": "copy",
                        "installed_at": "2026-01-01T00:00:00Z",
                        "review_gate_path": str(gate),
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(omc_install, "expected_map_hook_count", return_value=0):
                rc = omc_install.run_doctor(cursor_dir)
            self.assertEqual(rc, 0)

            for version_payload in ({}, {"version": True}, {"version": "1"}, {"version": 2}):
                with self.subTest(version_payload=version_payload):
                    (cursor_dir / "hooks.json").write_text(
                        json.dumps({**version_payload, "hooks": {}}),
                        encoding="utf-8",
                    )
                    with patch.object(omc_install, "expected_map_hook_count", return_value=0):
                        rc = omc_install.run_doctor(cursor_dir)
                    self.assertEqual(rc, 1)

    def test_doctor_fails_when_models_json_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_dir = Path(tmp) / ".cursor"
            hooks = cursor_dir / "hooks"
            hooks.mkdir(parents=True)
            gate = hooks / "review_gate.py"
            gate.write_text("# stub\n", encoding="utf-8")
            (cursor_dir / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
            (cursor_dir / "omc-install.json").write_text(
                json.dumps(
                    {
                        "mode": "copy",
                        "installed_at": "2026-01-01T00:00:00Z",
                        "review_gate_path": str(gate),
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(omc_install, "expected_map_hook_count", return_value=0):
                rc = omc_install.run_doctor(cursor_dir)
            self.assertEqual(rc, 1)

if __name__ == "__main__":
    unittest.main()
