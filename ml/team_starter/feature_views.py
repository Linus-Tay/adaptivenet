"""Leakage-resistant, deterministic feature groups for AdaptiveNet's ML team.

No network, AWS permissions, training, or routing. Input is an existing versioned
model_input.v1 or detection_input.v1 dict; values are never imputed or fabricated.
"""
from __future__ import annotations

import math
from typing import Any

AWS_COLUMNS = (
    "app_up", "cpu_busy_pct", "cpu_busy_ewma_pct", "memory_available_bytes",
    "memory_total_bytes", "memory_available_pct", "node_load1", "http_rps",
    "http_5xx_rps", "http_error_ratio", "http_p95_ms",
    "http_p95_ewma_ms", "http_p95_robust_z",
)
RIPE_COLUMNS = ("network_rtt_p50_ms", "network_rtt_p95_ms",
                "packet_loss_pct", "ripe_probes_reporting")
GROUPS = ("aws_only", "aws_ripe", "aws_ripe_http")
REGIONS = ("region-a", "region-b")
SCOPES = ("cloudfront", "region-a", "region-b")
WINDOWS = ("60s", "300s", "900s")
HTTP_WINDOW_COLUMNS = ("failure_ratio", "http_latency_p95_ms", "count")


def _number_or_none(value: Any, name: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid numeric feature: {name}")
    return value


def project(payload: dict, group: str) -> dict:
    """Return explicit feature values AND null masks for one ablation arm.

    No comparison deltas, RIPE provenance/quality or global CloudFront values
    can leak into aws_only. HTTP is only available as a separate group.
    Does not provide labels or establish cross-source as-of alignment.
    """
    if group not in GROUPS:
        raise ValueError(f"unknown group: {group}")
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    detection = payload.get("contract_version") == "adaptivenet.detection_input.v1"
    slow = payload.get("slow") if detection else payload
    fast = payload.get("fast") if detection else None
    if not isinstance(slow, dict) or slow.get("contract_version") != "adaptivenet.model_input.v1":
        raise ValueError("expected model_input.v1 or detection_input.v1")
    if not isinstance(slow.get("snapshot_id"), str):
        raise ValueError("snapshot_id missing")
    features = slow.get("features")
    if not isinstance(features, dict):
        raise ValueError("missing regional features")
    cols = AWS_COLUMNS + (RIPE_COLUMNS if group != "aws_only" else ())
    values: dict[str, float | int | None] = {}
    for region in REGIONS:
        row = features.get(region)
        if not isinstance(row, dict):
            raise ValueError(f"missing {region}")
        for col in cols:
            key = f"{region}.{col}"
            values[key] = _number_or_none(row.get(col), key)

    ready = slow.get("ready_for_inference") is True
    if group == "aws_ripe_http":
        if not detection or not isinstance(fast, dict) or fast.get("contract_version") != "adaptivenet.fast_signals.v1":
            raise ValueError("aws_ripe_http requires detection_input.v1 with fast_signals.v1")
        scopes = fast.get("scopes")
        if not isinstance(scopes, dict):
            raise ValueError("fast signal scopes missing")
        for scope in SCOPES:
            item = scopes.get(scope)
            if not isinstance(item, dict) or not isinstance(item.get("windows"), dict):
                raise ValueError(f"invalid HTTP scope: {scope}")
            key = f"http.{scope}.consecutive_failed_requests"
            values[key] = _number_or_none(item.get("consecutive_failed_requests"), key)
            for window in WINDOWS:
                row = item["windows"].get(window)
                if not isinstance(row, dict):
                    raise ValueError(f"missing HTTP window: {scope}/{window}")
                for name in HTTP_WINDOW_COLUMNS:
                    key = f"http.{scope}.{window}.{name}"
                    values[key] = _number_or_none(row.get(name), key)
        ready = ready and fast.get("ready_for_fast_assessment") is True
    return {
        "snapshot_id": slow["snapshot_id"],
        "group": group,
        "ready_for_shadow_assessment": ready,
        "values": values,
        "missing": {k: v is None for k, v in values.items()},
        "limitations": ["not_trained", "single_witness_vantage" if group == "aws_ripe_http" else "no_http_witness"],
    }
