"""Proposal -> safety reviewer -> EKS decision. No AWS or Kubernetes access."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import time

import pytest

from adaptivenet.controller.end_to_end import (
    ALLOWLIST, affected_region, decide, map_model_output, observation_from_dict,
)
from adaptivenet.controller.scaling import HpaObservation

NOW = time.time()


def snapshot(**overrides):
    def region(**features):
        base = {"app_up": 1, "cpu_busy_pct": 20.0, "memory_available_pct": 60.0,
                "http_error_ratio": 0.0, "http_p95_ms": 80.0}
        return {"features": {**base, **features}, "quality": {"internal": "fresh"}}
    snap = {"snapshot_id": "features-test", "computed_at_unix": NOW - 5, "ready_for_model": True,
            "regions": {"region-a": region(), "region-b": region()}}
    for key, features in overrides.items():
        snap["regions"][key.replace("_", "-")]["features"].update(features)
    return snap


def model(condition, confidence=.95, action="investigate"):
    return {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "features-test",
            "action": action, "confidence": confidence, "model_id": "m1",
            "evidence": [f"shadow_condition:{condition}", "not_approved_for_execution"]}


def hpa(region="region-a", **changes):
    base = HpaObservation(
        target=ALLOWLIST[region], observed_at=NOW - 2, metrics_observed_at=NOW - 10,
        min_replicas=2, max_replicas=6, current=2, desired=2, ready=2, available=2, pending=0,
        able_to_scale=True, scaling_active=True, scaling_limited=False,
        cpu_target_pct=60, cpu_current_pct=90,
    )
    return replace(base, **changes)


def test_healthy_model_output_is_no_op():
    result = decide(snapshot(), model("healthy"), now=NOW)
    assert result["decision"] == "no-op" and result["executed"] is False and result["dry_run"] is True
    assert result["plan"] is None


def test_network_degradation_in_region_a_becomes_bounded_failover():
    snap = snapshot(region_a={"http_p95_ms": 1500.0})
    result = decide(snap, model("network_degradation"), current_region_a_percent=50, now=NOW)
    assert result["decision"] == "failover"
    assert result["proposal"]["target_region_a_percent"] == 25
    assert result["plan"]["from_region"] == "region-a" and result["plan"]["to_region"] == "region-b"
    assert result["plan"]["live_executor"] is None
    assert result["review"]["approved_for_execution"] is False


def test_failover_respects_reviewer_destination_checks():
    snap = snapshot(region_a={"app_up": 0}, region_b={"cpu_busy_pct": 95.0})
    # region-b CPU high is still "healthy" for routing classification, but the reviewer blocks it.
    result = decide(snap, model("partial_outage"), now=NOW)
    assert result["decision"] == "no-op"
    assert "destination_high_or_unknown_cpu" in result["reasons"]


def test_failover_blocked_when_destination_hpa_not_ready():
    snap = snapshot(region_b={"http_error_ratio": .5})
    result = decide(snap, model("network_degradation"), now=NOW,
                    hpa_observations={"region-a": hpa("region-a", ready=1, available=1)})
    assert result["decision"] == "no-op" and result["reasons"] == ["destination_capacity_not_confirmed"]


def test_low_confidence_is_rejected_by_reviewer():
    snap = snapshot(region_a={"app_up": 0})
    result = decide(snap, model("network_degradation", confidence=.6), now=NOW)
    assert result["decision"] == "no-op" and "insufficient_confidence" in result["reasons"]


def test_ambiguous_region_is_no_op():
    snap = snapshot(region_a={"app_up": 0}, region_b={"app_up": 0})
    assert affected_region(snap) == (None, "both_regions_degraded")
    result = decide(snap, model("network_degradation"), now=NOW)
    assert result["decision"] == "no-op" and result["mapping"]["mapping"] == "affected_region_unclear"


def test_compute_bound_application_failure_becomes_scale():
    snap = snapshot(region_a={"cpu_busy_pct": 92.0, "http_error_ratio": .3})
    result = decide(snap, model("application_failure"), now=NOW, hpa_observations={"region-a": hpa()})
    assert result["decision"] == "scale"
    assert result["plan"]["site"] == "sg" and result["plan"]["to_replicas"] == 3
    assert result["executed"] is False


@pytest.mark.parametrize("changes, reason", [
    ({"scaling_limited": True}, "hpa_scaling_limited_or_unknown"),
    ({"cpu_current_pct": 2}, "compute_evidence_missing_or_below_hpa_target"),
    ({"current": 6, "desired": 6, "ready": 6, "available": 6}, "hpa_at_max_replicas"),
])
def test_scale_falls_back_to_no_op_on_capacity_checks(changes, reason):
    snap = snapshot(region_a={"cpu_busy_pct": 92.0, "app_up": 0})
    result = decide(snap, model("application_failure"), now=NOW,
                    hpa_observations={"region-a": hpa(**changes)})
    assert result["decision"] == "no-op" and result["reasons"] == [reason]


def test_scale_without_hpa_observation_is_no_op():
    snap = snapshot(region_a={"cpu_busy_pct": 92.0, "app_up": 0})
    result = decide(snap, model("application_failure"), now=NOW)
    assert result["reasons"] == ["missing_hpa_observation"]


def test_direct_proposal_passes_through_and_stale_snapshot_blocks():
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": "features-test",
                "action": "scale_out", "affected_region": "region-a", "confidence": .95,
                "evidence": ["fixture"]}
    snap = snapshot()
    assert map_model_output(snap, proposal, current_region_a_percent=50)[0] == proposal
    assert decide(snap, proposal, now=NOW, hpa_observations={"region-a": hpa()})["decision"] == "scale"
    stale = deepcopy(snap)
    stale["computed_at_unix"] = NOW - 600
    result = decide(stale, proposal, now=NOW, hpa_observations={"region-a": hpa()})
    assert result["decision"] == "no-op" and "feature_snapshot_expired_or_invalid" in result["reasons"]


def test_observation_round_trips_from_json():
    observation = hpa()
    assert observation_from_dict(json.loads(json.dumps(asdict(observation)))) == observation


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_cli_decide_is_dry_run(tmp_path, capsys):
    from scripts.end_to_end import main
    snap = snapshot(region_a={"cpu_busy_pct": 92.0, "app_up": 0})
    args = ["decide", "--snapshot", _write(tmp_path, "s.json", snap),
            "--proposal", _write(tmp_path, "p.json", model("application_failure")),
            "--hpa", f"region-a={_write(tmp_path, 'h.json', asdict(hpa()))}",
            "--out", str(tmp_path / "decision.json")]
    assert main(args) == 0
    decision = json.loads((tmp_path / "decision.json").read_text(encoding="utf-8"))
    assert decision["decision"] == "scale" and decision["executed"] is False


def test_cli_execute_refuses_without_matching_approval(tmp_path, monkeypatch):
    from scripts import end_to_end
    def forbidden(*args, **kwargs):
        raise AssertionError("must not connect without approval")
    monkeypatch.setattr("adaptivenet.controller.kubectl_connection.KubectlConnection", forbidden)
    snap = snapshot(region_a={"cpu_busy_pct": 92.0, "app_up": 0})
    args = ["execute", "--snapshot", _write(tmp_path, "s.json", snap),
            "--proposal", _write(tmp_path, "p.json", model("application_failure")),
            "--hpa", f"region-a={_write(tmp_path, 'h.json', asdict(hpa()))}"]
    assert end_to_end.main(args) == 3
    assert end_to_end.main(args + ["--approve-live-write", "us"]) == 3


def test_cli_execute_refuses_failover(tmp_path):
    from scripts.end_to_end import main
    snap = snapshot(region_a={"app_up": 0})
    args = ["execute", "--snapshot", _write(tmp_path, "s.json", snap),
            "--proposal", _write(tmp_path, "p.json", model("network_degradation")),
            "--approve-live-write", "sg"]
    assert main(args) == 1


def ripe(snap, region, **features):
    snap["regions"][region]["quality"]["external"] = "fresh"
    base = {"ripe_probes_reporting": 4, "ripe_probes_full_loss": 0,
            "packet_loss_pct": 0.0, "packet_loss_median_pct": 0.0, "network_rtt_p50_ms": 60.0}
    snap["regions"][region]["features"].update({**base, **features})
    return snap


def test_gray_network_failure_with_healthy_internals_becomes_failover():
    snap = ripe(ripe(snapshot(), "region-b"), "region-a",
                packet_loss_pct=45.0, packet_loss_median_pct=40.0)
    assert affected_region(snap) == ("region-a", "network_only_degradation_other_healthy")
    result = decide(snap, model("network_degradation"), now=NOW)
    assert result["decision"] == "failover" and result["proposal"]["target_region_a_percent"] == 25
    assert result["mapping"]["signals"]["region-a"]["network"] == "degraded"


def test_single_unreachable_probe_does_not_trigger_failover():
    # One of four Singapore probes is always dead: mean 25%, median 0%.
    snap = ripe(ripe(snapshot(), "region-b"), "region-a",
                ripe_probes_full_loss=1, packet_loss_pct=25.0, packet_loss_median_pct=0.0)
    assert affected_region(snap) == (None, "no_region_degraded")
    result = decide(snap, model("network_degradation"), now=NOW)
    assert result["decision"] == "no-op"
    from adaptivenet.controller.end_to_end import network_health
    assert network_health(snap, "region-a") == ("healthy", ["ignored_unreachable_ripe_probes:1"])


def test_all_probes_unreachable_counts_as_degraded():
    snap = ripe(ripe(snapshot(), "region-b"), "region-a", ripe_probes_full_loss=4,
                packet_loss_pct=100.0, packet_loss_median_pct=100.0, network_rtt_p50_ms=None)
    assert affected_region(snap)[0] == "region-a"


def test_stale_ripe_or_mean_only_loss_is_not_trusted():
    snap = ripe(ripe(snapshot(), "region-b"), "region-a", packet_loss_pct=60.0,
                packet_loss_median_pct=None, network_rtt_p50_ms=None)
    assert affected_region(snap) == (None, "no_region_degraded")
    snap = ripe(ripe(snapshot(), "region-b"), "region-a", packet_loss_median_pct=60.0)
    snap["regions"]["region-a"]["quality"]["external"] = "insufficient_or_stale"
    assert affected_region(snap) == (None, "no_region_degraded")


def test_high_rtt_counts_as_network_degradation():
    snap = ripe(ripe(snapshot(), "region-a"), "region-b", network_rtt_p50_ms=900.0)
    assert affected_region(snap) == ("region-b", "network_only_degradation_other_healthy")


@pytest.mark.parametrize("affected, current, target, executor_region", [
    ("region-a", 100, 75, "region-b"),  # Singapore primary, SSM stops its service
    ("region-b", 50, 75, "region-a"),   # Virginia interruption (9 Nov test)
])
def test_ssm_service_stop_shape_maps_to_failover_not_scale(affected, current, target, executor_region):
    # Recorded SSM experiments: app target scrapes fail (app_up=0) while node exporter,
    # CPU and memory stay normal; the other region stays healthy.
    snap = snapshot(**{affected.replace("-", "_"): {"app_up": 0, "cpu_busy_pct": 3.0}})
    result = decide(snap, model("application_failure"), current_region_a_percent=current, now=NOW,
                    hpa_observations={executor_region: hpa(executor_region)})
    assert result["decision"] == "failover"
    assert result["proposal"]["action"] == "shift_traffic"
    assert result["plan"]["to_region"] == executor_region
    assert result["plan"]["region_a_percent"] == {"from": current, "to": target}
    assert result["plan"]["live_executor"] is None
