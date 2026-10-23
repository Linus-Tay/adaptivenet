"""Pure validation boundary. Never calls AWS/Kubernetes, never executes routing.

The controller (services/adaptivenet/controller/mitigation.py) is the only code
that turns a *policy_valid* review into an action, and it applies its own
hysteresis, cooldown, bounded steps and recovery verification on top.
"""
from __future__ import annotations
import math
import time

ALLOWED_ACTIONS = ("hold", "investigate", "shift_traffic", "scale_out", "failover")
PASSIVE = ("hold", "investigate")
REGIONS = ("region-a", "region-b")

def _only_affected_loss(snapshot: dict, affected: str | None) -> bool:
    """Readiness fails only because the affected region's user path reports total loss
    (no RTT samples). That is evidence of the failure, not missing telemetry."""
    reasons = list(snapshot.get("quality_reasons") or [])
    return bool(affected) and bool(reasons) and all(r == f"{affected}:external:loss_only" for r in reasons)


def review_proposal(snapshot: dict, proposal: dict, *, current_region_a_percent: int = 50,
                    mode: str = "combined", max_route_delta: int = 25,
                    scale_adapter_available: bool = False, now: float | None = None) -> dict:
    """Model -> controller handoff. Model output is untrusted input; no side effects.

    mode: which readiness flag of the snapshot gates active actions
          ("combined" = AWS + RIPE, "internal_only" = AWS/in-region only).
    max_route_delta: largest single route-weight change (pp) a proposal may ask for.
                     The controller passes 100 for `failover`, which is only valid
                     when the destination region is confirmed healthy.
    """
    now = time.time() if now is None else now
    reasons = []
    if proposal.get("contract_version") != "adaptivenet.proposal.v1":
        reasons.append("proposal_contract_mismatch")
    if proposal.get("snapshot_id") != snapshot.get("snapshot_id"):
        reasons.append("snapshot_id_mismatch")
    action = proposal.get("action")
    if action not in ALLOWED_ACTIONS:
        reasons.append("unrecognized_action")
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int,float)) or not math.isfinite(confidence) or not 0<=confidence<=1:
        reasons.append("invalid_confidence")
    elif action not in PASSIVE and confidence < (.9 if action == "failover" else .8):
        reasons.append("insufficient_confidence")
    if isinstance(current_region_a_percent, bool) or not isinstance(current_region_a_percent, int) or not 0<=current_region_a_percent<=100:
        reasons.append("invalid_current_route_weight")
    if action not in PASSIVE:
        computed_at = snapshot.get("computed_at_unix")
        if (not isinstance(computed_at, (float, int)) or isinstance(computed_at, bool)
            or not math.isfinite(computed_at) or not 0 <= now-computed_at <= 180):
            reasons.append("feature_snapshot_expired_or_invalid")
        ready_flag = "ready_for_internal_only_model" if mode == "internal_only" else "ready_for_model"
        if not snapshot.get(ready_flag) and not _only_affected_loss(snapshot, proposal.get("affected_region")):
            reasons.append("telemetry_quality_gate_failed")
        if proposal.get("evidence") is None or not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
            reasons.append("missing_model_evidence")
    if action == "scale_out":
        if not scale_adapter_available:
            reasons.append("kubernetes_scale_adapter_not_implemented")
        if proposal.get("affected_region") not in REGIONS:
            reasons.append("scale_target_region_missing")
    if action in ("shift_traffic", "failover"):
        target = proposal.get("target_region_a_percent")
        if isinstance(target, bool) or not isinstance(target, int) or not 0<=target<=100:
            reasons.append("invalid_route_weight")
        elif target == current_region_a_percent:
            reasons.append("route_weight_unchanged")
        elif isinstance(current_region_a_percent, int) and abs(target-current_region_a_percent)>max_route_delta:
            reasons.append(f"route_delta_above_{max_route_delta}_percent_safety_limit")
        if isinstance(target,int) and not isinstance(target,bool) and 0<=target<=100:
            # A lower A share means B receives more traffic; validate that destination.
            destination = "region-b" if target < current_region_a_percent else "region-a" if target > current_region_a_percent else None
            if destination:
                region = snapshot.get("regions",{}).get(destination,{})
                features = region.get("features",{})
                quality = region.get("quality",{})
                if quality.get("internal") != "fresh" or features.get("app_up") != 1:
                    reasons.append("destination_not_confirmed_healthy")
                if features.get("memory_available_pct") is None or features["memory_available_pct"] < 15:
                    reasons.append("destination_low_or_unknown_memory")
                if features.get("cpu_busy_pct") is None or features["cpu_busy_pct"] > 85:
                    reasons.append("destination_high_or_unknown_cpu")
                ratio = features.get("http_error_ratio")
                if ratio is not None and ratio >= 0.2:
                    reasons.append("destination_serving_errors")
    eligible = not reasons and action not in PASSIVE
    return {"review_version": "adaptivenet.decision_review.v1",
            "snapshot_id": snapshot.get("snapshot_id"), "requested_action": action,
            "policy_valid": not reasons, "eligible_for_operator_review": eligible,
            # The reviewer never approves or executes; only the controller can, and it
            # records approval in its own journal entry for the action.
            "approved_for_execution": False, "executed": False,
            "effective_action": "hold", "rejection_reasons": reasons,
            "required_future_gates": ["authenticated operator permission", "route ownership",
                "live destination health verification", "cooldown", "rate limit", "idempotency key",
                "pre-action snapshot", "rollback verification", "audit logging"]}
