"""Pure validation boundary. Never calls AWS/Kubernetes, never executes routing."""
from __future__ import annotations
import math
import time

ALLOWED_ACTIONS = ("hold", "investigate", "shift_traffic", "scale_out")

def review_proposal(snapshot: dict, proposal: dict, *, current_region_a_percent: int = 50,
                    scale_adapter_available: bool = False, current_replicas: dict | None = None,
                    max_replicas: int = 6, max_scale_step: int = 2, input_mode: str = "combined") -> dict:
    """Model -> controller handoff. Proposed actions require future actuator gates.

    Model output is untrusted input, even from a model hosted locally. No side effects.
    """
    reasons = []
    if not isinstance(proposal, dict):
        reasons.append("proposal_not_an_object")
        proposal = {}
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
    elif action not in ("hold", "investigate") and confidence < .8:
        reasons.append("insufficient_confidence")
    if isinstance(current_region_a_percent, bool) or not isinstance(current_region_a_percent, int) or not 0<=current_region_a_percent<=100:
        reasons.append("invalid_current_route_weight")
    if action not in ("hold", "investigate"):
        computed_at = snapshot.get("computed_at_unix")
        if (not isinstance(computed_at, (float, int)) or isinstance(computed_at, bool)
            or not math.isfinite(computed_at) or not 0 <= time.time()-computed_at <= 180):
            reasons.append("feature_snapshot_expired_or_invalid")
    ready_flag = "ready_for_internal_only_model" if input_mode == "internal_only" else "ready_for_model"
    if not snapshot.get(ready_flag) and action not in ("hold", "investigate"):
        reasons.append("telemetry_quality_gate_failed")
    if action == "scale_out":
        if not scale_adapter_available:
            reasons.append("kubernetes_scale_adapter_not_implemented")
        else:
            region, target = proposal.get("region"), proposal.get("target_replicas")
            current = (current_replicas or {}).get(region) if isinstance(region, str) else None
            if region not in ("region-a", "region-b"):
                reasons.append("invalid_scale_region")
            if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= max_replicas:
                reasons.append("invalid_target_replicas")
            elif isinstance(current, int) and not isinstance(current, bool):
                if target <= current:
                    reasons.append("scale_out_must_increase_replicas")
                elif target - current > max_scale_step:
                    reasons.append("scale_step_above_safety_limit")
            else:
                reasons.append("current_replicas_unknown")
            if not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
                reasons.append("missing_model_evidence")
    if action == "shift_traffic":
        target = proposal.get("target_region_a_percent")
        if isinstance(target, bool) or not isinstance(target, int) or not 0<=target<=100:
            reasons.append("invalid_route_weight")
        elif target == current_region_a_percent:
            reasons.append("route_weight_unchanged")
        elif isinstance(current_region_a_percent, int) and abs(target-current_region_a_percent)>25:
            reasons.append("route_delta_above_25_percent_safety_limit")
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
        if proposal.get("evidence") is None or not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
            reasons.append("missing_model_evidence")
    eligible = not reasons and (action == "shift_traffic" or (action == "scale_out" and scale_adapter_available))
    return {"review_version": "adaptivenet.decision_review.v1",
            "snapshot_id": snapshot.get("snapshot_id"), "requested_action": action,
            "policy_valid": not reasons, "eligible_for_operator_review": eligible,
            "approved_for_execution": False, "executed": False,
            "effective_action": "hold", "rejection_reasons": reasons,
            "required_future_gates": ["authenticated operator permission", "route ownership",
                "live destination health verification", "cooldown", "rate limit", "idempotency key",
                "pre-action snapshot", "rollback verification", "audit logging"]}
