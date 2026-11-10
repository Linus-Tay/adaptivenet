"""Offline-only regression tests for raw AMP/snapshot reconciliation."""
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from ml.team_starter.reconcile_incident import reconcile
from ml.team_starter.verified_labels import validate_manifest


def ts(seconds):
    return (datetime(2026, 11, 9, 8, 43, tzinfo=timezone.utc) +
            timedelta(seconds=seconds)).isoformat()


def serie(job, ticks, *, region=None, val=None):
    labels = {"job": job, "project": "adaptivenet"}
    if region:
        labels["region"] = region
    return {"labels": labels,
            "samples": [{"observed_at_utc": ts(s),
                         "observed_at_unix": (datetime.fromisoformat(ts(s))).timestamp(),
                         "value": val(s) if val else 1.0} for s in ticks]}


def amp(region):
    sg = region == "region-a"
    ticks = [0, 15, 30, 45, 60]
    return {
        "report_version": "adaptivenet.historical_amp_raw_samples.v1",
        "evidence_status": "UNREVIEWED_NOT_GROUND_TRUTH", "region": region,
        "retrieved_at_utc": ts(600),
        "results": {
            "scrape_up": {"series": [
                serie("node", ticks),
                serie("adaptivenet_demo", ticks, val=lambda t: 0 if sg and t in (15, 30) else 1)
            ]},
            "app_up": {"series": [serie("adaptivenet_demo", [0, 45, 60], region=region)]},
            "http_requests": {"series": [serie("adaptivenet_demo", [0, 45, 60], region=region)]},
        }
    }


def snapshot(stamp):
    return {"contract_version": "adaptivenet.features.v1",
            "computed_at": ts(stamp), "snapshot_id": f"features-synthetic-{stamp}",
            "ready_for_model": True,
            "regions": {"region-a": {"features": {"app_up": 1.0},
                "provenance": {"app_up": {"observed_at": ts(0)}}}}}


class ReconcileIncidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sg = amp("region-a")
        self.va = amp("region-b")
        self.snapshots = [snapshot(25), snapshot(40)]
        self.http = [
            {"timestamp": ts(20), "scope": "region-a", "http_status": None},
            {"timestamp": ts(35), "scope": "region-a", "http_status": None},
            {"timestamp": ts(40), "scope": "region-a", "http_status": 200},
            {"timestamp": ts(22), "scope": "region-b", "http_status": 200},
            {"timestamp": ts(23), "scope": "cloudfront", "http_status": 200}
        ]
        self.draft = {"contract_version": "adaptivenet.verified_episodes.v1", "episodes": [{
            "episode_id": "test-failure", "experiment_run_id": "run-a",
            "label": "application_failure", "region": "region-a",
            "verified_start_utc": ts(20), "verified_end_utc": ts(40),
            "reviewed": False, "reviewer": None, "reviewed_at_utc": None,
            "independent_evidence_refs": ["source"]
        }]}
        self.sg_path = self.dir / "sg.json"
        self.va_path = self.dir / "va.json"
        self.snapshots_path = self.dir / "snapshots.jsonl"
        self.http_path = self.dir / "witness.jsonl"
        self.draft_path = self.dir / "draft.json"

    def run_reconciliation(self):
        self.sg_path.write_text(json.dumps(self.sg), encoding="utf-8")
        self.va_path.write_text(json.dumps(self.va), encoding="utf-8")
        self.snapshots_path.write_text("\n".join(json.dumps(r) for r in self.snapshots), encoding="utf-8")
        self.http_path.write_text("\n".join(json.dumps(r) for r in self.http), encoding="utf-8")
        self.draft_path.write_text(json.dumps(self.draft), encoding="utf-8")
        return reconcile(self.sg_path, self.va_path, self.snapshots_path,
                         self.http_path, self.draft_path, ssm_start_utc=ts(12))

    def test_correlated_scrape_can_justify_revised_candidate_interval_not_approval(self):
        audit, draft = self.run_reconciliation()
        self.assertEqual(audit["sg_demo_scrape_failed_count"], 2)
        self.assertEqual(audit["snapshot_count_in_revised_candidate_interval"], 1)
        self.assertEqual(audit["snapshots_in_revised_candidate_interval"][0]["reported_app_up"], 1)
        self.assertEqual(draft["episodes"][0]["verified_start_utc"], ts(15))
        self.assertEqual(draft["episodes"][0]["verified_end_utc"], ts(40))
        self.assertFalse(draft["episodes"][0]["reviewed"])
        self.assertEqual(audit["automatically_approved_training_labels"], 0)
        self.assertFalse(audit["raw_amp_eligible_as_live_model_features"])
        self.assertFalse(audit["raw_amp_receipt_times_at_original_cutoffs_known"])
        with self.assertRaisesRegex(ValueError, "independent review"):
            validate_manifest(draft)

    def test_no_retroactive_feature_replacement(self):
        original = copy.deepcopy(self.snapshots)
        audit, _ = self.run_reconciliation()
        self.assertEqual(original, self.snapshots)
        self.assertEqual(audit["snapshots_in_revised_candidate_interval"][0]["app_up_original_observed_at_utc"], ts(0))

    def test_reject_node_failure_ambiguous_attribution(self):
        self.sg["results"]["scrape_up"]["series"][0]["samples"][1]["value"] = 0
        with self.assertRaisesRegex(ValueError, "node-exporter"):
            self.run_reconciliation()

    def test_reject_unaffected_region_failure_claim(self):
        self.va["results"]["scrape_up"]["series"][1]["samples"][1]["value"] = 0
        with self.assertRaisesRegex(ValueError, "Virginia"):
            self.run_reconciliation()

    def test_reject_missing_sg_scrape_recovery(self):
        self.sg["results"]["scrape_up"]["series"][1]["samples"][3]["value"] = 0
        self.sg["results"]["scrape_up"]["series"][1]["samples"][4]["value"] = 0
        with self.assertRaisesRegex(ValueError, "no subsequent demo scrape recovery"):
            self.run_reconciliation()

    def test_reject_unreviewed_claim_that_it_was_reviewed(self):
        self.draft["episodes"][0]["reviewed"] = True
        self.draft["episodes"][0]["reviewer"] = "somebody"
        with self.assertRaisesRegex(ValueError, "already reviewed"):
            self.run_reconciliation()

    def test_reject_amp_sample_timestamp_after_retrieval(self):
        self.sg["retrieved_at_utc"] = ts(10)
        with self.assertRaisesRegex(ValueError, "after report retrieval"):
            self.run_reconciliation()

    def test_reject_missing_http_recovery(self):
        self.http = self.http[:2] + self.http[3:]
        with self.assertRaisesRegex(ValueError, "no subsequent Singapore HTTP recovery"):
            self.run_reconciliation()

    def test_reject_non_utc_timestamp(self):
        self.draft["episodes"][0]["verified_start_utc"] = "2026-11-09T08:43:20"
        # This field is not used as an evidence clock by reconciliation;
        # verify strict UTC checking on the SSM time instead.
        self.sg_path.write_text(json.dumps(self.sg), encoding="utf-8")
        self.va_path.write_text(json.dumps(self.va), encoding="utf-8")
        self.snapshots_path.write_text("\n".join(json.dumps(r) for r in self.snapshots), encoding="utf-8")
        self.http_path.write_text("\n".join(json.dumps(r) for r in self.http), encoding="utf-8")
        self.draft_path.write_text(json.dumps(self.draft), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTC"):
            reconcile(self.sg_path, self.va_path, self.snapshots_path, self.http_path,
                      self.draft_path, ssm_start_utc="2026-11-09T08:43:12")


if __name__ == "__main__":
    unittest.main()
