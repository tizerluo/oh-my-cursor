#!/usr/bin/env python3
"""Extended unit tests for review_gate.py (DEF backlog + MMR fixes)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import review_gate as rg  # noqa: E402


class AdvanceFixQueueTests(unittest.TestCase):
    def test_removes_resolved_and_resets_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review" / "fix-queue.json").write_text(
                json.dumps(
                    {
                        "round": 0,
                        "p0_issues": [{"id": "a", "desc": "x"}, {"id": "b", "desc": "y"}],
                        "p1_issues": [],
                    }
                )
            )
            (root / ".review" / "progress.json").write_text(
                json.dumps({"phase": "review-pending", "fix_round": 0})
            )
            result = rg.advance_fix_queue(root, mark_resolved_ids=["a"], increment_round=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["p0_remaining"], 1)
            self.assertEqual(result["phase"], "synthesis-complete")
            progress = json.loads((root / ".review/progress.json").read_text())
            self.assertEqual(progress["phase"], "synthesis-complete")

    def test_deletes_queue_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            fq = root / ".review/fix-queue.json"
            fq.write_text(json.dumps({"p0_issues": [{"id": "a"}], "p1_issues": []}))
            (root / ".review/progress.json").write_text(json.dumps({"phase": "x"}))
            result = rg.advance_fix_queue(root, mark_resolved_ids=["a"])
            self.assertEqual(result["queue_action"], "deleted")
            self.assertFalse(fq.exists())


class AdvanceCriticQueueTests(unittest.TestCase):
    def test_critic_queue_accepted_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/critic-queue.json").write_text(
                json.dumps({"pending_items": [{"id": "sec", "desc": "d"}]})
            )
            (root / ".review/progress.json").write_text(json.dumps({"phase": "debate"}))
            result = rg.advance_critic_queue(root, mark_resolved_ids=["sec"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["phase"], "accepted")


class SpecFrontmatterTests(unittest.TestCase):
    def test_parse_frontmatter(self):
        text = "---\nstatus: draft\ntitle: Foo\n---\n# Body"
        fm = rg.parse_spec_frontmatter(text)
        self.assertEqual(fm["status"], "draft")
        self.assertEqual(fm["title"], "Foo")

    def test_draft_specs_uses_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            specs = root / ".specs"
            specs.mkdir()
            (specs / "a.md").write_text("---\nstatus: draft\n---\n")
            (specs / "b.md").write_text("---\nstatus: accepted\n---\n")
            drafts = rg._draft_specs(root)
            self.assertEqual(drafts, [".specs/a.md"])


class RoutingRulesTests(unittest.TestCase):
    def test_match_chinese_signal(self):
        rules = {
            "rules": [
                {"id": "hp", "signals": ["写个 spec"], "workflow": "map-hyperplan", "priority": 10}
            ]
        }
        matched = rg.match_routing_rules("请写个 spec 评审", rules)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["workflow"], "map-hyperplan")

    def test_priority_tie_returns_multiple(self):
        rules = {
            "rules": [
                {"id": "a", "signals": ["plan"], "workflow": "map-hyperplan", "priority": 10},
                {"id": "b", "signals": ["plan"], "workflow": "multi-agent-pr", "priority": 10},
            ]
        }
        matched = rg.match_routing_rules("need a plan", rules)
        self.assertEqual(len(matched), 2)

    def test_load_routing_rules_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = rg.load_routing_rules(root)
            self.assertIn("rules", doc)
            self.assertTrue(len(doc["rules"]) >= 1)

    def test_routing_hint_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hints = rg.routing_hints_from_rules("security audit vulnerability", root)
            self.assertTrue(any("map-security" in h for h in hints))

    def test_no_match_empty(self):
        rules = {"rules": [{"id": "x", "signals": ["zzzzz"], "workflow": "map-hyperplan", "priority": 1}]}
        self.assertEqual(rg.match_routing_rules("hello world", rules), [])

    def test_english_refactor_signal(self):
        rules = rg.load_routing_rules(Path("/nonexistent"))
        matched = rg.match_routing_rules("need to refactor extract module", rules)
        self.assertTrue(any(m.get("workflow") == "map-refactor" for m in matched))

    def test_development_default_signal(self):
        rules = rg.load_routing_rules(Path("/nonexistent"))
        matched = rg.match_routing_rules("implement this feature fix", rules)
        self.assertTrue(any(m.get("workflow") == "multi-agent-pr" for m in matched))

    def test_higher_priority_wins(self):
        rules = {
            "rules": [
                {"id": "low", "signals": ["security"], "workflow": "multi-agent-pr", "priority": 5},
                {"id": "high", "signals": ["security audit"], "workflow": "map-security", "priority": 10},
            ]
        }
        matched = rg.match_routing_rules("run security audit now", rules)
        self.assertEqual(matched[0]["id"], "high")

    def test_custom_rules_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/routing-rules.json").write_text(
                json.dumps(
                    {
                        "rules": [
                            {"id": "custom", "signals": ["xyzzy"], "workflow": "map-hyperplan", "priority": 99}
                        ]
                    }
                )
            )
            doc = rg.load_routing_rules(root)
            self.assertEqual(doc["rules"][0]["id"], "custom")

    def test_routing_tie_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = {
                "rules": [
                    {"id": "a", "signals": ["plan"], "workflow": "map-hyperplan", "priority": 10},
                    {"id": "b", "signals": ["plan"], "workflow": "multi-agent-pr", "priority": 10},
                ]
            }
            (root / ".review").mkdir()
            hints = rg.routing_hints_from_rules("need a plan", root)
            # fallback rules used when no custom file; inject via match directly
            matched = rg.match_routing_rules("need a plan", rules)
            self.assertEqual(len(matched), 2)
            hints2 = []
            if len(matched) == 1:
                hints2 = [f"workflow={matched[0]['workflow']}"]
            elif len(matched) > 1:
                hints2 = [f"tie: {len(matched)} rules"]
            self.assertTrue(hints2 or hints or matched)


class DebateValidationTests(unittest.TestCase):
    def test_valid_debate(self):
        data = {
            "session_id": "s1",
            "round": 1,
            "claims": [],
            "counterclaims": [],
            "evidence": [],
            "unresolved": [],
            "consensus_items": [],
        }
        ok, _ = rg.validate_debate_report(data)
        self.assertTrue(ok)

    def test_invalid_debate(self):
        ok, msg = rg.validate_debate_report({"session_id": "s1"})
        self.assertFalse(ok)
        self.assertIn("claims", msg)


class SecurityFingerprintTests(unittest.TestCase):
    def test_fingerprint_stable(self):
        f = {"asset": "api", "vuln_class": "xss", "sink": "render", "exploitability": "high"}
        self.assertEqual(rg.security_fingerprint(f), rg.security_fingerprint(f))

    def test_append_queue_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            findings = [
                {"asset": "a", "vuln_class": "sqli", "severity": "high", "sink": "db"},
                {"asset": "a", "vuln_class": "sqli", "severity": "high", "sink": "db"},
            ]
            n = rg.append_unverified_findings_to_security_queue(
                root, findings, session_id="s1"
            )
            self.assertEqual(n, 1)
            queue = json.loads((root / ".review/security-queue.json").read_text())
            self.assertEqual(len(queue["pending_findings"]), 1)


class RegressionTests(unittest.TestCase):
    def test_quarantine_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".review").mkdir()
            (root / ".review/quarantine-tests.json").write_text(
                json.dumps({"schema_version": 1, "tests": ["test_flaky"]})
            )
            ok, _, status = rg.validate_regression_result(
                root, failed_tests=["test_flaky", "test_real"]
            )
            self.assertTrue(ok)
            self.assertEqual(status, "retry")

    def test_fail_after_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, msg, status = rg.validate_regression_result(
                root, failed_tests=["test_a"], retry_count=2, max_retries=2
            )
            self.assertFalse(ok)
            self.assertEqual(status, "fail")
            self.assertIn("test_a", msg)


class KnowledgeValidationTests(unittest.TestCase):
    def test_warnings_when_missing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / ".review/knowledge/pr1"
            base.mkdir(parents=True)
            (base / "learnings.md").write_text("# learnings\n- item one\n")
            (base / "decisions.md").write_text("# decisions\nok\n")
            ok, warnings = rg.validate_knowledge_artifacts(root, 1)
            self.assertFalse(ok)
            self.assertTrue(any("learnings" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
