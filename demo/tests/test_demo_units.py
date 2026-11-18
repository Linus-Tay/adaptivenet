"""Unit tests for the local demo. Synthetic observations here are TEST FIXTURES only."""
from __future__ import annotations

import argparse
import json
import time

from adaptivenet.controller.end_to_end import decide
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.sqs_transport import Outbox, encode_poll, receive_one
from adaptivenet.ingestion.store import Store

from demo import faultctl, replay
from demo.lab.collector import internal_observations, probe_observations
from demo.lab.controller import build_proposal, emulated_hpa
from demo.lab.file_queue import FileQueue, QUEUE_URL
from demo.lab.ml_service import assess


def origin_body(now, **over):
    body = {"observed_at": now, "app_up": 1, "cpu_busy_pct": 5.0, "memory_available_bytes": 6e9,
            "memory_total_bytes": 8e9, "node_load1": 0.2, "http_rps": 2.0, "http_5xx_rps": 0.0,
            "http_p95_ms": 4.0}
    body.update(over)
    return body


def fixture_events(now, *, a_rtt=5.0, a_loss=0.0):
    events = []
    for region in ("region-a", "region-b"):
        events += internal_observations(region, origin_body(now - 5), now)
        for n in range(1, 6):
            rtt, loss = (a_rtt, a_loss) if region == "region-a" else (5.0, 0.0)
            replies = [rtt] * 3 if loss == 0 else [None, rtt, rtt]
            events += probe_observations(region, f"lab-probe-{n}", replies, now - 10, now)
    return events


def test_null_metrics_are_skipped_not_zeroed():
    now = time.time()
    obs = internal_observations("region-a", origin_body(now, cpu_busy_pct=None, http_rps=None), now)
    names = {o.metric for o in obs}
    assert "cpu_busy_pct" not in names and "http_rps" not in names
    assert all(o.labels == {"lab": "local_origin"} for o in obs)


def test_probe_loss_and_rtt():
    now = time.time()
    loss, rtt = probe_observations("region-a", "p1", [None, 10.0, 30.0], now, now)
    assert loss.value == 33.333 and rtt.value == 30.0
    assert probe_observations("region-a", "p1", [None, None], now, now)[0].value == 100
    assert len(probe_observations("region-a", "p1", [None, None], now, now)) == 1


def test_outbox_file_queue_worker_roundtrip(tmp_path):
    now = time.time()
    queue = FileQueue(tmp_path / "queue")
    outbox = Outbox(tmp_path / "outbox")
    outbox.stage(encode_poll(fixture_events(now), [], "ok", None, now))
    assert outbox.flush(queue, QUEUE_URL) == 1 and not outbox.pending()
    result = receive_one(queue, QUEUE_URL, tmp_path / "db.sqlite", wait_seconds=0)
    assert result["processed"] == 1
    assert queue.depth() == {"ready": 0, "inflight": 0}
    snap = Store(tmp_path / "db.sqlite").latest_snapshot()
    assert snap["ready_for_model"] is True


def test_unacked_message_becomes_visible_again(tmp_path):
    q = FileQueue(tmp_path)
    q.send_message(QueueUrl=QUEUE_URL, MessageBody="x")
    first = q.receive_message(QueueUrl=QUEUE_URL, VisibilityTimeout=0)["Messages"][0]
    time.sleep(0.01)  # receipt deadline is stored at millisecond precision
    again = q.receive_message(QueueUrl=QUEUE_URL)["Messages"][0]
    assert first["MessageId"] == again["MessageId"]


def _assessment(tmp_path, monkeypatch, events, now):
    monkeypatch.setenv("DEMO_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADAPTIVENET_SHADOW_MODEL", raising=False)
    snap = make_snapshot(events, now)
    return snap, assess(snap, now)


def test_gray_failure_invisible_to_aws_only(tmp_path, monkeypatch):
    now = time.time()
    snap, a = _assessment(tmp_path, monkeypatch, fixture_events(now, a_rtt=650.0, a_loss=33.4), now)
    assert a["detectors"]["aws_only"]["condition"] == "healthy"
    ripe = a["detectors"]["aws_ripe"]
    assert ripe["condition"] == "network_degradation" and ripe["affected_region"] == "region-a"
    assert {"path_loss", "path_latency"} <= set(ripe["regions"]["region-a"]["reasons"])
    # The predictor proposal is reviewed and never executed.
    review = a["predictor"]["safety_review"]
    assert review["executed"] is False and review["approved_for_execution"] is False


def test_proposal_to_failover_decision_and_healthy_hold(tmp_path, monkeypatch):
    now = time.time()
    snap, a = _assessment(tmp_path, monkeypatch, fixture_events(now, a_rtt=650.0, a_loss=33.4), now)
    proposal = build_proposal(a, "aws_ripe", 100)
    assert proposal["action"] == "shift_traffic" and proposal["target_region_a_percent"] == 75
    hpa = {r: emulated_hpa(r, snap, now) for r in ("region-a", "region-b")}
    decision = decide(snap, proposal, current_region_a_percent=100, hpa_observations=hpa, now=now)
    assert decision["decision"] == "failover" and decision["executed"] is False
    aws_only = build_proposal(a, "aws_only", 100)
    assert aws_only["action"] == "hold"
    assert decide(snap, aws_only, current_region_a_percent=100, now=now)["decision"] == "no-op"


def test_compute_saturation_gives_scale_plan_not_execution(tmp_path, monkeypatch):
    now = time.time()
    events = [e for e in fixture_events(now) if not (e.region == "region-a" and e.metric == "cpu_busy_pct")]
    events.append(Observation("region-a", "aws_amp", "cpu_busy_pct", 97.0, "percent", now - 5, now,
                              "lab-origin-region-a", {"lab": "local_origin"}))
    snap, a = _assessment(tmp_path, monkeypatch, events, now)
    proposal = build_proposal(a, "aws_only", 100)
    assert proposal["action"] == "scale_out"
    hpa = {r: emulated_hpa(r, snap, now) for r in ("region-a", "region-b")}
    decision = decide(snap, proposal, current_region_a_percent=100, hpa_observations=hpa, now=now)
    assert decision["decision"] == "scale" and decision["plan"]["to_replicas"] == 3
    assert decision["executed"] is False and decision["dry_run"] is True


def test_missing_internal_gives_unknown_not_healthy(tmp_path, monkeypatch):
    now = time.time()
    events = [e for e in fixture_events(now) if e.source != "aws_amp"]
    _, a = _assessment(tmp_path, monkeypatch, events, now)
    assert a["detectors"]["aws_only"]["condition"] == "unknown"
    assert build_proposal(a, "aws_only", 100)["action"] == "investigate"


def test_replay_keeps_values_and_original_times(tmp_path):
    now = time.time()
    snaps = [make_snapshot(fixture_events(t), t) for t in (now - 7200, now - 7170)]
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "feature_snapshots.jsonl").write_text("\n".join(json.dumps(s) for s in snaps), encoding="utf-8")
    loaded = replay.load_snapshots(archive)
    slept = []
    clock = [now]
    def fake_sleep(s):
        slept.append(s); clock[0] += s
    count = replay.replay(loaded, [], tmp_path / "out", speed=10, sleep=fake_sleep, clock=lambda: clock[0])
    assert count == 2 and round(sum(slept), 3) == 3.0
    stored = Store(tmp_path / "out" / "adaptivenet-ingestion.db").latest_snapshot()
    original = snaps[1]
    assert stored["snapshot_id"] == original["snapshot_id"]
    assert stored["regions"]["region-a"]["features"] == original["regions"]["region-a"]["features"]
    assert stored["replay"]["original_computed_at_unix"] == original["computed_at_unix"]
    assert stored["demo"]["mode"] == "replay" and abs(stored["computed_at_unix"] - clock[0]) < 1


def test_faultctl_plans():
    ns = lambda **k: argparse.Namespace(**{"region": None, "duration": 60, **k})
    assert faultctl.plan(ns(command="lab-latency", region="sg", ms=400)) == [
        ("POST", "lab-gateway", "/admin/faults", {"region": "region-a", "latency_ms": 400, "duration_seconds": 60})]
    assert faultctl.plan(ns(command="lab-loss", region="sg", pct=50))[0][3]["loss_pct"] == 50
    down = faultctl.plan(ns(command="region-down", region="us"))
    assert [c[1] for c in down] == ["region-b", "lab-gateway"]
    assert faultctl.plan(ns(command="app-5xx", region="us", rate=0.5))[0][3]["kind"] == "app_5xx"
    assert faultctl.plan(ns(command="lab-errors", region="sg", rate=1.0, duration=900))[0][3]["duration_seconds"] == 120
    assert len(faultctl.plan(ns(command="lab-clear"))) == 3


def test_origin_metrics_work_without_unix_apis(monkeypatch):
    """Windows has no os.sysconf, os.getloadavg or /proc: metrics must still answer."""
    import os as _os
    from demo.lab import origin
    monkeypatch.delattr(_os, "sysconf", raising=False)
    monkeypatch.delattr(_os, "getloadavg", raising=False)
    monkeypatch.setattr(origin, "_memory", lambda: (1.0e9, 2.0e9))
    body = origin.metrics()
    assert body["app_up"] == 1 and body["node_load1"] is None
    assert body["memory_available_bytes"] == 1.0e9
