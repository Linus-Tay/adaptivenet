"""Step 5.1 synthetic-only missing/stale sensor regression tests.

All values are deliberately fabricated TEST FIXTURES. Not native observations,
not a real outage, and never eligible for training or incident evaluation.
"""
from __future__ import annotations
import math
import pickle
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

from ml.team_starter.dataset import make_dataset
from ml.team_starter.feature_views import AWS_COLUMNS, RIPE_COLUMNS
from ml.team_starter.sensor_readiness import external_sensor_ready
from ml.team_starter.training import vector
from ml.team_starter.shadow import shadow_predict, shadow_review
from adaptivenet.ingestion.fast_signals import build_fast_signals
from adaptivenet.ingestion.handoff import conservative_rule_baseline, build_model_input


class NeverPredict:
    """Fixture model: any attempted inference means an unsafe readiness bypass."""
    def predict_proba(self, _):
        raise AssertionError("should abstain before calling the fixture model")


class FixedPredict:
    """Fixture model only; not a trained native classifier."""
    def predict_proba(self, _):
        import numpy as np
        return np.array([[0.51, 0.49]])


def model_path(tmp, model, group="aws_ripe"):
    import sklearn
    from ml.team_starter.feature_views import project
    t = time.time()
    slow = build_model_input(sample_snapshot(t), mode="combined", now=t)
    names = sorted(project(slow, group)["values"])
    artifact = {"artifact_version": "adaptivenet.offline_model.v1",
                "scikit_learn_version": sklearn.__version__,
                "model_id": "synthetic-test-only", "group": group,
                "classes": ["healthy", "network_degradation"],
                "min_confidence": .9, "feature_names": names,
                "model": model}
    out = Path(tmp) / "fixture-only.pkl"
    out.write_bytes(pickle.dumps(artifact))
    return out


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def sample_snapshot(t):
    raw = {key: None for key in AWS_COLUMNS + RIPE_COLUMNS}
    raw.update({"app_up": 1, "memory_available_bytes": 750, "memory_total_bytes": 1000,
                "memory_available_pct": 75, "cpu_busy_pct": 4, "http_rps": 2,
                "http_5xx_rps": 0, "http_error_ratio": 0, "http_p95_ms": 20,
                "packet_loss_pct": 0, "network_rtt_p50_ms": 30,
                "network_rtt_p95_ms": 45, "ripe_probes_reporting": 4})
    regions = {}
    for name in ("region-a", "region-b"):
        regions[name] = {
            "features": dict(raw),
            "quality": {"internal": "fresh", "external": "fresh"},
            "provenance": {"ripe_atlas": {"source": "ripe_atlas", "latest_observed_at": iso(t-180)}},
        }
    return {
        "contract_version": "adaptivenet.features.v1", "snapshot_id": "features-fixture-only-001",
        "computed_at_unix": t, "ready_for_model": True,
        "ready_for_internal_only_model": True, "regions": regions,
        "comparison": {}, "cloudfront": {},
    }


def verified_fixture(t):
    return {"contract_version": "adaptivenet.verified_episodes.v1", "episodes": [{
        "episode_id": "synthetic-test-only", "experiment_run_id": "synthetic-run-only",
        "label": "healthy", "region": "region-a",
        "verified_start_utc": iso(t-50), "verified_end_utc": iso(t+50),
        "reviewed_at_utc": iso(t+100), "reviewed": True,
        "reviewer": "synthetic-test-only",
        "independent_evidence_refs": ["test-fixture:not-real"],
        "independent_vantage_count": 2,
    }]}


class SensorQualityTests(unittest.TestCase):
    def setUp(self):
        self.t = 1800000000  # synthetic fixture, not a real collection time

    def make_one(self, snapshot):
        result = make_dataset([snapshot], [], verified_fixture(self.t))
        self.assertEqual(len(result["rows"]), 1)
        return result["rows"][0]

    def test_valid_zero_loss_does_not_count_as_missing(self):
        row = self.make_one(sample_snapshot(self.t))
        self.assertTrue(row["views"]["aws_only"]["ready_for_shadow_assessment"])
        self.assertTrue(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])
        self.assertEqual(row["views"]["aws_ripe"]["values"]["region-a.packet_loss_pct"], 0)
        self.assertFalse(row["views"]["aws_ripe"]["missing"]["region-a.packet_loss_pct"])

    def test_missing_ripe_with_stale_quality_flag_abstains_in_external_arm(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["features"]["packet_loss_pct"] = None
        row = self.make_one(snap)
        self.assertTrue(row["views"]["aws_only"]["ready_for_shadow_assessment"])
        self.assertFalse(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])
        self.assertTrue(row["views"]["aws_ripe"]["missing"]["region-a.packet_loss_pct"])

    def test_future_ripe_source_is_masked_and_cannot_be_ready(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["provenance"]["ripe_atlas"]["latest_observed_at"] = iso(self.t+10)
        row = self.make_one(snap)
        self.assertTrue(row["views"]["aws_only"]["ready_for_shadow_assessment"])
        self.assertFalse(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])
        self.assertIsNone(row["views"]["aws_ripe"]["values"]["region-a.packet_loss_pct"])

    def test_stale_ripe_source_rejected(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["provenance"]["ripe_atlas"]["latest_observed_at"] = iso(self.t-2101)
        row = self.make_one(snap)
        self.assertFalse(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])

    def test_too_few_probes_rejected_even_if_quality_says_fresh(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["features"]["ripe_probes_reporting"] = 2
        row = self.make_one(snap)
        self.assertFalse(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])

    def test_missing_internal_required_value_blocks_all_groups(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-b"]["features"]["app_up"] = None
        row = self.make_one(snap)
        for group in ("aws_only", "aws_ripe", "aws_ripe_http"):
            self.assertFalse(row["views"][group]["ready_for_shadow_assessment"])

    def test_http_stale_single_vantage_is_not_fast_ready(self):
        events = [{"timestamp": iso(self.t-46), "scope": scope,
                   "outcome": "ok", "http_status": 200, "latency_ms": 12}
                  for scope in ("cloudfront", "region-a", "region-b")]
        fast = build_fast_signals(events, now=self.t)
        self.assertFalse(fast["ready_for_fast_assessment"])
        self.assertEqual(fast["independent_vantage_count"], 1)
        snap = sample_snapshot(self.t)
        row = make_dataset([snap], events, verified_fixture(self.t))["rows"][0]
        self.assertFalse(row["views"]["aws_ripe_http"]["ready_for_shadow_assessment"])
        self.assertTrue(row["views"]["aws_ripe"]["ready_for_shadow_assessment"])

    def test_future_http_event_is_never_reused_at_earlier_cutoff(self):
        events = [{"timestamp": iso(self.t + 10), "scope": scope,
                   "outcome": "network_error", "http_status": None, "latency_ms": None}
                  for scope in ("cloudfront", "region-a", "region-b")]
        row = make_dataset([sample_snapshot(self.t)], events, verified_fixture(self.t))["rows"][0]
        view = row["views"]["aws_ripe_http"]
        self.assertFalse(view["ready_for_shadow_assessment"])
        self.assertIsNone(view["values"]["http.region-a.60s.failure_ratio"])
        self.assertTrue(view["missing"]["http.region-a.60s.failure_ratio"])

    def test_missingness_mask_and_vector_distinguish_null_and_zero(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["features"]["cpu_busy_pct"] = None
        row = self.make_one(snap)
        group = "aws_only"
        values = row["views"][group]["values"]
        names = sorted(values)
        encoded = vector(row, group, names)
        pos = names.index("region-a.cpu_busy_pct")
        pos_zero = names.index("region-a.http_5xx_rps")
        self.assertTrue(math.isnan(encoded[pos]))
        self.assertEqual(encoded[len(names)+pos], 1.0)
        self.assertEqual(encoded[pos_zero], 0.0)
        self.assertEqual(encoded[len(names)+pos_zero], 0.0)

    def test_shadow_masks_future_ripe_even_when_old_quality_flag_is_fresh(self):
        now = time.time()
        snap = sample_snapshot(now)
        snap["regions"]["region-a"]["provenance"]["ripe_atlas"]["latest_observed_at"] = iso(now+15)
        slow = build_model_input(snap, mode="combined", now=now)
        with tempfile.TemporaryDirectory() as tmp:
            path = model_path(tmp, NeverPredict())
            with patch("ml.team_starter.shadow.time.time", return_value=now):
                proposal = shadow_predict(slow, path)
        self.assertEqual(proposal["action"], "investigate")
        self.assertIn("source_quality_not_ready; abstained", proposal["evidence"])

    def test_low_confidence_shadow_abstains_and_reviewer_executes_nothing(self):
        now = time.time()
        snap = sample_snapshot(now)
        slow = build_model_input(snap, mode="combined", now=now)
        with tempfile.TemporaryDirectory() as tmp:
            path = model_path(tmp, FixedPredict())
            with patch("ml.team_starter.shadow.time.time", return_value=now):
                proposal = shadow_predict(slow, path)
        self.assertEqual(proposal["action"], "investigate")
        self.assertIn("prediction_confidence_below_threshold; abstained", proposal["evidence"])
        review = shadow_review(snap, proposal)
        self.assertTrue(review["policy_valid"])
        self.assertFalse(review["approved_for_execution"])
        self.assertFalse(review["executed"])
        self.assertEqual(review["effective_action"], "hold")

    def test_unready_model_input_uses_conservative_investigate(self):
        snap = sample_snapshot(self.t)
        snap["regions"]["region-a"]["features"]["app_up"] = None
        snap["ready_for_model"] = False
        snap["ready_for_internal_only_model"] = False
        inp = build_model_input(snap, mode="combined", now=self.t)
        self.assertFalse(inp["ready_for_inference"])
        proposal = conservative_rule_baseline(inp)
        self.assertEqual(proposal["action"], "investigate")
        self.assertEqual(proposal["contract_version"], "adaptivenet.proposal.v1")


if __name__ == "__main__":
    unittest.main()
