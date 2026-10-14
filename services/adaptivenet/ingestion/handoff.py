"""A small, deterministic bridge between telemetry features and ML teammates.

No network, AWS calls, model training or mitigation side effects.
"""
from __future__ import annotations

import math
import time
from typing import Literal

from .models import REGIONS
from .features import VERSION as FEATURE_VERSION

MODEL_VERSION = "adaptivenet.model_input.v1"
PROPOSAL_VERSION = "adaptivenet.proposal.v1"
MAX_LIVE_AGE_SECONDS = 180
Mode = Literal["internal_only", "combined"]

# A stable column order makes ML training/export deterministic. Keep in sync with
# features.py and packages/contracts/src/model_handoff.ts when versioning.
FEATURE_COLUMNS = (
    "app_up", "cpu_busy_pct", "cpu_busy_ewma_pct",
    "memory_available_bytes", "memory_total_bytes", "memory_available_pct",
    "node_load1", "http_rps", "http_5xx_rps", "http_error_ratio",
    "http_p95_ms", "http_p95_ewma_ms", "http_p95_robust_z",
    "network_rtt_p50_ms", "network_rtt_p95_ms", "packet_loss_pct",
    "ripe_probes_reporting",
)


def build_model_input(snapshot: dict, *, mode: Mode = "internal_only",
                      now: float | None = None) -> dict:
    """Create a versioned model input from a *real* stored feature snapshot.

    Missing observations remain None and have an explicit mask. Past readiness
    flags are NOT treated as current: live inference checks wall-clock age.
    """
    if mode not in ("internal_only", "combined"):
        raise ValueError("mode must be internal_only or combined")
    if not isinstance(snapshot, dict) or snapshot.get("contract_version") != FEATURE_VERSION:
        raise ValueError("unsupported feature snapshot version")
    if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"].startswith("features-"):
        raise ValueError("invalid snapshot ID")
    stamp = snapshot.get("computed_at_unix")
    clock = time.time() if now is None else now
    if (isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or
        isinstance(clock, bool) or not isinstance(clock, (float, int)) or
        not math.isfinite(stamp) or not math.isfinite(clock)):
        raise ValueError("missing or invalid snapshot timestamp")
    age = clock - stamp
    # Disallow future-dated snapshots rather than silently making them ready.
    recent = 0 <= age <= MAX_LIVE_AGE_SECONDS
    regions = snapshot.get("regions")
    if not isinstance(regions, dict) or any(r not in regions for r in REGIONS):
        raise ValueError("both regions required")

    values: dict[str, dict[str, float | int | None]] = {}
    missing: dict[str, dict[str, bool]] = {}
    quality: dict[str, dict] = {}
    provenance: dict[str, dict] = {}
    for region in REGIONS:
        bundle = regions[region]
        if not isinstance(bundle, dict) or not isinstance(bundle.get("features"), dict):
            raise ValueError("invalid region feature bundle")
        raw = bundle["features"]
        row = {}
        for name in FEATURE_COLUMNS:
            value = raw.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"invalid numeric value: {region}:{name}")
            row[name] = value
        values[region] = row
        missing[region] = {name: value is None for name, value in row.items()}
        quality[region] = dict(bundle.get("quality") or {})
        provenance[region] = dict(bundle.get("provenance") or {})

    source_ready = bool(snapshot.get(
        "ready_for_internal_only_model" if mode == "internal_only" else "ready_for_model", False))
    reasons = list(snapshot.get("quality_reasons") or [])
    if not recent:
        reasons.append("feature_snapshot_expired_or_future_dated")
    if not source_ready:
        reasons.append(f"{mode}_source_quality_not_ready")
    return {
        "contract_version": MODEL_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "mode": mode,
        "computed_at_unix": stamp,
        "age_seconds": round(age, 3),
        "ready_for_inference": source_ready and recent,
        "quality_reasons": list(dict.fromkeys(reasons)),  # ordered, deduplicated
        "feature_columns": list(FEATURE_COLUMNS),
        "features": values,
        "missing": missing,
        "quality": quality,
        "provenance": provenance,
        "comparison": dict(snapshot.get("comparison") or {}),
        "actions_enabled": False,
    }


def conservative_rule_baseline(model_input: dict) -> dict:
    """Working shadow-mode example; NOT trained ML and NEVER recommends routing.

    A classmate replaces this function with predict(model_input) returning the
    same proposal schema. Unknown/stale telemetry leads to investigate, not shift.
    """
    evidence = []
    if not model_input.get("ready_for_inference"):
        action, confidence = "investigate", 0.5
        evidence.append("model_input_not_ready")
    else:
        action, confidence = "hold", 0.8
        for region in REGIONS:
            f = model_input["features"][region]
            if f["app_up"] != 1:
                evidence.append(f"{region}:app_not_confirmed_up")
            if f["cpu_busy_pct"] is not None and f["cpu_busy_pct"] >= 85:
                evidence.append(f"{region}:high_cpu")
            if f["memory_available_pct"] is not None and f["memory_available_pct"] < 15:
                evidence.append(f"{region}:low_memory")
        if evidence:
            action, confidence = "investigate", 0.9
        else:
            evidence.append("no_rule_threshold_breached")
    return {
        "contract_version": PROPOSAL_VERSION,
        "snapshot_id": model_input["snapshot_id"],
        "action": action,
        "confidence": confidence,
        "evidence": evidence,
        "model_id": "conservative_rule_baseline_v1",  # metadata only
    }
