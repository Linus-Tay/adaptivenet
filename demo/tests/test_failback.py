"""Failback gates in the demo controller. Snapshots are TEST FIXTURES built from synthetic lab observations."""
from __future__ import annotations

import time

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


def loop(tmp_path, weight, *, fast=True):
    lp = ctl.DemoLoop.__new__(ctl.DemoLoop)
    lp.data_dir, lp.group, lp.mode = tmp_path, "aws_ripe_http", "local"
    lp.route = FakeRoute(weight)
    lp.safety = object()
    lp.healthy_since, lp.failback_checks, lp.failback_started = None, 0, False
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


def test_waits_then_steps_back_after_three_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "FAILBACK_HEALTHY_SEC", 60)
    monkeypatch.setattr(ctl, "evaluate_recovery", lambda *a, **k: ("recovered", "client_window_met_deadline"))
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50)
    assert lp.failback(snap, assessment(), 50, "no-op", now)["executed_locally"]["step"]["state"] == "waiting_healthy"
    lp.healthy_since = now - 61
    states = [lp.failback(snap, assessment(), 50, "no-op", now)["executed_locally"] for _ in range(3)]
    assert [s.get("step", {}).get("qualifying_count") for s in states[:2]] == [1, 2]
    assert states[2]["verify"]["state"] == "failback_step_ok"
    assert lp.route.calls == [(50, 75)]


def test_no_failback_while_degraded_or_at_primary(tmp_path):
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 50)
    assert lp.failback(snap, assessment("network_degradation"), 50, "failover", now) is None
    assert lp.healthy_since is None
    assert loop(tmp_path, 100).failback(snap, assessment(), 100, "no-op", now) is None


def test_slow_primary_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "FAILBACK_HEALTHY_SEC", 0)
    monkeypatch.setattr(ctl, "evaluate_recovery", lambda *a, **k: ("blocked", "destination_client_deadline_missed"))
    now = time.time()
    snap = make_snapshot(fixture_events(now), now)
    lp = loop(tmp_path, 25)
    for _ in range(3):
        out = lp.failback(snap, assessment(), 25, "no-op", now)
    assert out["executed_locally"]["verify"]["state"] == "rolled_back"
    assert lp.route.calls == [(25, 50), (50, 25)] and lp.route.weight == 25
