"""Synthetic-only unit tests: NEVER real incident or model evidence."""
import unittest
from ml.team_starter.paired_incident_report import compare_paired_incidents
from ml.team_starter.feature_views import GROUPS


def demo():
    episodes = [
        {"episode_id": "fault-synthetic", "experiment_run_id": "run-synthetic-1",
         "label": "network_degradation", "region": "region-a", "start_unix": 100, "end_unix": 200},
        {"episode_id": "healthy-synthetic", "experiment_run_id": "run-synthetic-2",
         "label": "healthy", "region": "region-a", "start_unix": 300, "end_unix": 400},
    ]
    rows = []
    for ep, times in ((episodes[0], (110, 150)), (episodes[1], (310, 350))):
        rows.extend({"episode_id": ep["episode_id"], "experiment_run_id": ep["experiment_run_id"],
                     "snapshot_id": "features-synthetic-" + str(t), "at_unix": t,
                     "region": ep["region"], "label": ep["label"]} for t in times)
    dataset = {"contract_version": "adaptivenet.offline_dataset.v1", "rows": rows, "episodes": episodes}
    arms = {}
    for arm in GROUPS:
        preds = []
        for ep, times in ((episodes[0], (110, 150)), (episodes[1], (310, 350))):
            for t in times:
                label = None
                if ep["label"] == "network_degradation":
                    if arm == "aws_only" and t == 150:
                        label = "application_failure"  # Wrong cause, still an alert
                    if arm == "aws_ripe" and t == 110:
                        label = "network_degradation"
                    if arm == "aws_ripe_http" and t == 110:
                        label = "network_degradation"
                else:
                    label = "healthy"
                preds.append({"episode_id": ep["episode_id"],
                              "experiment_run_id": ep["experiment_run_id"],
                              "snapshot_id": "features-synthetic-" + str(t),
                              "at_unix": t, "region": ep["region"], "label": ep["label"],
                              "predicted_label": label})
        arms[arm] = {"contract_version": "adaptivenet.native_test_predictions.v1",
                     "group": arm, "source": "reviewed_native_episodes", "test_only": True,
                     "rows": preds}
    return dataset, arms


class PairedIncidentTests(unittest.TestCase):
    def test_no_native_predictions_fails_closed(self):
        d, _ = demo()
        out = compare_paired_incidents(d, {})
        self.assertEqual(out["status"], "not_evaluable")
        self.assertIn("aws_only", out["blockers"][0])

    def test_paired_delays_and_detection_labels(self):
        d, p = demo()
        out = compare_paired_incidents(d, p)
        self.assertEqual(out["status"], "evaluated")
        self.assertEqual(out["identical_held_out_rows"], 4)
        self.assertEqual(out["verified_held_out_incidents"], 1)
        self.assertEqual(out["per_incident"][0]["per_arm"]["aws_only"]["delay_seconds"], 50)
        self.assertFalse(out["per_incident"][0]["per_arm"]["aws_only"]["correct_class_detected"])
        self.assertEqual(out["per_incident"][0]["per_arm"]["aws_ripe"]["delay_seconds"], 10)
        self.assertEqual(out["paired"]["aws_only_vs_aws_ripe"]["median_aws_only_minus_external_delay_seconds_when_both_detected"], 40)
        self.assertEqual(out["by_arm"]["aws_ripe"]["test_abstentions"], 1)
        self.assertEqual(out["by_arm"]["aws_only"]["false_alerts_per_24h_observed_healthy"], 0)

    def test_mismatched_test_cohorts_rejected(self):
        d, p = demo()
        p["aws_ripe"]["rows"].pop()
        with self.assertRaisesRegex(ValueError, "identical held-out"):
            compare_paired_incidents(d, p)

    def test_conflicting_labels_rejected(self):
        d, p = demo()
        p["aws_ripe"]["rows"][0]["label"] = "healthy"
        with self.assertRaisesRegex(ValueError, "truth/run disagreement"):
            compare_paired_incidents(d, p)

    def test_no_incidents_is_not_evaluable(self):
        d, p = demo()
        d["episodes"] = [d["episodes"][1]]
        for arm in GROUPS:
            p[arm]["rows"] = [r for r in p[arm]["rows"] if r["label"] == "healthy"]
        out = compare_paired_incidents(d, p)
        self.assertEqual(out["status"], "not_evaluable")
        self.assertEqual(out["blockers"], ["no_verified_held_out_fault_incidents"])

    def test_prediction_not_in_dataset_rejected(self):
        d, p = demo()
        d["rows"].pop()
        with self.assertRaisesRegex(ValueError, "not contained"):
            compare_paired_incidents(d, p)

    def test_unverified_predictions_rejected(self):
        d, p = demo()
        p["aws_only"]["test_only"] = False
        with self.assertRaisesRegex(ValueError, "unverified / nontest"):
            compare_paired_incidents(d, p)


if __name__ == "__main__":
    unittest.main()
