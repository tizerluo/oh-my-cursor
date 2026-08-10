#!/usr/bin/env python3
"""Tests for unified queue interface: QueueManager, FixQueue, CriticQueue, SecurityQueue."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from map_test_helpers import load_review_gate


class FixQueueTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".review").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_file(self):
        q = self.rg.FixQueue(self.root)
        self.assertTrue(q.is_empty())
        self.assertEqual(q.total_count(), 0)

    def test_counts_p0_and_p1(self):
        q = self.rg.FixQueue(self.root)
        data = {
            "p0_issues": [{"id": "1"}, {"id": "2"}],
            "p1_issues": [{"id": "3"}],
            "round": 0,
        }
        self.rg._write_json_file(q.path, data)
        self.assertEqual(q.p0_count(), 2)
        self.assertEqual(q.p1_count(), 1)
        self.assertEqual(q.total_count(), 3)
        self.assertFalse(q.is_empty())

    def test_resolve_ids_removes_issues(self):
        q = self.rg.FixQueue(self.root)
        data = {
            "p0_issues": [{"id": "1"}, {"id": "2"}],
            "p1_issues": [{"id": "3"}],
        }
        self.rg._write_json_file(q.path, data)
        q.resolve_ids({"1", "3"})
        updated = q.load()
        self.assertEqual(len(updated["p0_issues"]), 1)
        self.assertEqual(updated["p0_issues"][0]["id"], "2")
        self.assertEqual(len(updated["p1_issues"]), 0)

    def test_resolve_all_deletes_file(self):
        q = self.rg.FixQueue(self.root)
        data = {"p0_issues": [{"id": "1"}], "p1_issues": []}
        self.rg._write_json_file(q.path, data)
        q.resolve_ids({"1"})
        self.assertTrue(q.is_empty())
        self.assertFalse(q.path.exists())

    def test_increment_round(self):
        q = self.rg.FixQueue(self.root)
        self.rg._write_json_file(q.path, {"round": 0, "p0_issues": [], "p1_issues": []})
        new_round = q.increment_round()
        self.assertEqual(new_round, 1)
        self.assertEqual(q.load()["round"], 1)

    def test_scope_check(self):
        q = self.rg.FixQueue(self.root)
        self.rg._write_json_file(q.path, {
            "branch": "feat/x", "head_sha": "abc123",
            "p0_issues": [], "p1_issues": [],
        })
        self.assertTrue(q.scope_check("feat/x", "abc123"))
        self.assertFalse(q.scope_check("feat/y", "abc123"))
        self.assertFalse(q.scope_check("feat/x", "def456"))


class CriticQueueTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".review").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_file(self):
        q = self.rg.CriticQueue(self.root)
        self.assertTrue(q.is_empty())
        self.assertEqual(q.pending_count(), 0)

    def test_pending_count(self):
        q = self.rg.CriticQueue(self.root)
        data = {"pending_items": [{"id": "a"}, {"id": "b"}], "round": 1}
        self.rg._write_json_file(q.path, data)
        self.assertEqual(q.pending_count(), 2)

    def test_resolve_ids(self):
        q = self.rg.CriticQueue(self.root)
        data = {"pending_items": [{"id": "a"}, {"id": "b"}, {"dimension": "c"}]}
        self.rg._write_json_file(q.path, data)
        q.resolve_ids({"a", "c"})
        updated = q.load()
        self.assertEqual(len(updated["pending_items"]), 1)
        self.assertEqual(updated["pending_items"][0]["id"], "b")

    def test_resolve_all_deletes_file(self):
        q = self.rg.CriticQueue(self.root)
        data = {"pending_items": [{"id": "a"}]}
        self.rg._write_json_file(q.path, data)
        q.resolve_ids({"a"})
        self.assertTrue(q.is_empty())
        self.assertFalse(q.path.exists())

    def test_increment_round(self):
        q = self.rg.CriticQueue(self.root)
        self.rg._write_json_file(q.path, {"pending_items": [], "round": 1})
        new_round = q.increment_round()
        self.assertEqual(new_round, 2)


class SecurityQueueTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".review").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_file(self):
        q = self.rg.SecurityQueue(self.root)
        self.assertTrue(q.is_empty())
        self.assertEqual(q.pending_count(), 0)

    def test_append_findings_dedup(self):
        q = self.rg.SecurityQueue(self.root)
        findings = [
            {"asset": "api", "vuln_class": "sqli", "sink": "query", "exploitability": "easy", "severity": "high"},
            {"asset": "api", "vuln_class": "sqli", "sink": "query", "exploitability": "easy", "severity": "high"},
        ]
        added = q.append_findings(findings, session_id="s1")
        self.assertEqual(added, 1)
        self.assertEqual(q.pending_count(), 1)

    def test_append_skips_low_severity(self):
        q = self.rg.SecurityQueue(self.root)
        findings = [
            {"asset": "api", "vuln_class": "info", "sink": "log", "exploitability": "none", "severity": "low"},
        ]
        added = q.append_findings(findings, session_id="s1")
        self.assertEqual(added, 0)

    def test_append_skips_verified(self):
        q = self.rg.SecurityQueue(self.root)
        findings = [
            {"asset": "api", "vuln_class": "xss", "sink": "render", "exploitability": "easy", "severity": "high", "verified": True},
        ]
        added = q.append_findings(findings, session_id="s1")
        self.assertEqual(added, 0)

    def test_existing_fingerprints(self):
        q = self.rg.SecurityQueue(self.root)
        findings = [
            {"asset": "api", "vuln_class": "sqli", "sink": "query", "exploitability": "easy", "severity": "high"},
        ]
        q.append_findings(findings, session_id="s1")
        fps = q.existing_fingerprints()
        self.assertEqual(len(fps), 1)

    def test_existing_fingerprints_filters_invalid_values(self):
        q = self.rg.SecurityQueue(self.root)
        q.save(
            {
                "pending_findings": [
                    {"fingerprint": "valid"},
                    {"fingerprint": ""},
                    {"fingerprint": None},
                    {"fingerprint": True},
                    {},
                ]
            }
        )
        self.assertEqual(q.existing_fingerprints(), {"valid"})


class QueueManagerCommonTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".review").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_queues_share_base_interface(self):
        for cls in (self.rg.FixQueue, self.rg.CriticQueue, self.rg.SecurityQueue):
            q = cls(self.root)
            self.assertTrue(q.is_empty())
            self.assertTrue(q.path.name.endswith(".json"))

    def test_save_sets_updated_at(self):
        q = self.rg.FixQueue(self.root)
        q.save({"p0_issues": [{"id": "1"}], "p1_issues": []})
        data = q.load()
        self.assertIn("updated_at", data)


if __name__ == "__main__":
    unittest.main()
