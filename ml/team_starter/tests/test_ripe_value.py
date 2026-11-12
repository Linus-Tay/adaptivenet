"""SYNTHETIC TEST FIXTURES ONLY: no sample here is real RIPE, AWS or incident evidence."""
from __future__ import annotations

import unittest

from ml.team_starter.ripe_value import (ProbeIndex, aws_only_alert, bootstrap_ci, candidate_events,
                                        join_events_with_aws, mcnemar_exact, normalize, paired_summary,
                                        path_features, ripe_path_alert)
from ml.team_starter.feature_views import AWS_COLUMNS

T0 = 1_800_000_000
STEP = 240


def raw(probe, ts, rtt, rcvd=3):
    return {"prb_id": probe, "timestamp": ts, "sent": 3, "rcvd": rcvd,
            "avg": rtt if rcvd else -1, "msm_id": 1}


def series(event_probe_rtt=None, start=200, stop=240, dead_probe=True):
    """Three healthy probes (+ one dead one). Optionally inflate probe 2 in [start, stop)."""
    a, b = [], []
    for i in range(300):
        ts = T0 + i * STEP
        a.append(raw(1, ts, 150.0))
        rtt2 = event_probe_rtt if event_probe_rtt and start <= i < stop else 96.0
        a.append(raw(2, ts, rtt2))
        a.append(raw(3, ts, 170.0))
        if dead_probe:
            a.append(raw(4, ts, None, rcvd=0))
        b.append(raw(2, ts, 229.0))  # same probe -> other region stays normal
        b.append(raw(4, ts, 233.0))
    return normalize(a, "region-a") + normalize(b, "region-b")


def slow(ts, p95=40.0, app_up=1, err=0.0):
    feats = {}
    for r in ("region-a", "region-b"):
        feats[r] = {c: 1.0 for c in AWS_COLUMNS}
        feats[r].update({"app_up": app_up if r == "region-a" else 1, "http_p95_ms": p95,
                         "http_error_ratio": err, "http_p95_robust_z": 0.0})
    return {"contract_version": "adaptivenet.model_input.v1", "snapshot_id": f"fixture-{ts}",
            "computed_at_unix": ts, "ready_for_inference": True, "features": feats}


class RipeValueTests(unittest.TestCase):
    def test_missing_rtt_is_none_not_zero(self):
        s = normalize([raw(9, T0, None, rcvd=0)], "region-a")[0]
        self.assertIsNone(s["rtt"])

    def test_dead_probe_is_excluded_from_loss(self):
        idx = ProbeIndex(series())
        f = path_features(idx, "region-a", T0 + 100 * STEP)
        self.assertEqual(f["ripe_probes_unreachable"], 1)
        self.assertEqual(f["ripe_reachable_probes"], 3)
        self.assertEqual(f["ripe_loss_pct_reachable"], 0.0)
        self.assertFalse(ripe_path_alert(f))

    def test_single_path_inflation_detected_and_path_specific(self):
        samples = series(event_probe_rtt=205.0)
        idx = ProbeIndex(samples)
        f = path_features(idx, "region-a", T0 + 210 * STEP)
        self.assertEqual(f["ripe_paths_inflated"], 1)
        self.assertTrue(ripe_path_alert(f))
        ev = [e for e in candidate_events(samples) if e["region"] == "region-a"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["probe"], 2)
        self.assertTrue(ev[0]["path_specific"])
        self.assertEqual(ev[0]["status"], "candidate_unreviewed")

    def test_features_are_past_only(self):
        idx = ProbeIndex(series(event_probe_rtt=205.0))
        before = path_features(idx, "region-a", T0 + 200 * STEP - 1)
        self.assertEqual(before["ripe_paths_inflated"], 0)

    def test_stale_ripe_is_missing(self):
        idx = ProbeIndex(series())
        f = path_features(idx, "region-a", T0 + 400 * STEP)
        self.assertIsNone(f["ripe_reachable_probes"])
        self.assertIsNone(ripe_path_alert(f))

    def test_aws_only_alert_rules(self):
        self.assertTrue(aws_only_alert(slow(T0, app_up=0), "region-a", []))
        self.assertTrue(aws_only_alert(slow(T0, err=.2), "region-a", []))
        self.assertTrue(aws_only_alert(slow(T0, p95=90), "region-a", [40.0] * 20))
        self.assertFalse(aws_only_alert(slow(T0), "region-a", [40.0] * 20))
        unready = slow(T0)
        unready["ready_for_inference"] = False
        self.assertIsNone(aws_only_alert(unready, "region-a", []))

    def test_join_external_only_detection(self):
        samples = series(event_probe_rtt=205.0)
        idx = ProbeIndex(samples)
        events = [e for e in candidate_events(samples) if e["region"] == "region-a"]
        snaps = [slow(T0 + i * 30) for i in range(0, 300 * STEP // 30)]
        joined = join_events_with_aws(events, snaps, idx)
        self.assertTrue(joined[0]["evaluable"])
        self.assertFalse(joined[0]["aws_only_detected"])
        self.assertTrue(joined[0]["aws_ripe_detected"])

    def test_mcnemar_and_bootstrap(self):
        self.assertEqual(mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(mcnemar_exact(0, 6), 2 / 64)
        self.assertGreater(mcnemar_exact(3, 3), .99)
        lo, hi = bootstrap_ci([1.0] * 5)
        self.assertEqual((lo, hi), (1.0, 1.0))
        self.assertIsNone(bootstrap_ci([]))

    def test_paired_summary_counts(self):
        eps = [{"evaluable": True, "aws_only_detected": False, "aws_ripe_detected": True,
                "aws_only_delay_s": None, "aws_ripe_delay_s": 60.0}] * 3 + \
              [{"evaluable": True, "aws_only_detected": True, "aws_ripe_detected": True,
                "aws_only_delay_s": 120.0, "aws_ripe_delay_s": 60.0},
               {"evaluable": False}]
        s = paired_summary(eps)
        self.assertEqual(s["evaluable_episodes"], 4)
        self.assertEqual(s["aws_ripe_only"], 3)
        self.assertEqual(s["aws_only_only"], 0)
        self.assertEqual(s["median_delay_gain_s_jointly_detected"], 60.0)


if __name__ == "__main__":
    unittest.main()


class NativePairedFixtureTest(unittest.TestCase):
    """Pipeline wiring only. A fixture result is NEVER detector performance."""

    def test_native_paired_runs_on_identical_split(self):
        try:
            import sklearn  # noqa: F401
        except ImportError:
            self.skipTest("optional sklearn not installed")
        from ml.team_starter.tests.test_network_binary import fixture
        from ml.team_starter.ripe_value import native_paired
        dataset, manifest, reviews = fixture()
        start, stop = 1_800_000_000 - 8 * 3600, 1_800_000_000 + 14 * 6000
        rows = [raw(p, t, 100.0 + p) for t in range(start, stop, STEP) for p in (1, 2, 3)]
        idx = ProbeIndex(normalize(rows, "region-a") + normalize(rows, "region-b"))
        out = native_paired(dataset, manifest, reviews, idx)
        self.assertEqual(set(out["arms"]), {"aws_only", "aws_ripe_path"})
        self.assertIn("mcnemar_exact_p", out["paired"])
        self.assertTrue(out["split"]["test"])


class EpisodeStudyFixtureTest(unittest.TestCase):
    """Wiring only, synthetic data."""

    def test_loss_quorum_detects_external_only_episode(self):
        from ml.team_starter.ripe_value import episode_study, trial_request_delays
        rows_raw = []
        for i in range(300):
            ts = T0 + i * STEP
            lossy = 200 <= i < 210
            for p in (1, 2, 3):
                rows_raw.append(raw(p, ts, 80.0, rcvd=2 if lossy and p != 3 else 3))
        idx = ProbeIndex(normalize(rows_raw, "region-b"))
        values = {f"region-b.{c}": 1.0 for c in AWS_COLUMNS}
        values.update({"region-b.app_up": 1.0, "region-b.http_error_ratio": 0.0,
                       "region-b.http_p95_robust_z": 0.0, "region-b.http_p95_ms": 5.0})
        view = {"values": values, "missing": {k: False for k in values}, "ready_for_shadow_assessment": True}
        eps = [{"episode_id": "fx-healthy", "label": "healthy", "region": "region-b",
                "start_unix": T0 + 100 * STEP, "end_unix": T0 + 110 * STEP},
               {"episode_id": "fx-net", "label": "network_degradation", "region": "region-b",
                "start_unix": T0 + 200 * STEP, "end_unix": T0 + 210 * STEP}]
        rows = [{"episode_id": e["episode_id"], "at_unix": e["start_unix"] + k * 60, "views": {"aws_only": view}}
                for e in eps for k in range(5)]
        out = episode_study({"rows": rows, "episodes": eps}, idx)
        self.assertEqual(out["positives"]["aws_ripe_only"], 1)
        self.assertEqual(out["positives"]["aws_only_only"], 0)
        self.assertEqual(out["healthy_false_alarm_episodes"]["aws_ripe"], 0)
        delays = trial_request_delays([{"run_id": "fx", "region": "region-b", "duration_seconds": 2400,
            "requested_utc": "2027-01-15T08:00:00+00:00"}], idx)
        self.assertEqual(len(delays), 1)


class CampaignFixtureTest(unittest.TestCase):
    """Wiring only, synthetic data."""

    def test_campaign_eval_positive_and_control(self):
        from ml.team_starter.ripe_value import campaign_eval
        raw_a, raw_b = [], []
        for i in range(300):
            ts = T0 + i * STEP
            lossy = 200 <= i < 204
            for p in (1, 2, 3):
                raw_b.append(raw(p, ts, 80.0, rcvd=2 if lossy and p != 3 else 3))
                raw_a.append(raw(p, ts, 150.0))
        idx = ProbeIndex(normalize(raw_a, "region-a") + normalize(raw_b, "region-b"))
        amp = {r: [{"ts": T0 + k * 30, "region": r, "app_up": 1.0, "http_5xx_rps": 0.0, "http_rps": .07,
                    "http_error_ratio": 0.0, "http_p95_ms": 4.75} for k in range(300 * STEP // 30)]
               for r in ("region-a", "region-b")}
        req = T0 + 200 * STEP - 10
        slots = [{"slot": 0, "region": "region-b", "control_region": "region-a", "loss_percent": 25,
                  "request_unix": req, "duration_seconds": 960, "control_start_unix": req + 240,
                  "control_end_unix": req + 960, "effect_confirmed": True},
                 {"slot": 1, "region": "region-a", "control_region": "region-b", "loss_percent": 10,
                  "request_unix": req + 3000, "duration_seconds": 960, "control_start_unix": req + 3240,
                  "control_end_unix": req + 3960, "effect_confirmed": False}]
        out = campaign_eval(slots, amp, idx)
        self.assertEqual(out["paired_positives"]["aws_ripe_only"], 1)
        self.assertEqual(out["controls_false_alarms"]["aws_ripe"], 0)
        self.assertEqual(out["excluded_unconfirmed_slots"], [1])

    def test_campaign_plan_is_seeded_and_non_overlapping(self):
        import importlib.util, pathlib
        from datetime import datetime, timezone
        spec = importlib.util.spec_from_file_location(
            "camp", pathlib.Path(__file__).resolve().parents[3] / "scripts" / "ripe_trial_campaign.py")
        camp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(camp)
        start = datetime(2027, 1, 1, tzinfo=timezone.utc)
        p1 = camp.plan(7, start, 10, {"region-a": 1, "region-b": 2})
        self.assertEqual(p1["slots"], camp.plan(7, start, 10, {"region-a": 1, "region-b": 2})["slots"])
        self.assertEqual(len(p1["slots"]), 20)
        self.assertEqual(sum(s["region"] == "region-a" for s in p1["slots"]), 10)
        ts = [datetime.fromisoformat(s["planned_start_utc"]).timestamp() for s in p1["slots"]]
        self.assertTrue(all(b - a >= 960 + 300 for a, b in zip(ts, ts[1:])))
