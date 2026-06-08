#!/usr/bin/env python3
"""Security tests for migrate_map_state rollback path bounds."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_migrate_map_state():
    spec = importlib.util.spec_from_file_location(
        "migrate_map_state", SCRIPTS_DIR / "migrate_map_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RollbackBoundsTests(unittest.TestCase):
    def setUp(self):
        self.migrate = load_migrate_map_state()

    def test_rollback_rejects_escaped_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evil = Path("/tmp/evil")
            manifest = {
                "schema_version": 1,
                "repo": str(root),
                "actions": [
                    {
                        "kind": "verdict",
                        "source": str(root / ".review-verdict.json"),
                        "dest": str(evil),
                        "mode": "copy",
                    }
                ],
            }
            manifest_path = root / ".review" / "migrate-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(self.migrate.MigrateError) as ctx:
                self.migrate._rollback(manifest_path)
            self.assertIn("rollback dest escapes repo", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
