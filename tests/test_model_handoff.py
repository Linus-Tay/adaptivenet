"""No AWS access: model boundaries must be safe and deterministic."""
from __future__ import annotations
import pytest
from adaptivenet.ingestion.handoff import (
    build_model_input, conservative_rule_baseline, FEATURE_COLUMNS,
)
from adaptivenet.ingestion.decisions import review_proposal
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.models import Observation


def snapshot(now=10000.0):
    events = []
    for region in ("region-a", "region-b"):
        for metric, value, unit in (
            ("app_up", 1, "bool"),
            ("memory_available_bytes", 55_000_000, "bytes"),
            ("memory_total_bytes", 100_000_000, "bytes"),
            ("cpu_busy_pct", 0.9, "%"),
            ("http_rps", 0.0, "requests/s"),
        ):
            events.append(Observation(region, "aws_amp", metric, float(value), unit,
                                      now - 10, now - 5))
    return make_snapshot(events, now)


def test_internal_only_ready_without_ripe():
    snap = snapshot()
    data = build_model_input(snap, mode="internal_only", now=10020)
    assert data["contract_version"] == "adaptivenet.model_input.v1"
    assert data["ready_for_inference"] is True
    assert data["actions_enabled"] is False
    assert data["feature_columns"] == list(FEATURE_COLUMNS)
    assert data["features"]["region-b"]["http_rps"] == 0.0
    assert data["missing"]["region-b"]["http_rps"] is False
    assert data["features"]["region-b"]["http_p95_ms"] is None
    assert data["missing"]["region-b"]["http_p95_ms"] is True


def test_combined_not_ready_if_ripe_stopped():
    data = build_model_input(snapshot(), mode="combined", now=10020)
    assert data["ready_for_inference"] is False
    assert "combined_source_quality_not_ready" in data["quality_reasons"]


def test_expired_saved_snapshot_is_not_live_ready():
    data = build_model_input(snapshot(), mode="internal_only", now=10400)
    assert data["ready_for_inference"] is False
    assert "feature_snapshot_expired_or_future_dated" in data["quality_reasons"]


def test_future_snapshot_rejected_for_live_inference():
    data = build_model_input(snapshot(), now=9999)
    assert data["ready_for_inference"] is False


def test_baseline_makes_only_shadow_hold_or_investigate():
    snap = snapshot()
    inp = build_model_input(snap, now=10020)
    proposal = conservative_rule_baseline(inp)
    assert proposal["action"] == "hold"
    reviewed = review_proposal(snap, proposal)
    assert reviewed["approved_for_execution"] is False
    assert reviewed["executed"] is False
    assert reviewed["effective_action"] == "hold"
    inp = build_model_input(snap, now=10300)
    proposal = conservative_rule_baseline(inp)
    assert proposal["action"] == "investigate"


def test_invalid_non_finite_feature_rejected():
    snap = snapshot()
    snap["regions"]["region-b"]["features"]["http_rps"] = float("nan")
    with pytest.raises(ValueError, match="invalid numeric"):
        build_model_input(snap, now=10020)


def test_unrecognized_snapshot_version_rejected():
    snap = snapshot()
    snap["contract_version"] = "adaptivenet.features.v99"
    with pytest.raises(ValueError, match="unsupported"):
        build_model_input(snap, now=10020)


def test_repeatable_snapshot_adapter():
    snap = snapshot()
    a = build_model_input(snap, now=10020)
    b = build_model_input(snap, now=10020)
    assert a == b
