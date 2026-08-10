#!/usr/bin/env python3
"""Tests for phase state machine: VALID_TRANSITIONS, validate_phase_transition, safe_transition_phase, repair_phase_state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from map_test_helpers import load_review_gate


class PhaseTransitionValidationTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_valid_transition_multi_agent_pr(self):
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "config-confirmed", "spec-writing")
        )

    def test_valid_transition_coding_to_testing(self):
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "coding", "testing")
        )

    def test_invalid_transition_skip_phase(self):
        self.assertFalse(
            self.rg.validate_phase_transition("multi-agent-pr", "config-confirmed", "coding")
        )

    def test_invalid_transition_backwards(self):
        self.assertFalse(
            self.rg.validate_phase_transition("multi-agent-pr", "testing", "coding")
        )

    def test_fix_round_glob_pattern(self):
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "synthesis-complete", "fix-round-1")
        )
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "synthesis-complete", "fix-round-3")
        )

    def test_fix_round_back_to_synthesis(self):
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "fix-round-1", "synthesis-complete")
        )

    def test_hyperplan_valid_transitions(self):
        self.assertTrue(
            self.rg.validate_phase_transition("map-hyperplan", "debate", "revise")
        )
        self.assertTrue(
            self.rg.validate_phase_transition("map-hyperplan", "debate", "accepted")
        )

    def test_hyperplan_invalid_skip(self):
        self.assertFalse(
            self.rg.validate_phase_transition("map-hyperplan", "draft", "accepted")
        )

    def test_security_linear_flow(self):
        self.assertTrue(self.rg.validate_phase_transition("map-security", "scope", "hunt"))
        self.assertTrue(self.rg.validate_phase_transition("map-security", "hunt", "triage"))
        self.assertFalse(self.rg.validate_phase_transition("map-security", "scope", "poc"))

    def test_refactor_regression_to_fix_round(self):
        self.assertTrue(
            self.rg.validate_phase_transition("map-refactor", "regression", "fix-round-1")
        )

    def test_unknown_workflow_allows_any(self):
        self.assertTrue(
            self.rg.validate_phase_transition("unknown-workflow", "any-phase", "other-phase")
        )

    def test_unknown_current_phase_rejected_for_known_workflow(self):
        self.assertFalse(
            self.rg.validate_phase_transition("multi-agent-pr", "unknown-phase", "anything")
        )

    def test_fix_round_to_review_pending_valid(self):
        self.assertTrue(
            self.rg.validate_phase_transition("multi-agent-pr", "fix-round-1", "review-pending")
        )

    def test_fix_round_to_merge_ready_invalid(self):
        self.assertFalse(
            self.rg.validate_phase_transition("multi-agent-pr", "fix-round-1", "merge-ready")
        )

    def test_typo_current_phase_invalid(self):
        self.assertFalse(
            self.rg.validate_phase_transition("multi-agent-pr", "fix-rond-1", "synthesis-complete")
        )


class PhaseTransitionErrorTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_error_attributes(self):
        err = self.rg.PhaseTransitionError("multi-agent-pr", "coding", "merge-ready")
        self.assertEqual(err.workflow, "multi-agent-pr")
        self.assertEqual(err.current, "coding")
        self.assertEqual(err.target, "merge-ready")
        self.assertIn("coding", str(err))
        self.assertIn("merge-ready", str(err))


class SafeTransitionPhaseTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".review").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_transition_writes_phase(self):
        progress = {"phase": "coding", "session_id": "s1"}
        self.rg._write_json_file(self.root / ".review" / "progress.json", progress)
        result = self.rg.safe_transition_phase(self.root, "multi-agent-pr", "testing")
        self.assertTrue(result)
        updated = self.rg._read_json_file(self.root / ".review" / "progress.json")
        self.assertEqual(updated["phase"], "testing")

    def test_invalid_transition_raises(self):
        progress = {"phase": "coding", "session_id": "s1"}
        self.rg._write_json_file(self.root / ".review" / "progress.json", progress)
        with self.assertRaises(self.rg.PhaseTransitionError):
            self.rg.safe_transition_phase(self.root, "multi-agent-pr", "merge-ready")

    def test_force_bypasses_validation(self):
        progress = {"phase": "coding", "session_id": "s1"}
        self.rg._write_json_file(self.root / ".review" / "progress.json", progress)
        result = self.rg.safe_transition_phase(
            self.root, "multi-agent-pr", "merge-ready", force=True
        )
        self.assertTrue(result)
        updated = self.rg._read_json_file(self.root / ".review" / "progress.json")
        self.assertEqual(updated["phase"], "merge-ready")

    def test_empty_current_phase_allows_any(self):
        progress = {"session_id": "s1"}
        self.rg._write_json_file(self.root / ".review" / "progress.json", progress)
        result = self.rg.safe_transition_phase(self.root, "multi-agent-pr", "coding")
        self.assertTrue(result)


class RepairPhaseStateTests(unittest.TestCase):
    def setUp(self):
        self.rg = load_review_gate()

    def test_repair_coding_when_coder_done(self):
        progress = {"phase": "coding"}
        markers = [{"type": "coder"}, {"type": "architect"}]
        result = self.rg.repair_phase_state(
            Path("/tmp"), "multi-agent-pr", progress, markers
        )
        self.assertEqual(result, "testing")

    def test_repair_testing_when_tester_done(self):
        progress = {"phase": "testing"}
        markers = [{"type": "tester-writer"}]
        result = self.rg.repair_phase_state(
            Path("/tmp"), "multi-agent-pr", progress, markers
        )
        self.assertEqual(result, "review-pending")

    def test_repair_review_pending_when_all_reviewers_done(self):
        progress = {"phase": "review-pending"}
        markers = [
            {"type": "reviewer-grok"},
            {"type": "reviewer-codex"},
            {"type": "reviewer-gemini"},
        ]
        result = self.rg.repair_phase_state(
            Path("/tmp"), "multi-agent-pr", progress, markers
        )
        self.assertEqual(result, "synthesis-complete")

    def test_no_repair_needed(self):
        progress = {"phase": "coding"}
        markers = [{"type": "architect"}]
        result = self.rg.repair_phase_state(
            Path("/tmp"), "multi-agent-pr", progress, markers
        )
        self.assertIsNone(result)

    def test_repair_refactor_implement(self):
        progress = {"phase": "implement"}
        markers = [{"type": "coder"}]
        result = self.rg.repair_phase_state(
            Path("/tmp"), "map-refactor", progress, markers
        )
        self.assertEqual(result, "regression")

    def test_no_repair_for_unknown_phase(self):
        progress = {"phase": "merge-ready"}
        markers = []
        result = self.rg.repair_phase_state(
            Path("/tmp"), "multi-agent-pr", progress, markers
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
