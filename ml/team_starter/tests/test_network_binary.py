"""SYNTHETIC TEST FIXTURES ONLY: no sample here is real incident evidence."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from ml.team_starter.network_binary import (REVIEW_VERSION, validate_reviews, task_data,
    chronological_run_split, rule_candidate, run, score)
from ml.team_starter.feature_views import AWS_COLUMNS, RIPE_COLUMNS, GROUPS


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def fixture(n=14):
    t = 1_800_000_000
    episodes, rows, reviews = [], [], []
    for i in range(n):
        start = t + i * 6000
        # Ordered runs by alternating status, with full 900-second embargo.
        target = "normal" if i % 2 == 0 else "network_degradation"
        label = "healthy" if target == "normal" else "network_degradation"
        eid, runid = f"fixture-episode-{i}", f"fixture-run-{i}"
        episodes.append({"episode_id": eid, "experiment_run_id": runid,
            "label": label, "region": "region-a", "verified_start_utc": iso(start),
            "verified_end_utc": iso(start+1000), "reviewed_at_utc": iso(start+1100),
            "reviewed": True, "reviewer": "fictional-test-only",
            "independent_evidence_refs": ["FAKE:unit-test-only"], "independent_vantage_count": 2})
        reviews.append({"episode_id": eid, "experiment_run_id": runid, "network_state": target,
            "reviewed": True, "reviewer": "fictional-test-only",
            "reviewed_at_utc": iso(start+1100), "network_evidence_refs": ["FAKE:unit-test-only"],
            "independent_network_vantage_count": 2,
            "observed_network_impact_verified": target=="network_degradation"})
        for j in range(5):
            stamp = start + 100 + j*150
            views = {}
            for group in GROUPS:
                cols = AWS_COLUMNS + (RIPE_COLUMNS if group != "aws_only" else ())
                values = {f"{r}.{col}": 0.0 for r in ("region-a", "region-b") for col in cols}
                for r in ("region-a", "region-b"):
                    values[f"{r}.app_up"] = 1
                    values[f"{r}.memory_available_bytes"] = 10_000_000
                    values[f"{r}.memory_total_bytes"] = 20_000_000
                    values[f"{r}.cpu_busy_pct"] = 75 if target=="network_degradation" else 5
                    values[f"{r}.http_p95_ms"] = 900 if target=="network_degradation" else 45
                    if group != "aws_only":
                        values[f"{r}.packet_loss_pct"] = 70 if target=="network_degradation" else 0
                        values[f"{r}.ripe_probes_reporting"] = 5
                views[group] = {"values": values, "missing": {k:False for k in values},
                                "ready_for_shadow_assessment": True}
            rows.append({"episode_id": eid, "snapshot_id": f"features-TEST-fixture-{i}-{j}",
                         "label": label, "at_unix": stamp, "views": views})
    return ({"contract_version": "adaptivenet.offline_dataset.v1", "rows": rows},
            {"contract_version": "adaptivenet.verified_episodes.v1", "episodes": episodes},
            {"contract_version": REVIEW_VERSION, "reviews": reviews})


class NetworkBinaryTests(unittest.TestCase):
    def test_no_network_review_does_not_infer_normal_or_positive(self):
        ds, labs, _ = fixture()
        d = task_data(ds,labs,{"contract_version":REVIEW_VERSION,"reviews":[]})
        self.assertEqual(d["rows"],[])
        with tempfile.TemporaryDirectory() as tmp:
            result=run(ds,labs,{"contract_version":REVIEW_VERSION,"reviews":[]},Path(tmp))
        self.assertEqual(result["status"],"not_trained")
        self.assertEqual(result["network_labeled_rows"],0)

    def test_refuses_single_vantage_even_if_three_endpoints(self):
        _, labs, revs=fixture()
        revs["reviews"][1]["independent_network_vantage_count"]=1
        with self.assertRaisesRegex(ValueError,"independent network vantages"):
            validate_reviews(revs,labs)

    def test_unreviewed_and_no_network_effect_rejected(self):
        _, labs, revs=fixture()
        revs["reviews"][1]["reviewed"]=False
        with self.assertRaisesRegex(ValueError,"network-specific review"):
            validate_reviews(revs,labs)
        revs["reviews"][1]["reviewed"]=True
        revs["reviews"][1]["observed_network_impact_verified"]=False
        with self.assertRaisesRegex(ValueError,"verified network impact"):
            validate_reviews(revs,labs)

    def test_review_missing_evidence_rejected(self):
        _, labs, revs=fixture()
        revs["reviews"][1]["network_evidence_refs"]=[]
        with self.assertRaisesRegex(ValueError,"evidence"):
            validate_reviews(revs,labs)

    def test_healthy_cannot_be_positive_and_degradation_cannot_be_normal(self):
        _,labs,revs=fixture()
        revs["reviews"][0]["network_state"]="network_degradation"
        with self.assertRaisesRegex(ValueError,"contradictory"):
            validate_reviews(revs,labs)
        revs["reviews"][0]["network_state"]="normal"
        revs["reviews"][1]["network_state"]="normal"
        with self.assertRaisesRegex(ValueError,"silently relabel"):
            validate_reviews(revs,labs)

    def test_no_application_failure_automatically_negative(self):
        ds, labs, revs=fixture()
        labs["episodes"][0]["label"]="application_failure"
        for row in ds["rows"]:
            if row["episode_id"] == "fixture-episode-0":
                row["label"] = "application_failure"
        # Only specifically network-reviewed episodes become negatives, no auto-inference.
        revs["reviews"] = revs["reviews"][1:]
        data=task_data(ds,labs,revs)
        self.assertTrue(all(r["episode_id"]!='fixture-episode-0' for r in data["rows"]))

    def test_same_experiment_phases_not_split(self):
        eps=[{"episode_id":f"p{i}","experiment_run_id":f"run{i}",
              "start_unix":100000+i*5000,"end_unix":100500+i*5000} for i in range(10)]
        # Different phase of same late-run starts near the first run.
        eps.append({"episode_id":"p0-recovery","experiment_run_id":"run0",
                    "start_unix":eps[1]["start_unix"]+100,"end_unix":eps[1]["start_unix"]+500})
        split=chronological_run_split(eps)
        for name in ("train","validation","test"):
            if "p0" in split[name]:self.assertIn("p0-recovery",split[name])
            if "p0-recovery" in split[name]:self.assertIn("p0",split[name])

    def test_dataset_labels_must_match_original(self):
        ds,labs,revs=fixture()
        ds["rows"][0]["label"]="application_failure"
        with self.assertRaisesRegex(ValueError,"label mismatch"):
            task_data(ds,labs,revs)

    def test_no_leakage_of_ripe_into_aws_only(self):
        ds,labs,revs=fixture()
        data=task_data(ds,labs,revs)
        first=data["rows"][0]
        self.assertFalse(any("ripe" in k or "packet_loss" in k or "network_rtt" in k
                             for k in first["views"]["aws_only"]["values"]))

    def test_true_missing_sensor_values_stay_missing(self):
        ds,labs,revs=fixture()
        ds["rows"][0]["views"]["aws_ripe"]["values"]["region-a.packet_loss_pct"]=None
        ds["rows"][0]["views"]["aws_ripe"]["missing"]["region-a.packet_loss_pct"]=True
        data=task_data(ds,labs,revs)
        from ml.team_starter.training import vector
        feature_names=sorted(data["rows"][0]["views"]["aws_ripe"]["values"])
        arr=vector(data["rows"][0],"aws_ripe",feature_names)
        self.assertTrue(any(__import__('math').isnan(x) for x in arr))

    def test_metric_missing_prediction_is_not_success(self):
        rows=[{"label":"network_degradation", "episode_id":"f", "at_unix":100},
              {"label":"normal", "episode_id":"n", "at_unix":101}]
        result=score(rows,[None,"normal"],{"f":90,"n":100})
        self.assertEqual(result["missed_incidents"],1)
        self.assertEqual(result["recall"],0.)
        self.assertEqual(result["abstentions"],1)

    def test_trains_only_on_synthetic_test_fixture_and_preserves_binary_classes(self):
        ds,labs,revs=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result=run(ds,labs,revs,Path(tmp))
            self.assertEqual(result["status"],"trained",result.get("blockers"))
            for group in GROUPS:
                item=result["groups"][group]
                self.assertEqual(item["status"],"trained")
                self.assertIn("f1",item["test"])
                self.assertTrue(Path(item["model_artifact"]).exists())
                import pickle
                with open(item["model_artifact"],"rb") as fh:
                    model=pickle.load(fh) # generated inside synthetic unit test only
                self.assertEqual(set(model["classes"]),{"normal","network_degradation"})


if __name__ == "__main__":
    unittest.main()
