"""End-to-end decision path: proposal -> independent safety review -> EKS decision.

The result is always one of three decisions:

* ``scale``    raise one EKS HPA minimum by one replica in the affected region
* ``failover`` shift traffic away from the affected region in one bounded step
* ``no-op``    anything else, with the reasons that blocked an action

``decide`` is pure. It never calls AWS or Kubernetes and never executes.
Callers supply HPA observations they have already read (or fixtures).
Execution is a separate step in ``scripts/end_to_end.py`` that is off by
default and refuses to run without an explicit live-write approval.

Model output is untrusted input. The mapping from a model condition to a
candidate action is deliberately small and conservative so the safety
reviewer, not the model, has the final word.
"""
from __future__ import annotations

from dataclasses import asdict
import math
import time
from typing import Mapping

from adaptivenet.ingestion.decisions import review_proposal, scale_proposal_rejection_reasons

from .scaling import HpaObservation, WorkloadTarget, recommend_capacity

DECISION_VERSION = "adaptivenet.eks_decision.v1"
PROPOSAL_VERSION = "adaptivenet.proposal.v1"
REGIONS = ("region-a", "region-b")
# Mirrors kubectl_connection.TARGETS; kept here so decide() needs no kubectl.
SITE_BY_REGION = {"region-a": "sg", "region-b": "us"}
ALLOWLIST: Mapping[str, WorkloadTarget] = {
    "region-a": WorkloadTarget("adaptivenet-sg", "adaptivenet", "demo-sg", "demo-sg"),
    "region-b": WorkloadTarget("adaptivenet-us", "adaptivenet", "demo-us", "demo-us"),
}
FAILOVER_STEP_PERCENT = 25  # the reviewer's per-step safety limit
DEGRADED_ERROR_RATIO = 0.20
DEGRADED_P95_MS = 800
COMPUTE_BOUND_CPU_PCT = 80
NETWORK_LOSS_PCT = 20       # median per-probe loss
NETWORK_RTT_P50_MS = 400
MIN_RIPE_PROBES = 3
NETWORK_CONDITIONS = ("network_degradation", "partial_outage")
APPLICATION_CONDITIONS = ("application_failure",)
QUIET_CONDITIONS = ("healthy", "normal", "recovery", "transient")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def internal_health(snapshot: dict, region: str) -> str:
    """AWS-internal view of one region: healthy, degraded or unknown."""
    bundle = (snapshot.get("regions") or {}).get(region) or {}
    features = bundle.get("features") or {}
    quality = bundle.get("quality") or {}
    if quality.get("internal") != "fresh":
        return "unknown"
    app_up = features.get("app_up")
    if app_up == 0:
        return "degraded"
    if app_up != 1:
        return "unknown"
    errors = _number(features.get("http_error_ratio"))
    p95 = _number(features.get("http_p95_ms"))
    if (errors is not None and errors >= DEGRADED_ERROR_RATIO) or (p95 is not None and p95 >= DEGRADED_P95_MS):
        return "degraded"
    return "healthy"


def network_health(snapshot: dict, region: str) -> tuple[str, list[str]]:
    """RIPE Atlas view of one region, used only when external quality is fresh.

    The mean ``packet_loss_pct`` is not trusted: one permanently unreachable
    probe out of four pins it at 25%. Loss is judged on the per-probe median,
    and probes at 100% loss are flagged unless every probe agrees.
    """
    bundle = (snapshot.get("regions") or {}).get(region) or {}
    features = bundle.get("features") or {}
    if (bundle.get("quality") or {}).get("external") != "fresh":
        return "unknown", ["ripe_not_fresh"]
    probes, full = features.get("ripe_probes_reporting"), features.get("ripe_probes_full_loss")
    if type(probes) is not int or probes < MIN_RIPE_PROBES:
        return "unknown", ["too_few_ripe_probes"]
    flags = []
    if type(full) is int and 0 < full < probes:
        flags.append(f"ignored_unreachable_ripe_probes:{full}")
    if type(full) is int and full == probes:
        return "degraded", ["all_ripe_probes_unreachable"]
    loss = _number(features.get("packet_loss_median_pct"))
    rtt = _number(features.get("network_rtt_p50_ms"))
    if loss is None and rtt is None:
        return "unknown", [*flags, "no_robust_ripe_loss_or_rtt"]
    if loss is not None and loss >= NETWORK_LOSS_PCT:
        return "degraded", [*flags, f"ripe_median_loss_pct:{loss:g}"]
    if rtt is not None and rtt >= NETWORK_RTT_P50_MS:
        return "degraded", [*flags, f"ripe_rtt_p50_ms:{rtt:g}"]
    return "healthy", flags


def region_health(snapshot: dict, region: str) -> str:
    """Combine internal and network views; network-only degradation is a gray failure."""
    internal = internal_health(snapshot, region)
    if internal != "healthy":
        return internal
    return "degraded" if network_health(snapshot, region)[0] == "degraded" else "healthy"


def affected_region(snapshot: dict) -> tuple[str | None, str]:
    """Return the single degraded region when the other one is confirmed healthy."""
    health = {region: region_health(snapshot, region) for region in REGIONS}
    degraded = [r for r, h in health.items() if h == "degraded"]
    healthy = [r for r, h in health.items() if h == "healthy"]
    if len(degraded) == 1 and len(healthy) == 1:
        region = degraded[0]
        if internal_health(snapshot, region) == "healthy":
            return region, "network_only_degradation_other_healthy"
        return region, "one_region_degraded_other_healthy"
    if not degraded:
        return None, "no_region_degraded"
    if len(degraded) == 2:
        return None, "both_regions_degraded"
    return None, "other_region_health_unknown"


def signals(snapshot: dict) -> dict:
    """Per-region evidence behind the affected-region choice (for the decision record)."""
    out = {}
    for region in REGIONS:
        state, flags = network_health(snapshot, region)
        out[region] = {"internal": internal_health(snapshot, region), "network": state, "network_flags": flags}
    return out


def _condition(proposal: dict) -> str | None:
    for item in proposal.get("evidence") or ():
        if isinstance(item, str) and item.startswith("shadow_condition:"):
            return item.split(":", 1)[1]
    return None


def map_model_output(snapshot: dict, model_proposal: dict, *, current_region_a_percent: int) -> tuple[dict, dict]:
    """Turn a model proposal into a candidate action proposal (still untrusted).

    A proposal that already asks for ``shift_traffic`` or ``scale_out`` passes
    through unchanged. A shadow model's ``investigate`` with a
    ``shadow_condition:<label>`` is mapped as follows:

    * network_degradation / partial_outage -> shift away from the degraded region
    * application_failure with CPU >= 80% in that region -> scale_out
    * application_failure otherwise -> shift away from the degraded region
    * anything else -> unchanged (the reviewer then holds)
    """
    action = model_proposal.get("action")
    if action in ("shift_traffic", "scale_out"):
        return dict(model_proposal), {"mapping": "passthrough"}
    condition = _condition(model_proposal)
    note = {"mapping": "none", "condition": condition}
    if action != "investigate" or condition is None or condition in QUIET_CONDITIONS:
        return dict(model_proposal), note
    if condition not in NETWORK_CONDITIONS + APPLICATION_CONDITIONS:
        return dict(model_proposal), {**note, "mapping": "unmapped_condition"}
    region, why = affected_region(snapshot)
    note.update(affected_region=region, affected_region_reason=why, signals=signals(snapshot))
    if region is None:
        return dict(model_proposal), {**note, "mapping": "affected_region_unclear"}

    candidate = {
        "contract_version": PROPOSAL_VERSION,
        "snapshot_id": model_proposal.get("snapshot_id"),
        "confidence": model_proposal.get("confidence"),
        "model_id": model_proposal.get("model_id"),
        "affected_region": region,
        "evidence": [*(model_proposal.get("evidence") or []), f"mapped_from:{condition}", f"affected_region:{region}"],
    }
    features = ((snapshot.get("regions") or {}).get(region) or {}).get("features") or {}
    cpu = _number(features.get("cpu_busy_pct"))
    if condition in APPLICATION_CONDITIONS and cpu is not None and cpu >= COMPUTE_BOUND_CPU_PCT:
        return {**candidate, "action": "scale_out"}, {**note, "mapping": "scale_out"}
    if region == "region-a":
        target = max(0, current_region_a_percent - FAILOVER_STEP_PERCENT)
    else:
        target = min(100, current_region_a_percent + FAILOVER_STEP_PERCENT)
    if target == current_region_a_percent:
        return dict(model_proposal), {**note, "mapping": "route_already_at_limit"}
    return ({**candidate, "action": "shift_traffic", "target_region_a_percent": target},
            {**note, "mapping": "shift_traffic"})


def observation_from_dict(data: dict) -> HpaObservation:
    """Build an HpaObservation from ``dataclasses.asdict`` output (fixtures, captures)."""
    fields = dict(data)
    fields["target"] = WorkloadTarget(**fields["target"])
    return HpaObservation(**fields)


def _no_op(base: dict, reasons) -> dict:
    return {**base, "decision": "no-op", "reasons": list(reasons) or ["no_action_requested"], "plan": None}


def decide(
    snapshot: dict, model_proposal: dict, *,
    current_region_a_percent: int = 50,
    hpa_observations: Mapping[str, HpaObservation | None] | None = None,
    now: float | None = None,
) -> dict:
    """Return a dry-run EKS decision. No side effects."""
    now = time.time() if now is None else now
    observations = dict(hpa_observations or {})
    proposal, mapping = map_model_output(
        snapshot, model_proposal, current_region_a_percent=current_region_a_percent,
    )
    action = proposal.get("action")
    base = {
        "decision_version": DECISION_VERSION,
        "dry_run": True,
        "executed": False,
        "snapshot_id": snapshot.get("snapshot_id"),
        "model_proposal": model_proposal,
        "mapping": mapping,
        "proposal": proposal,
    }

    if action == "scale_out":
        reasons = scale_proposal_rejection_reasons(snapshot, proposal)
        base["review"] = {"reviewer": "scale_proposal_rejection_reasons", "policy_valid": not reasons,
                          "rejection_reasons": list(reasons)}
        if reasons:
            return _no_op(base, reasons)
        region = proposal.get("affected_region")
        if region not in ALLOWLIST:
            return _no_op(base, ["unknown_affected_region"])
        observation = observations.get(region)
        if observation is None:
            return _no_op(base, ["missing_hpa_observation"])
        cpu, target_cpu = observation.cpu_current_pct, observation.cpu_target_pct
        if type(cpu) is not int or type(target_cpu) is not int or cpu < target_cpu:
            return _no_op(base, ["compute_evidence_missing_or_below_hpa_target"])
        recommendation = recommend_capacity(region, allowlist=ALLOWLIST, observation=observation, now=now)
        base["capacity"] = asdict(recommendation)
        if recommendation.requested_replicas is None:
            return _no_op(base, [recommendation.reason])
        return {**base, "decision": "scale", "reasons": [recommendation.reason], "plan": {
            "site": SITE_BY_REGION[region], "region": region, **asdict(ALLOWLIST[region]),
            "change": "hpa.spec.minReplicas",
            "from_replicas": observation.current, "to_replicas": recommendation.requested_replicas,
            "live_executor": "scripts/end_to_end.py execute (SafetyController mode=eks)",
        }}

    review = review_proposal(snapshot, proposal, current_region_a_percent=current_region_a_percent)
    base["review"] = {"reviewer": "review_proposal", **review}
    if action != "shift_traffic" or not review["eligible_for_operator_review"]:
        return _no_op(base, review["rejection_reasons"])
    target = proposal["target_region_a_percent"]
    source = "region-a" if target < current_region_a_percent else "region-b"
    destination = "region-b" if source == "region-a" else "region-a"
    observation = observations.get(destination)
    if observation is not None:
        if (observation.target != ALLOWLIST[destination]
                or _number(observation.observed_at) is None
                or not now - 60 <= observation.observed_at <= now
                or observation.ready < observation.min_replicas
                or observation.available < observation.ready):
            return _no_op(base, ["destination_capacity_not_confirmed"])
    return {**base, "decision": "failover", "reasons": ["reviewer_eligible_shift"], "plan": {
        "from_region": source, "to_region": destination,
        "region_a_percent": {"from": current_region_a_percent, "to": target},
        "destination_capacity_checked": observation is not None,
        # EKS has no traffic actuator; only the local gateway can apply this today.
        "live_executor": None,
        "live_executor_reason": "eks_routing_not_implemented",
        "local_executor": "SafetyController mode=local (local gateway)",
    }}
