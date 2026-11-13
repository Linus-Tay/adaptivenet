"""Reviewer rule reviewer_source_loss_only_v1 (team decision, option B, 13 Nov 2026).

When every RIPE probe to a region loses every packet there is no RTT, so ingestion marks that
region's RIPE quality ``loss_only`` and the snapshot is not ``ready_for_model``. The reviewer now
accepts that snapshot for a traffic shift *away from* the loss_only region, and only then.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "services"))
from adaptivenet.ingestion.decisions import (  # noqa: E402
    LOSS_ONLY_SOURCE_RULE, review_proposal, scale_proposal_rejection_reasons,
)
from adaptivenet.ingestion.features import make_snapshot  # noqa: E402
from adaptivenet.ingestion.models import Observation  # noqa: E402

NOW = 1_800_000_000.0
GATE = "telemetry_quality_gate_failed"


def o(region, source, metric, value, source_id="one"):
    return Observation(region=region, source=source, metric=metric, value=value,
                       unit="%" if metric.endswith("pct") else "unit",
                       observed_at=NOW, collected_at=NOW, source_id=source_id)


def region_events(region, *, ripe="fresh", internal=True, probes=5):
    events = []
    if internal:
        events += [o(region, "aws_amp", "app_up", 1),
                   o(region, "aws_amp", "memory_available_bytes", 8e8),
                   o(region, "aws_amp", "memory_total_bytes", 1e9),
                   o(region, "aws_amp", "cpu_busy_pct", 35),
                   o(region, "aws_amp", "http_rps", 2),
                   o(region, "aws_amp", "http_5xx_rps", 0),
                   o(region, "aws_amp", "http_p95_ms", 120)]
    for probe_id in range(1, probes + 1):
        sid = f"123/{probe_id}"
        if ripe == "fresh":
            events += [o(region, "ripe_atlas", "packet_loss_pct", 0, sid),
                       o(region, "ripe_atlas", "network_rtt_ms", 50 + probe_id, sid)]
        else:  # every packet lost: loss reported, no RTT
            events.append(o(region, "ripe_atlas", "packet_loss_pct", 100, sid))
    return events


def snapshot(a=None, b=None):
    return make_snapshot((a or region_events("region-a")) + (b or region_events("region-b")), NOW)


def shift(snap, current, target):
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": snap["snapshot_id"],
                "action": "shift_traffic", "confidence": 0.99, "target_region_a_percent": target,
                "evidence": ["ripe:all_probes_lost"]}
    with patch("adaptivenet.ingestion.decisions.time.time", return_value=NOW + 30):
        return review_proposal(snap, proposal, current_region_a_percent=current)


def test_fixture_really_produces_loss_only():
    snap = snapshot(a=region_events("region-a", ripe="loss_only"))
    assert snap["regions"]["region-a"]["quality"]["external"] == "loss_only"
    assert snap["ready_for_model"] is False  # ingestion and ML readiness are unchanged
    assert LOSS_ONLY_SOURCE_RULE == "reviewer_source_loss_only_v1"


@pytest.mark.parametrize(("lossy", "current", "target"), [
    ("region-a", 100, 75),   # move away from Singapore
    ("region-a", 50, 25),
    ("region-b", 50, 75),    # move away from Virginia
])
def test_loss_only_accepted_for_source_region(lossy, current, target):
    a = region_events("region-a", ripe="loss_only" if lossy == "region-a" else "fresh")
    b = region_events("region-b", ripe="loss_only" if lossy == "region-b" else "fresh")
    review = shift(snapshot(a, b), current, target)
    assert GATE not in review["rejection_reasons"]
    assert review["rejection_reasons"] == []
    assert review["eligible_for_operator_review"] is True
    assert review["approved_for_execution"] is False
    assert review["executed"] is False


def test_loss_only_destination_still_rejected():
    # Region B is loss_only; moving traffic towards it must still fail the gate.
    review = shift(snapshot(b=region_events("region-b", ripe="loss_only")), 100, 75)
    assert GATE in review["rejection_reasons"]
    assert review["eligible_for_operator_review"] is False


def test_both_regions_loss_only_rejected():
    snap = snapshot(region_events("region-a", ripe="loss_only"), region_events("region-b", ripe="loss_only"))
    assert GATE in shift(snap, 100, 75)["rejection_reasons"]


def test_source_with_too_few_probes_still_rejected():
    # Fewer than the minimum probes is insufficient data, not loss_only.
    snap = snapshot(a=region_events("region-a", ripe="loss_only", probes=2))
    assert snap["regions"]["region-a"]["quality"]["external"] == "insufficient_or_stale"
    assert GATE in shift(snap, 100, 75)["rejection_reasons"]


def test_source_missing_internal_data_still_rejected():
    snap = snapshot(a=region_events("region-a", ripe="loss_only", internal=False))
    assert snap["regions"]["region-a"]["quality"]["internal"] == "missing"
    assert GATE in shift(snap, 100, 75)["rejection_reasons"]


def test_no_change_proposal_gets_no_exemption():
    snap = snapshot(a=region_events("region-a", ripe="loss_only"))
    review = shift(snap, 100, 100)
    assert GATE in review["rejection_reasons"]
    assert "route_weight_unchanged" in review["rejection_reasons"]


def test_scale_out_gets_no_exemption():
    snap = snapshot(a=region_events("region-a", ripe="loss_only"))
    proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": snap["snapshot_id"],
                "action": "scale_out", "confidence": 0.99, "evidence": ["cpu"]}
    with patch("adaptivenet.ingestion.decisions.time.time", return_value=NOW + 30):
        assert GATE in scale_proposal_rejection_reasons(snap, proposal)


def test_other_safety_checks_still_apply_with_loss_only_source():
    review = shift(snapshot(a=region_events("region-a", ripe="loss_only")), 100, 50)
    assert review["rejection_reasons"] == ["route_delta_above_25_percent_safety_limit"]
