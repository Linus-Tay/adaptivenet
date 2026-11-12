"""Transparent per-feature-group rule detectors for the demo (NOT trained ML).

Each detector sees only its group's projected view from
``ml.team_starter.feature_views.project``, so the AWS-only arm cannot read RIPE
or HTTP values. Missing inputs give ``unknown``, never ``healthy``.
Thresholds are demo defaults, chosen for the lab, not tuned on real labels.
"""
from __future__ import annotations

import os

from ml.team_starter.feature_views import project

REGIONS = ("region-a", "region-b")
DETECTOR_ID = "lab_rule_detector_v1"


def _threshold(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def region_reasons(values: dict, region: str, group: str) -> tuple[list[str], bool]:
    """Return (degradation reasons, internal_known)."""
    v = lambda col: values.get(f"{region}.{col}")
    reasons: list[str] = []
    up = v("app_up")
    known = up is not None
    if up is not None and up != 1:
        reasons.append("app_down")
    if (v("http_error_ratio") or 0) >= _threshold("DEMO_ERROR_RATIO", 0.2):
        reasons.append("app_errors")
    if (v("http_p95_ms") or 0) >= _threshold("DEMO_APP_P95_MS", 800):
        reasons.append("app_slow")
    if (v("cpu_busy_pct") or 0) >= _threshold("DEMO_CPU_PCT", 85):
        reasons.append("compute_saturated")
    if group != "aws_only":
        probes = v("ripe_probes_reporting")
        if probes is not None and probes >= 3:
            # Mean over probes; 30% default so one dead probe of four (25%) cannot flag a region.
            if (v("packet_loss_pct") or 0) >= _threshold("DEMO_LOSS_PCT", 30):
                reasons.append("path_loss")
            if (v("network_rtt_p50_ms") or 0) >= _threshold("DEMO_RTT_MS", 400):
                reasons.append("path_latency")
    if group == "aws_ripe_http":
        failure = values.get(f"http.{region}.60s.failure_ratio")
        p95 = values.get(f"http.{region}.60s.http_latency_p95_ms")
        if failure is not None and failure >= _threshold("DEMO_HTTP_FAILURE", 0.2):
            reasons.append("http_failures")
        if p95 is not None and p95 >= _threshold("DEMO_HTTP_P95_MS", 800):
            reasons.append("http_slow")
    return reasons, known


def classify(reasons: list[str]) -> str:
    if not reasons:
        return "healthy"
    if "app_down" in reasons and "path_loss" in reasons:
        return "region_down"
    if any(r in reasons for r in ("app_down", "app_errors", "app_slow")):
        return "application_failure"
    if "compute_saturated" in reasons:
        return "compute_saturation"
    return "network_degradation"


def assess_group(detection_input: dict, group: str) -> dict:
    try:
        view = project(detection_input, group)
    except ValueError as exc:
        return {"group": group, "detector": DETECTOR_ID, "condition": "unknown",
                "error": str(exc), "regions": {}}
    regions = {}
    for region in REGIONS:
        reasons, known = region_reasons(view["values"], region, group)
        regions[region] = {"state": classify(reasons) if (known or reasons) else "unknown",
                           "reasons": reasons}
    degraded = [r for r in REGIONS if regions[r]["state"] not in ("healthy", "unknown")]
    if len(degraded) == 1:
        affected = degraded[0]
        condition = regions[affected]["state"]
    elif degraded:
        affected, condition = None, "multi_region_degradation"
    elif any(regions[r]["state"] == "unknown" for r in REGIONS):
        affected, condition = None, "unknown"
    else:
        affected, condition = None, "healthy"
    return {"group": group, "detector": DETECTOR_ID, "condition": condition,
            "affected_region": affected, "regions": regions,
            "inputs_ready": view["ready_for_shadow_assessment"],
            "missing_inputs": sorted(k for k, m in view["missing"].items() if m)}
