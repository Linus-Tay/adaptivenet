from ml.team_starter import campaign_ml as cm
from ml.team_starter.ripe_value import ProbeIndex


def _setup():
    t0 = 1_000_000.0
    samples = [{"region": "region-a", "probe": p, "ts": t0 + i * 240, "sent": 3, "rcvd": 3, "rtt": 80.0, "msm": 1}
               for i in range(60) for p in (1, 2, 3)]
    amp = {"region-a": [{"ts": t0 + i * 30, "app_up": 1.0, "http_rps": .1, "http_5xx_rps": 0.0, "http_p95_ms": 5.0,
                         "memory_available_bytes": 5e8, "memory_total_bytes": 1e9, "cpu_busy_pct": 1.0,
                         "node_load1": 0.0, "http_error_ratio": 0.0} for i in range(480)]}
    slots = [{"slot": 0, "region": "region-a", "control_region": "region-b", "loss_percent": 30,
              "request_unix": t0 + 3600, "duration_seconds": 960, "control_start_unix": t0 + 3840,
              "control_end_unix": t0 + 4560, "effect_confirmed": True},
             {"slot": 1, "region": "region-a", "control_region": "region-b", "loss_percent": 25,
              "request_unix": t0 + 7200, "duration_seconds": 960, "control_start_unix": t0 + 7440,
              "control_end_unix": t0 + 8160, "effect_confirmed": False}]
    return t0, ProbeIndex(samples), amp, slots


def test_labels_and_pads():
    t0, index, amp, slots = _setup()
    rows = cm.build_rows(amp, index, slots)
    by_ts = {r["ts"]: r["label"] for r in rows}
    assert by_ts[t0 + 3600] == "impaired" and by_ts[t0 + 3600 + 960] == "impaired"
    assert t0 + 3600 + 960 + 300 not in by_ts            # recovery pad dropped, never healthy
    assert t0 + 3600 - 30 not in by_ts                   # pre-pad dropped
    assert not any(t0 + 7200 - 60 <= t <= t0 + 7200 + 960 + 600 for t in by_ts)  # unconfirmed slot excluded
    assert by_ts[t0 + 3000] == "healthy"
    assert min(by_ts) >= t0 + cm.PROTOCOL["ripe_warmup_seconds"]


def test_embargo_keeps_training_away_from_test_fold():
    rows = [{"ts": float(t)} for t in range(0, 10000, 30)]
    train = cm._train_rows(rows, 4000, 6000, 900)
    assert all(r["ts"] < 3100 or r["ts"] >= 6900 for r in train)


def test_false_alarm_onsets_count_transitions_only():
    rows = [{"region": "region-a", "ts": float(i * 30), "label": "healthy", "slot": None, "values": {}} for i in range(10)]
    alerts = {("region-a", r["ts"]): i in (2, 3, 4, 7) for i, r in enumerate(rows)}
    out = cm.score(rows, alerts, [])
    assert out["false_alarm_onsets"] == 2
    assert out["healthy_rows_in_alert_pct"] == 40.0
