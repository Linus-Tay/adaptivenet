"""Evaluate a versioned prediction without attaching any actuator."""
from __future__ import annotations

from dataclasses import asdict
import json
from typing import Callable

from adaptivenet.ingestion.decisions import review_proposal, scale_proposal_rejection_reasons
from adaptivenet.ingestion.handoff import Mode, build_model_input

from .kubernetes_shadow import KubernetesShadowHpa


def assess_shadow(
    snapshot: dict, predictor: Callable[[dict], dict], *, mode: Mode = "combined",
    current_region_a_percent: int | None = None, hpa: KubernetesShadowHpa | None = None,
) -> dict:
    result = {
        "contract_version": "adaptivenet.controller_shadow.v1",
        "mode": "shadow",
        "input_mode": mode,
        "state": "blocked",
        "executed": False,
        "effective_action": "hold",
    }
    try:
        model_input = build_model_input(snapshot, mode=mode)
    except (TypeError, ValueError, OverflowError):
        return {**result, "reasons": ["invalid_feature_snapshot"]}
    result["snapshot_id"] = model_input["snapshot_id"]
    if not model_input["ready_for_inference"]:
        return {**result, "reasons": model_input["quality_reasons"]}

    try:
        proposal = predictor(model_input)
    except Exception:
        return {**result, "reasons": ["predictor_failed"]}
    if not isinstance(proposal, dict):
        return {**result, "reasons": ["invalid_proposal"]}
    try:
        json.dumps(proposal, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return {**result, "reasons": ["invalid_proposal"]}
    result["proposal"] = proposal

    action = proposal.get("action")
    try:
        if action == "shift_traffic":
            review = review_proposal(snapshot, proposal, current_region_a_percent=current_region_a_percent)
        else:
            review = review_proposal(snapshot, proposal)
    except (TypeError, AttributeError):
        return {**result, "reasons": ["invalid_feature_snapshot"]}
    result["decision_review"] = review

    if action == "scale_out":
        reasons = scale_proposal_rejection_reasons(snapshot, proposal)
        if reasons:
            return {**result, "reasons": reasons}
        if hpa is None:
            return {**result, "reasons": ["missing_kubernetes_context"]}
        capacity = hpa.assess(proposal.get("affected_region"))
        return {
            **result, "state": "capacity_review_only" if capacity.recommendation.state == "recommend"
            else capacity.recommendation.state,
            "reasons": [capacity.recommendation.reason], "capacity": asdict(capacity),
        }
    if not review["policy_valid"]:
        return {**result, "reasons": review["rejection_reasons"]}
    state = "route_review_only" if review["eligible_for_operator_review"] else "observing"
    return {**result, "state": state, "reasons": []}
