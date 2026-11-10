"""TEST FIXTURES ONLY. No measurements or labels here are real training data."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from ml.team_starter.dataset import make_dataset
from ml.team_starter.evaluation import split_episodes, metrics
from ml.team_starter.feature_views import AWS_COLUMNS, RIPE_COLUMNS
from ml.team_starter.shadow import shadow_predict, shadow_review
from ml.team_starter.training import train_and_evaluate
from ml.team_starter.verified_labels import validate_manifest


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def fake_snapshot(t, *, cpu=12, loss=0, future_amp=False):
    """Completely fabricated fixtures: must never be used as real measurements."""
    columns = AWS_COLUMNS + RIPE_COLUMNS
    features = {}
    provenance = {}
    for r in ("region-a", "region-b"):
        features[r] = {col: None for col in columns}
        features[r].update({"app_up": 1, "cpu_busy_pct": cpu,
                            "memory_available_pct": 80, "memory_total_bytes": 10000000,
                            "memory_available_bytes": 8000000, "http_rps": 4,
                            "http_error_ratio": 0, "http_p95_ms": 33,
                            "packet_loss_pct": loss, "ripe_probes_reporting": 5,
                            "network_rtt_p50_ms": 30, "network_rtt_p95_ms": 40})
        provenance[r] = {
            "cpu_busy_pct": {"observed_at": iso(t + 30 if future_amp else t - 15), "source": "aws_amp"},
            "ripe_atlas": {"latest_observed_at": iso(t - 600), "source": "ripe_atlas"},
        }
    return {"contract_version": "adaptivenet.features.v1", "snapshot_id": f"features-test-{int(t)}",
            "computed_at_unix": t, "ready_for_model": True,
            "ready_for_internal_only_model": True,
            "regions": {r: {"features": features[r], "quality": {"internal": "fresh", "external": "fresh"},
                            "provenance": provenance[r]} for r in features},
            "comparison": {"rtt_p50_a_minus_b_ms": 900},
            "cloudfront": {"last_known": {"secret_bad_feature": 900}}}


def reviewed(episode_id, label, start, end, region="region-a", *, approved=True, run_id=None):
    return {"episode_id": episode_id, "experiment_run_id": run_id or episode_id,
            "label": label, "region": region,
            "verified_start_utc": iso(start), "verified_end_utc": iso(end),
            "reviewed_at_utc": iso(end + 60), "reviewed": approved,
            "reviewer": "fixture-reviewer-not-real",
            "independent_evidence_refs": ["fixture-only:not-real-evidence"],
            "independent_vantage_count": 2}


def manifest(items):
    return {"contract_version": "adaptivenet.verified_episodes.v1", "episodes": items}


class OfflineFoundationTests(unittest.TestCase):
    def setUp(self):
        self.t = 1_800_000_000  # TEST-only timestamp; not actual incident time

    def test_review_and_evidence_are_required(self):
        ep = reviewed("test", "healthy", self.t, self.t + 60, approved=False)
        with self.assertRaisesRegex(ValueError, "independent review"):
            validate_manifest(manifest([ep]))
        ep["reviewed"] = True
        ep["independent_evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "evidence"):
            validate_manifest(manifest([ep]))

    def test_partial_outage_requires_multiple_vantage_points(self):
        ep = reviewed("test", "partial_outage", self.t, self.t + 60)
        ep["independent_vantage_count"] = 1
        with self.assertRaisesRegex(ValueError, "vantage"):
            validate_manifest(manifest([ep]))

    def test_outside_verified_episode_is_not_assumed_healthy(self):
        m = manifest([reviewed("fixture-event", "network_degradation", self.t+100, self.t+300)])
        ds = make_dataset([fake_snapshot(self.t), fake_snapshot(self.t+150)], [], m)
        self.assertEqual(len(ds["rows"]), 1)
        self.assertEqual(ds["skipped_unlabeled_snapshots"], 1)
        self.assertEqual(ds["rows"][0]["label"], "network_degradation")

    def test_future_witness_and_future_amp_are_excluded(self):
        t = self.t
        events = [
            {"timestamp": iso(t-3), "scope": "region-a", "outcome": "ok", "http_status": 200, "latency_ms": 27},
            {"timestamp": iso(t+3), "scope": "region-a", "outcome": "network_error", "http_status": None},
        ]
        ds = make_dataset([fake_snapshot(t, future_amp=True)], events,
                          manifest([reviewed("fixture", "healthy", t-10, t+10)]))
        row = ds["rows"][0]
        aws = row["views"]["aws_only"]
        http = row["views"]["aws_ripe_http"]
        self.assertTrue(aws["missing"]["region-a.cpu_busy_pct"])
        self.assertTrue(aws["missing"]["region-a.cpu_busy_ewma_pct"])
        self.assertEqual(http["values"]["http.region-a.60s.failure_ratio"], 0)
        self.assertEqual(http["values"]["http.region-a.60s.count"], 1)

    def test_no_ripe_leak_into_aws_only_even_if_input_combined(self):
        t = self.t
        ds = make_dataset([fake_snapshot(t, loss=100)], [],
                          manifest([reviewed("fixture", "network_degradation", t-10, t+10)]))
        aws = ds["rows"][0]["views"]["aws_only"]
        combined = ds["rows"][0]["views"]["aws_ripe"]
        self.assertEqual(len(aws["values"]), 26)
        self.assertEqual(len(combined["values"]), 34)
        self.assertFalse(any("packet_loss" in k or "network_rtt" in k or "ripe" in k for k in aws["values"]))
        self.assertTrue(aws["ready_for_shadow_assessment"])
        self.assertTrue(combined["ready_for_shadow_assessment"])

    def test_duplicate_source_snapshot_id_does_not_gain_samples(self):
        t = self.t
        snap = fake_snapshot(t)
        ds = make_dataset([snap, snap], [], manifest([reviewed("fixture", "healthy", t-10, t+10)]))
        self.assertEqual(len(ds["rows"]), 1)

    def test_grouped_time_split_and_embargo(self):
        t = self.t
        episodes = [{"episode_id": str(i), "start_unix": t+i*2400, "end_unix": t+i*2400+100}
                    for i in range(8)]
        result = split_episodes(episodes)
        self.assertFalse(set(result["train"]) & set(result["test"]))
        self.assertTrue(result["validation"])
        episodes[1]["start_unix"] = episodes[0]["start_unix"] + 30
        result = split_episodes(episodes)
        self.assertTrue(any(set(a) & {"0", "1"} == {"0", "1"} for a in
                            (result["train"], result["validation"], result["test"])))

    def test_run_id_propagates_to_rows_and_episode_metadata(self):
        t = self.t
        m = manifest([reviewed("phase-healthy", "healthy", t-20, t+10,
                               run_id="run-001")])
        ds = make_dataset([fake_snapshot(t)], [], m)
        self.assertEqual(ds["rows"][0]["experiment_run_id"], "run-001")
        self.assertEqual(ds["episodes"][0]["experiment_run_id"], "run-001")
        self.assertTrue(ds["episodes"][0]["experiment_run_id_explicit"])

    def test_invalid_experiment_run_id_rejected(self):
        e = reviewed("phase", "healthy", self.t, self.t+20)
        e["experiment_run_id"] = "  "
        with self.assertRaisesRegex(ValueError, "experiment_run_id"):
            validate_manifest(manifest([e]))

    def test_nonadjacent_phases_and_interleaved_runs_do_not_leak(self):
        t = self.t
        episodes = [
            {"episode_id": "run-a-healthy", "experiment_run_id": "run-a",
             "start_unix": t, "end_unix": t+100},
            {"episode_id": "run-b-fault", "experiment_run_id": "run-b",
             "start_unix": t+4000, "end_unix": t+4100},
            {"episode_id": "run-a-recovery", "experiment_run_id": "run-a",
             "start_unix": t+6000, "end_unix": t+6100},
        ]
        episodes += [{"episode_id": f"run-{i}", "experiment_run_id": f"run-{i}",
                      "start_unix": t+i*4000, "end_unix": t+i*4000+100}
                     for i in range(3, 9)]
        split = split_episodes(episodes)
        partitions = ("train", "validation", "test", "excluded_for_embargo")
        membership = lambda ident: next(k for k in partitions if ident in split[k])
        self.assertEqual(membership("run-a-healthy"), membership("run-a-recovery"))
        self.assertEqual(membership("run-a-healthy"), membership("run-b-fault"))
        ids = [ident for p in partitions for ident in split[p]]
        self.assertEqual(set(ids), {e["episode_id"] for e in episodes})
        self.assertEqual(len(ids), len(set(ids)))

    def test_legacy_manifest_readable_but_cannot_train(self):
        t = self.t
        e = reviewed("old-v1", "healthy", t-20, t+20)
        del e["experiment_run_id"]
        ds = make_dataset([fake_snapshot(t)], [], manifest([e]))
        self.assertIn("verified_episodes_missing_explicit_experiment_run_id",
                      ds["limitations"])
        with tempfile.TemporaryDirectory() as dirname:
            report = train_and_evaluate(ds, Path(dirname))
        self.assertEqual(report["status"], "not_trained")
        self.assertIn("explicit experiment_run_id", report["blockers"][0])

    def test_multiple_phases_one_run_are_not_independent_training_support(self):
        t = self.t
        episodes = []
        snapshots = []
        # Two classes and enough snapshots, but every healthy phase is one run
        # and every fault phase another run. This must fail the min-2-runs gate.
        for i, (run, label) in enumerate((("one-healthy", "healthy"),
                                          ("one-fault", "network_degradation"),
                                          ("val", "healthy"), ("test", "network_degradation"))):
            start = t + i*5000
            episodes.append(reviewed(f"{run}-phase-1", label, start, start+700,
                                     run_id=run))
            episodes.append(reviewed(f"{run}-phase-2", label, start+900, start+1600,
                                     run_id=run))
            for offset in (50, 150, 250, 950, 1050, 1150):
                snapshots.append(fake_snapshot(start+offset))
        ds = make_dataset(snapshots, [], manifest(episodes))
        with tempfile.TemporaryDirectory() as dirname:
            report = train_and_evaluate(ds, Path(dirname))
        self.assertEqual(report["status"], "not_trained")
        self.assertEqual(report["verified_train_class_run_counts"].get("healthy"), 1)
        self.assertEqual(report["verified_train_class_run_counts"].get("network_degradation"), 1)
        self.assertTrue(any("independent training runs" in x for x in report["blockers"]))

    def test_empty_labels_never_train(self):
        result = train_and_evaluate({"contract_version": "adaptivenet.offline_dataset.v1",
                                     "rows": [], "episodes": [], "limitations": []}, Path("ignored"))
        self.assertEqual(result["status"], "not_trained")
        self.assertIsNone(result["model_artifact"])

    def test_trained_shadow_model_only_from_explicit_test_fixture(self):
        # Synthetic example exists ONLY inside temp directory and unit test.
        # Do not claim its scores as actual AdaptiveNet performance.
        t = time.time() - 100
        labs = ["healthy", "network_degradation", "healthy", "network_degradation",
                "healthy", "network_degradation", "healthy", "network_degradation"]
        snapshots, episodes = [], []
        for i, label in enumerate(labs):
            start = t - 30_000 + i*3000
            episodes.append(reviewed(f"synthetic-fixture-{i}", label, start, start+700))
            for j in range(4):
                snapshots.append(fake_snapshot(start+50+j*100, cpu=5 if label=="healthy" else 70,
                                               loss=0 if label=="healthy" else 80))
        ds = make_dataset(snapshots, [], manifest(episodes))
        with tempfile.TemporaryDirectory() as dirname:
            out = Path(dirname)
            result = train_and_evaluate(ds, out, group="aws_ripe")
            self.assertEqual(result["status"], "trained", result.get("blockers"))
            self.assertTrue(Path(result["model_artifact"]).is_file())
            # Live inference uses real wall-clock freshness, so historical fixture
            # must abstain even if its label is known.
            old = fake_snapshot(self.t)
            from adaptivenet.ingestion.handoff import build_model_input
            payload = build_model_input(old, mode="combined", now=self.t)
            proposal = shadow_predict(payload, Path(result["model_artifact"]))
            self.assertEqual(proposal["action"], "investigate")
            self.assertEqual(proposal["contract_version"], "adaptivenet.proposal.v1")
            self.assertFalse(shadow_review(old, proposal)["approved_for_execution"])
            fresh = fake_snapshot(time.time()-15, cpu=25, loss=0)
            fresh_input = build_model_input(fresh, mode="combined", now=time.time())
            new_proposal = shadow_predict(fresh_input, Path(result["model_artifact"]))
            self.assertIn(new_proposal["action"], ("hold", "investigate"))
            self.assertTrue(shadow_review(fresh, new_proposal)["policy_valid"])
            self.assertFalse(shadow_review(fresh, new_proposal)["executed"])

    def test_metrics_no_fake_scores_from_empty_test(self):
        self.assertEqual(metrics([], []), {"status": "no_verified_test_rows", "samples": 0})


if __name__ == "__main__":
    unittest.main()
