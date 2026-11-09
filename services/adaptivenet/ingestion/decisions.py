"""Pure validation boundary. Never calls AWS/Kubernetes, never executes routing."""
from __future__ import annotations
import time

ALLOWED_ACTIONS = ("hold", "investigate", "shift_traffic", "scale_out")


def _valid_route_weight(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def proposal_rejection_reasons(snapshot: dict, proposal: dict) -> list[str]:
    """Check the proposal and snapshot requirements common to both action paths."""
    reasons = []
    if proposal.get("contract_version") != "adaptivenet.proposal.v1":
        reasons.append("proposal_contract_mismatch")
    if proposal.get("snapshot_id") != snapshot.get("snapshot_id"):
        reasons.append("snapshot_id_mismatch")
    action = proposal.get("action")
    if action not in ALLOWED_ACTIONS:
        reasons.append("unrecognized_action")
    confidence = proposal.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        reasons.append("invalid_confidence")
    elif action not in ("hold", "investigate") and confidence < .8:
        reasons.append("insufficient_confidence")
    if action not in ("hold", "investigate"):
        computed_at = snapshot.get("computed_at_unix")
        now = time.time()
        if (not isinstance(computed_at, (float, int)) or isinstance(computed_at, bool)
            or not now - 180 <= computed_at <= now):
            reasons.append("feature_snapshot_expired_or_invalid")
    if snapshot.get("ready_for_model") is not True and action not in ("hold", "investigate"):
        reasons.append("telemetry_quality_gate_failed")
    return reasons


def scale_proposal_rejection_reasons(snapshot: dict, proposal: dict) -> list[str]:
    reasons = proposal_rejection_reasons(snapshot, proposal)
    if proposal.get("action") != "scale_out":
        reasons.append("scale_out_proposal_required")
    if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"]:
        reasons.append("missing_snapshot_id")
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        reasons.append("missing_model_evidence")
    return reasons


def review_proposal(snapshot: dict, proposal: dict, *, current_region_a_percent: int = 50) -> dict:
    """Model -> controller handoff. Proposed actions require future actuator gates.

    Model output is untrusted input, even from a model hosted locally. No side effects.
    """
    reasons = proposal_rejection_reasons(snapshot, proposal)
    action = proposal.get("action")
    current_weight_valid = _valid_route_weight(current_region_a_percent)
    if not current_weight_valid:
        reasons.append("invalid_current_route_weight")
    if action == "scale_out":
        reasons.append("kubernetes_scale_adapter_not_implemented")
    if action == "shift_traffic":
        target = proposal.get("target_region_a_percent")
        if not _valid_route_weight(target):
            reasons.append("invalid_route_weight")
        elif current_weight_valid:
            delta = target - current_region_a_percent
            if delta == 0:
                reasons.append("route_weight_unchanged")
            else:
                if abs(delta) > 25:
                    reasons.append("route_delta_above_25_percent_safety_limit")
                destination = "region-b" if delta < 0 else "region-a"
                region = snapshot.get("regions",{}).get(destination,{})
                features = region.get("features",{})
                quality = region.get("quality",{})
                if quality.get("internal") != "fresh" or features.get("app_up") != 1:
                    reasons.append("destination_not_confirmed_healthy")
                memory = features.get("memory_available_pct")
                if not isinstance(memory, (int, float)) or not 15 <= memory <= 100:
                    reasons.append("destination_low_or_unknown_memory")
                cpu = features.get("cpu_busy_pct")
                if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not 0 <= cpu < 85:
                    reasons.append("destination_high_or_unknown_cpu")
        if proposal.get("evidence") is None or not isinstance(proposal.get("evidence"), list) or not proposal["evidence"]:
            reasons.append("missing_model_evidence")
    eligible = not reasons and action == "shift_traffic"
    return {"review_version": "adaptivenet.decision_review.v1",
            "snapshot_id": snapshot.get("snapshot_id"), "requested_action": action,
            "policy_valid": not reasons, "eligible_for_operator_review": eligible,
            "approved_for_execution": False, "executed": False,
            "effective_action": "hold", "rejection_reasons": reasons,
            "required_future_gates": ["authenticated operator permission", "route ownership",
                "live destination health verification", "cooldown", "rate limit", "idempotency key",
                "pre-action snapshot", "rollback verification", "audit logging"]}
