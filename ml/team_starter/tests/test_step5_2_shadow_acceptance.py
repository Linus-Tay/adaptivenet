"""Step 5.2: synthetic-only shadow safety acceptance tests.

Fixtures are NOT native data or evidence of trained detector performance.
No calls to AWS, Docker, SQS, routing, or other external services.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptivenet.ingestion.decisions import review_proposal
from ml.team_starter.offline_cli import main as offline_main
from ml.team_starter.shadow import shadow_review


def fixture_snapshot(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "contract_version": "adaptivenet.features.v1",
        "snapshot_id": "features-synthetic-shadow-test",
        "computed_at_unix": now,
        "ready_for_model": True,
        "regions": {
            region: {
                "features": {
                    "app_up": 1,
                    "memory_available_pct": 70,
                    "cpu_busy_pct": 10,
                },
                "quality": {"internal": "fresh", "external": "fresh"},
            }
            for region in ("region-a", "region-b")
        },
    }


def fixture_proposal(snapshot: dict, *, action: str = "hold", confidence: float = 0.95) -> dict:
    return {
        "contract_version": "adaptivenet.proposal.v1",
        "snapshot_id": snapshot["snapshot_id"],
        "action": action,
        "confidence": confidence,
        "evidence": ["synthetic-test-fixture-only"],
        "model_id": "synthetic-test-not-a-trained-model",
    }


def assert_shadow_only(test: unittest.TestCase, review: dict) -> None:
    test.assertEqual(review["review_version"], "adaptivenet.decision_review.v1")
    test.assertFalse(review["approved_for_execution"])
    test.assertFalse(review["executed"])
    test.assertEqual(review["effective_action"], "hold")
    test.assertTrue(review["required_future_gates"])


class ShadowSafetyAcceptanceTests(unittest.TestCase):
    def test_hold_proposal_valid_but_never_executes(self):
        snap = fixture_snapshot()
        decision = shadow_review(snap, fixture_proposal(snap))
        self.assertTrue(decision["policy_valid"])
        self.assertFalse(decision["eligible_for_operator_review"])
        assert_shadow_only(self, decision)

    def test_investigate_proposal_valid_but_never_executes(self):
        snap = fixture_snapshot()
        decision = shadow_review(snap, fixture_proposal(snap, action="investigate", confidence=0.5))
        self.assertTrue(decision["policy_valid"])
        assert_shadow_only(self, decision)

    def test_shift_traffic_can_be_eligible_for_review_but_not_execution(self):
        snap = fixture_snapshot()
        proposal = fixture_proposal(snap, action="shift_traffic")
        proposal["target_region_a_percent"] = 40  # valid delta, healthy destination
        decision = shadow_review(snap, proposal)
        self.assertTrue(decision["policy_valid"])
        self.assertTrue(decision["eligible_for_operator_review"])
        assert_shadow_only(self, decision)

    def test_mismatched_snapshot_is_rejected_by_independent_reviewer(self):
        snap = fixture_snapshot()
        bad = fixture_proposal(snap)
        bad["snapshot_id"] = "features-unrelated"
        direct = review_proposal(snap, bad)
        self.assertIn("snapshot_id_mismatch", direct["rejection_reasons"])
        assert_shadow_only(self, direct)
        with self.assertRaisesRegex(ValueError, "snapshot_id_mismatch"):
            shadow_review(snap, bad)

    def test_bad_contract_and_invalid_confidence_rejected(self):
        snap = fixture_snapshot()
        for patch_fields, expected in (
            ({"contract_version": "wrong"}, "proposal_contract_mismatch"),
            ({"confidence": float("nan")}, "invalid_confidence"),
            ({"confidence": True}, "invalid_confidence"),
            ({"action": "delete_all"}, "unrecognized_action"),
        ):
            with self.subTest(fields=patch_fields):
                proposal = {**fixture_proposal(snap), **patch_fields}
                decision = review_proposal(snap, proposal)
                self.assertIn(expected, decision["rejection_reasons"])
                assert_shadow_only(self, decision)

    def test_stale_snapshot_rejects_routing_even_if_otherwise_healthy(self):
        snap = fixture_snapshot(time.time() - 600)
        proposal = fixture_proposal(snap, action="shift_traffic")
        proposal["target_region_a_percent"] = 40
        decision = review_proposal(snap, proposal)
        self.assertIn("feature_snapshot_expired_or_invalid", decision["rejection_reasons"])
        assert_shadow_only(self, decision)

    def test_scale_out_rejected_until_adapter_and_gates_exist(self):
        snap = fixture_snapshot()
        proposal = fixture_proposal(snap, action="scale_out")
        decision = review_proposal(snap, proposal)
        self.assertIn("kubernetes_scale_adapter_not_implemented", decision["rejection_reasons"])
        assert_shadow_only(self, decision)

    def test_cli_invokes_real_reviewer_before_writing_shadow_result(self):
        snap = fixture_snapshot()
        proposal = fixture_proposal(snap, action="investigate")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            snapshot_path = base / "synthetic-snapshot.json"
            input_path = base / "synthetic-model-input.json"
            out_path = base / "shadow-result.json"
            snapshot_path.write_text(json.dumps(snap), encoding="utf-8")
            input_path.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
            with patch("ml.team_starter.offline_cli.shadow_predict", return_value=proposal) as predict_mock, patch(
                "ml.team_starter.offline_cli.shadow_review", wraps=shadow_review
            ) as review_mock:
                status = offline_main(["shadow", "--input", str(input_path), "--model", str(base / "fixture-only.pkl"),
                                       "--snapshot", str(snapshot_path), "--out", str(out_path)])
            self.assertEqual(status, 0)
            predict_mock.assert_called_once()
            review_mock.assert_called_once_with(snap, proposal)
            doc = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(doc["shadow_only"])
            self.assertEqual(doc["proposal"], proposal)
            assert_shadow_only(self, doc["independent_review"])

    def test_cli_rejects_mismatch_without_writing_success_output(self):
        snap = fixture_snapshot()
        forged = fixture_proposal(snap)
        forged["snapshot_id"] = "features-not-the-source-snapshot"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            snapshot_path = base / "synthetic-snapshot.json"
            input_path = base / "synthetic-input.json"
            out_path = base / "must-not-write.json"
            snapshot_path.write_text(json.dumps(snap), encoding="utf-8")
            input_path.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
            with patch("ml.team_starter.offline_cli.shadow_predict", return_value=forged):
                with self.assertRaisesRegex(ValueError, "snapshot_id_mismatch"):
                    offline_main(["shadow", "--input", str(input_path), "--model", str(base / "fixture-only.pkl"),
                                  "--snapshot", str(snapshot_path), "--out", str(out_path)])
            self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()
