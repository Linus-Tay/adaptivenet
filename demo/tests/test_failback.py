"""Failback gates in the demo controller. Snapshots are TEST FIXTURES built from synthetic lab observations."""
from __future__ import annotations

import time

from adaptivenet.controller.confidence import ConfidenceRules, ConfidenceTracker
from adaptivenet.controller.recovery import HttpMeasurement
from adaptivenet.ingestion.features import make_snapshot

from demo.lab import controller as ctl
from demo.tests.test_demo_units import fixture_events


class FakeRoute:
    def __init__(self, weight):
        self.weight, self.calls = weight, []

    def read_weight(self):
        return self.weight

    def request_weight(self, *, original, target):
        assert original == self.weight
        self.calls.append((original, target))
        self.weight = target


def loop(tmp_path, weight, *, fast=True, healthy_sec=60):
    lp = ctl.DemoLoop.__new__(ctl.DemoLoop)
    lp.data_dir, lp.group, lp.mode = tmp_path, "aws_ripe_http", "local"
    lp.route = FakeRoute(weight)
    lp.safety = object()
    lp.confidence = ConfidenceTracker(ConfidenceRules(failback_healthy_sec=healthy_sec))
    lp.failback_checks, lp.failback_started = 0, False
    lp._new_controller = lambda archive=False: None

    def probe(scope):
        t = time.time()
        region = "region-a" if scope == "region-a" else ("region-a" if lp.route.weight == 100 else "region-b")
        return HttpMeasurement(str(t) + scope, t, t + (0.05 if fast else 1.0), region, 200)
    lp.probe = probe

    def window(action_id, destination):
        t = time.time()
        reqs = tuple(HttpMeasurement(f"r{i}", t + i, t + i + 0.05, "region-a", 200) for i in range(12))
        return ctl.RecoveryWindow(action_id, t, reqs, HttpMeasurement("d", t + 1, t + 1.05, "region-a", 200))
    lp.recovery_window = window
    return lp


def assessment(condition="healthy"):
    return {"detectors": {"aws_ripe_http": {"condition": condition}}}


GOOD = {"region-a": True, "region-b": True}


def test_waits_then_steps_back_after_three_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "evaluate_recovery", lambda *a, **k: ("recovered", "client_window_met_deadline"))
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50)
    lp.confidence.observe(snap, now=now - 30, http_ok=GOOD)
    step = lp.failback(snap, assessment(), 50, "no-op", now)["executed_locally"]["step"]
    assert step["state"] == "waiting_healthy" and step["reasons"] == ["primary_healthy:30/60s"]
    lp.confidence.regions["region-a"].healthy_since = now - 61
    states = [lp.failback(snap, assessment(), 50, "no-op", now)["executed_locally"] for _ in range(3)]
    assert [s.get("step", {}).get("qualifying_count") for s in states[:2]] == [1, 2]
    assert states[2]["verify"]["state"] == "failback_step_ok"
    assert lp.route.calls == [(50, 75)]


def test_no_failback_while_degraded_or_at_primary(tmp_path):
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50)
    lp.confidence.observe(snap, now=now - 300, http_ok=GOOD)
    assert lp.failback(snap, assessment("network_degradation"), 50, "failover", now) is None
    assert loop(tmp_path, 100).failback(snap, assessment(), 100, "no-op", now) is None
    # Singapore's own client check failing (one bad signal) is enough to hold.
    lp = loop(tmp_path, 50)
    lp.confidence.observe(snap, now=now - 300, http_ok={"region-a": False, "region-b": True})
    assert lp.failback(snap, assessment(), 50, "no-op", now) is None


def test_no_failback_within_two_minutes_of_a_failover_step(tmp_path):
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50, healthy_sec=0)
    lp.confidence.observe(snap, now=now - 10, http_ok=GOOD)
    lp.confidence.record_traffic_change(now - 60, towards="region-b")
    step = lp.failback(snap, assessment(), 50, "no-op", now)["executed_locally"]["step"]
    assert step["state"] == "waiting_healthy" and step["reasons"][0].startswith("cooldown_reversal:")
    assert lp.route.calls == []


def test_slow_primary_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "evaluate_recovery", lambda *a, **k: ("blocked", "destination_client_deadline_missed"))
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 25, healthy_sec=0)
    lp.confidence.observe(snap, now=now, http_ok=GOOD)
    for _ in range(3):
        out = lp.failback(snap, assessment(), 25, "no-op", now)
    assert out["executed_locally"]["verify"]["state"] == "rolled_back"
    assert lp.route.calls == [(25, 50), (50, 25)] and lp.route.weight == 25
    # After a rollback Singapore must earn trust again before the next try.
    assert lp.confidence.regions["region-a"].healthy_since is None


class FakeSafety:
    """Records what the loop tells the SafetyController."""

    def __init__(self):
        self.calls = []

    def step(self, snapshot, proposal, *, observed_at, source_degraded, destination_responsive):
        self.calls.append(source_degraded)
        from adaptivenet.controller.safety import ControllerResult
        return ControllerResult("local", "qualifying" if source_degraded else "observing",
                                () if source_degraded else ("degradation_evidence_missing_or_stale",))


def test_failover_waits_for_confidence_before_the_safety_controller_counts(tmp_path):
    """The lab probe says Singapore is slow, but AWS and RIPE say it is fine: hold."""
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50, fast=False)
    lp.safety = FakeSafety()
    plan = {"from_region": "region-a", "to_region": "region-b"}
    lp.confidence.observe(snap, now=now, http_ok={"region-a": False, "region-b": True})
    verdict = lp.confidence.failover("region-a", now)
    assert not verdict.ok and verdict.reason == "signals_disagree:1/2_bad(HTTP)"
    out = lp.execute_failover(snap, {}, plan, confident=verdict)
    assert out["step"]["state"] == "confirming_signals" and out["step"]["reasons"] == [verdict.reason]
    assert lp.safety.calls == [False] and lp.route.calls == []


def test_confident_failover_hands_the_probe_result_to_the_safety_controller(tmp_path):
    from adaptivenet.controller.confidence import Verdict
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50, fast=False)
    lp.safety = FakeSafety()
    out = lp.execute_failover(snap, {}, {"from_region": "region-a", "to_region": "region-b"},
                              confident=Verdict(True, "confident:AWS+HTTP_bad_for_3_windows"))
    assert out["step"]["state"] == "qualifying" and lp.safety.calls == [True]
