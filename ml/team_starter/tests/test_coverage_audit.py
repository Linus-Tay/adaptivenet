"""Coverage / false-alarm estimability audit. Fixtures are NOT real evidence."""
import unittest

from ml.team_starter.coverage_audit import audit, false_alarm_rate, poisson_interval
from ml.team_starter.tests.test_offline_foundation import manifest, reviewed

T0 = 1_790_000_000
HOUR = 3600
GAP = 2 * HOUR  # larger than the default 900 s embargo


def _blocks(labels_per_block):
    items, t = [], T0
    for i, labels in enumerate(labels_per_block):
        for j, label in enumerate(labels):
            items.append(reviewed(f"ep-{i}-{j}", label, t, t + HOUR, run_id=f"run-{i}"))
            t += HOUR
        t += GAP
    return manifest(items)


def _preds(rows):
    return {"contract_version": "adaptivenet.native_test_predictions.v1",
            "rows": [dict(r) for r in rows]}


class PoissonTests(unittest.TestCase):
    def test_zero_count_upper_bound_matches_exact_value(self):
        lo, hi = poisson_interval(0)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 3.689, places=2)  # -ln(0.025)

    def test_known_interval_for_five(self):
        lo, hi = poisson_interval(5)
        self.assertAlmostEqual(lo, 1.623, places=2)
        self.assertAlmostEqual(hi, 11.668, places=2)

    def test_zero_exposure_is_not_estimable(self):
        self.assertEqual(false_alarm_rate(0, 0)["status"], "not_estimable")
        self.assertIsNone(false_alarm_rate(3, 0)["per_day"])

    def test_short_exposure_is_flagged(self):
        result = false_alarm_rate(1, HOUR)
        self.assertEqual(result["status"], "indicative_only")
        self.assertAlmostEqual(result["per_day"], 24.0)
        self.assertEqual(false_alarm_rate(0, 24 * HOUR)["status"], "estimated")


class AuditTests(unittest.TestCase):
    def test_binary_network_study_reports_missing_classes_and_no_fa_rate(self):
        # Mirrors the shape of the current native study: network + healthy in
        # training, but the held-out test block has no healthy control.
        blocks = [["healthy", "network_degradation"]] * 3 + [["network_degradation"]] * 2
        result = audit(_blocks(blocks))
        self.assertFalse(result["six_class_ready"])
        self.assertEqual(result["false_alarms_per_day"]["status"], "not_estimable")
        for missing in ("partial_outage", "transient", "recovery", "application_failure"):
            self.assertFalse(result["classes"][missing]["trainable_and_scorable"])
            self.assertIn("no_verified_episode_of_this_class", result["classes"][missing]["missing"])
        self.assertTrue(any("vantage" in m for m in result["classes"]["partial_outage"]["missing"]))
        self.assertTrue(any(b.startswith("false_alarms_per_day_not_estimable") for b in result["blockers"]))

    def test_healthy_test_block_is_estimable_and_uses_verified_duration(self):
        blocks = [["healthy", "network_degradation"]] * 5
        result = audit(_blocks(blocks))
        fa = result["false_alarms_per_day"]
        self.assertEqual(fa["status"], "estimable_needs_predictions")
        self.assertAlmostEqual(fa["exposure_hours"], 1.0)

    def test_alarm_transitions_counted_from_predictions(self):
        blocks = [["healthy", "network_degradation"]] * 5
        labels = _blocks(blocks)
        test_ids = audit(labels)["split"]["test"]
        healthy = [i for i in test_ids if i.endswith("-0")][0]
        rows = [{"episode_id": healthy, "snapshot_id": f"features-{k}", "at_unix": T0 + k,
                 "label": "healthy"} for k in range(6)]
        dataset = {"rows": rows}
        preds = _preds(rows)
        for row, label in zip(preds["rows"], ["healthy", "network_degradation", "network_degradation",
                                              "healthy", None, "application_failure"]):
            row["predicted_label"] = label
        fa = audit(labels, dataset, preds)["false_alarms_per_day"]
        # Two transitions into alert; an abstention is not an alarm.
        self.assertEqual(fa["false_alarms"], 2)
        self.assertAlmostEqual(fa["per_day"], 48.0)
        self.assertEqual(fa["status"], "indicative_only")

    def test_missing_predictions_are_reported_not_assumed_healthy(self):
        blocks = [["healthy", "network_degradation"]] * 5
        labels = _blocks(blocks)
        healthy = [i for i in audit(labels)["split"]["test"] if i.endswith("-0")][0]
        rows = [{"episode_id": healthy, "snapshot_id": "features-1", "at_unix": T0, "label": "healthy"}]
        fa = audit(labels, {"rows": rows}, _preds([]))["false_alarms_per_day"]
        self.assertEqual(fa["status"], "incomplete_predictions")
        self.assertEqual(fa["rows_without_prediction"], 1)

    def test_too_few_blocks_reports_blocker_instead_of_crashing(self):
        result = audit(_blocks([["healthy"], ["network_degradation"]]))
        self.assertIsNone(result["split"])
        self.assertTrue(any(b.startswith("chronological_split_impossible") for b in result["blockers"]))

    def test_unreviewed_manifest_is_rejected(self):
        bad = manifest([reviewed("x", "healthy", T0, T0 + HOUR, approved=False)])
        with self.assertRaises(ValueError):
            audit(bad)


if __name__ == "__main__":
    unittest.main()
