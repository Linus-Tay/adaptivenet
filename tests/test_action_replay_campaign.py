"""scripts/action_replay_campaign.py on a tiny SYNTHETIC campaign folder.

These fixtures are made up to check the replay harness itself (that it can reach
an action when signals agree, and stays quiet when healthy). They are not
campaign data; the real result is in docs/ACTION_REPLAY_CAMPAIGN.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("action_replay", ROOT / "scripts" / "action_replay_campaign.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

T0 = 1_800_000_000.0  # fault request time
MSM = {"region-a": 101, "region-b": 202}


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def write_campaign(tmp: Path, *, fault: bool) -> Path:
    (tmp / "ripe").mkdir()
    (tmp / "trials").mkdir()
    in_fault = lambda region, t: fault and region == "region-a" and T0 <= t <= T0 + 960
    plan = {"slots": [
        {"slot": 0, "region": "region-a", "control_region": "region-b", "duration_seconds": 960,
         "loss_percent": 35, "ripe_measurement_id": MSM["region-a"]},
        {"slot": 1, "region": "region-b", "control_region": "region-a", "duration_seconds": 960,
         "loss_percent": 35, "ripe_measurement_id": MSM["region-b"]}]}
    (tmp / "campaign-plan.json").write_text(json.dumps(plan))
    record = {"requested_utc": iso(T0), "region": "region-a", "configured_packet_loss_percent": 35,
              "duration_seconds": 960, "remote_rollback_armed_and_rules_added": True}
    (tmp / "trials" / "trial-0.json").write_text(json.dumps(record))
    (tmp / "campaign-log.jsonl").write_text(
        json.dumps({"slot": 0, "status": "submitted", "trial_record": "trial-0.json"}) + "\n"
        + json.dumps({"slot": 1, "status": "skipped"}) + "\n")
    for region in ("region-a", "region-b"):
        rows = [{"ts": t, "region": region, "app_up": 1.0, "http_rps": 1.0, "http_5xx_rps": 0.0,
                 "http_p95_ms": 5.0, "memory_available_bytes": 5e8, "memory_total_bytes": 1e9,
                 "cpu_busy_pct": 2.0, "node_load1": 0.01, "http_error_ratio": 0.0}
                for t in range(int(T0 - 7 * 3600), int(T0 + 1000), 30)]
        (tmp / f"amp-{region}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        results = []
        for t in range(int(T0 - 7 * 3600), int(T0 + 1000), 240):
            for probe in (11, 12, 13, 14):
                rcvd = 1 if in_fault(region, t) else 3
                results.append({"prb_id": probe, "timestamp": t + probe, "msm_id": MSM[region], "sent": 3,
                                "rcvd": rcvd, "avg": 40.0, "result": [{"rtt": 40.0}] * rcvd})
        (tmp / "ripe" / f"{MSM[region]}.json").write_text(json.dumps(results))
    lines = []
    for t in range(int(T0 - 3600), int(T0 + 1000), 15):
        for scope in ("cloudfront", "region-a", "region-b"):
            down = scope == "region-a" and in_fault("region-a", t)
            lines.append(json.dumps({"timestamp": iso(t), "scope": scope, "kind": "http_witness",
                                     "http_status": None if down else 200, "latency_ms": None if down else 60.0,
                                     "origin": None if down else ("region-a" if scope == "cloudfront" else scope),
                                     "outcome": "timeout" if down else "ok"}))
    (tmp / "witness.stdout").write_text("schedule header line\n" + "\n".join(lines) + "\n")
    return tmp


def test_agreeing_signals_reach_an_action(tmp_path):
    inputs = replay.Inputs(write_campaign(tmp_path, fault=True))
    slot = inputs.slots[0]
    for proposer in ("campaign", "lab"):
        row = replay.replay_slot(inputs, slot, proposer)
        assert row["action"] is not None, (proposer, row["blocked_at"])
        assert row["action"]["away_from"] == "region-a"
        assert row["action"]["route"] == "50->25"  # region-a share drops by one 25-point step
        assert 0 < row["action"]["delay_s"] < 600
        assert "RIPE+HTTP" in row["action"]["confidence"]


def test_healthy_campaign_never_acts(tmp_path):
    inputs = replay.Inputs(write_campaign(tmp_path, fault=False))
    for proposer in ("campaign", "lab"):
        row = replay.replay_slot(inputs, inputs.slots[0], proposer)
        assert row["action"] is None
        assert row["blocked_at"]["stage"] == "no_proposal"
    summary = replay.summarise([row], inputs.slots)
    assert summary["would_fail_over_away_from_impaired_region"] == 0
    assert summary["false_actions_in_control_windows"] == 0


def test_unconfirmed_slots_are_excluded(tmp_path):
    campaign = write_campaign(tmp_path, fault=True)
    record = json.loads((campaign / "trials" / "trial-0.json").read_text())
    record["remote_rollback_armed_and_rules_added"] = False
    (campaign / "trials" / "trial-0.json").write_text(json.dumps(record))
    out = tmp_path / "report.json"
    assert replay.main(["--campaign-dir", str(campaign), "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["excluded_unconfirmed_slots"] == [0]
    assert report["runs"]["campaign"]["summary"]["trials"] == 0
