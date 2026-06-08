#!/usr/bin/env python3
"""Security tests for review_gate secret trust contract (Phase 2b)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from map_test_helpers import load_review_gate


class SecretTrustTests(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("OMC_SECRET_FILE", None)
        os.environ.pop("OMC_LEGACY_SECRET_FILE", None)

    def tearDown(self):
        self._env_patch.stop()

    def test_seal_round_trip(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            rg.bootstrap_secret(secret)
            os.environ["OMC_SECRET_FILE"] = str(secret)
            data = {"type": "coder", "subagent_id": "abc", "ok": True}
            sealed = rg._seal_marker(data)
            self.assertTrue(rg._valid_marker_seal(sealed))

    def test_forged_seal_rejected(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            rg.bootstrap_secret(secret)
            os.environ["OMC_SECRET_FILE"] = str(secret)
            sealed = rg._seal_marker({"type": "coder", "ok": True})
            sealed["seal"] = "deadbeef"
            self.assertFalse(rg._valid_marker_seal(sealed))

    def test_symlink_secret_rejected(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            real = base / "real-secret"
            real.write_bytes(b"abc123\n")
            os.chmod(real, 0o600)
            link = base / "link-secret"
            link.symlink_to(real)
            with self.assertRaises(rg.SecretError):
                rg._read_secret_bytes(link)

    def test_legacy_migration_copy(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy-secret"
            legacy.write_bytes(b"legacykey1234567890\n")
            os.chmod(legacy, 0o600)
            target = base / "nested" / "secret"
            os.environ["OMC_LEGACY_SECRET_FILE"] = str(legacy)
            copied = rg.migrate_legacy_secret_if_needed(target)
            self.assertTrue(copied)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes().strip(), b"legacykey1234567890")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_legacy_secret_env_override(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            custom_legacy = base / "custom-legacy-secret"
            custom_legacy.write_bytes(b"customlegacykey12345\n")
            os.chmod(custom_legacy, 0o600)
            target = base / "target-secret"
            os.environ["OMC_LEGACY_SECRET_FILE"] = str(custom_legacy)
            self.assertEqual(rg.legacy_secret_file_path(), custom_legacy.resolve())
            copied = rg.migrate_legacy_secret_if_needed(target)
            self.assertTrue(copied)
            self.assertEqual(target.read_bytes().strip(), b"customlegacykey12345")

    def test_mode_not_0600_rejected(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret"
            secret.write_bytes(b"abc123\n")
            os.chmod(secret, 0o644)
            with self.assertRaises(rg.SecretError):
                rg._validate_secret_stat(secret)

    def test_fail_closed_no_ephemeral_fallback(self):
        rg = load_review_gate()
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "missing" / "secret"
            os.environ["OMC_SECRET_FILE"] = str(secret)
            with patch.object(rg, "migrate_legacy_secret_if_needed", return_value=False):
                with patch.object(sys, "exit", side_effect=SystemExit(2)):
                    with self.assertRaises(SystemExit) as ctx:
                        rg._secret()
                    self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(secret.is_file())


if __name__ == "__main__":
    unittest.main()
