"""Read-only capacity recommendations for the emulated HPA path."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

HPA_MAX_AGE_SEC = 60
LOCAL_MIN_REPLICAS = 2
LOCAL_MAX_REPLICAS = 6


@dataclass(frozen=True)
class WorkloadTarget:
    cluster: str
    namespace: str
    deployment: str
    hpa: str


@dataclass(frozen=True)
class HpaObservation:
    target: WorkloadTarget
    observed_at: float
    metrics_observed_at: float | None
    min_replicas: int
    max_replicas: int
    current: int
    desired: int
    ready: int
    available: int
    pending: int
    able_to_scale: bool | None
    scaling_active: bool | None
    scaling_limited: bool | None
    cpu_target_pct: int | None = None
    cpu_current_pct: int | None = None
    restart_count: int | None = None


@dataclass(frozen=True)
class CapacityRecommendation:
    state: Literal["recommend", "already_in_progress", "wait", "blocked"]
    reason: str
    requested_replicas: int | None = None


def recommend_capacity(
    affected_region: str | None, *,
    allowlist: Mapping[str, WorkloadTarget],
    observation: HpaObservation | None,
    now: float,
) -> CapacityRecommendation:
    """Evaluate capacity after the controller has independently qualified a scale proposal."""
    target = allowlist.get(affected_region) if isinstance(affected_region, str) else None
    if target is None:
        return CapacityRecommendation("blocked", "unknown_affected_region")
    if observation is None:
        return CapacityRecommendation("blocked", "missing_hpa_observation")
    if observation.target != target:
        return CapacityRecommendation("blocked", "hpa_target_mismatch")
    if not all(
        type(at) in (int, float) and now - HPA_MAX_AGE_SEC <= at <= now
        for at in (observation.observed_at, observation.metrics_observed_at)
    ):
        return CapacityRecommendation("blocked", "stale_or_invalid_hpa_evidence")

    counts = (
        observation.min_replicas, observation.max_replicas, observation.current,
        observation.desired, observation.ready, observation.available, observation.pending,
    )
    if not all(type(count) is int and count >= 0 for count in counts) or not (
        LOCAL_MIN_REPLICAS <= observation.min_replicas <= observation.max_replicas <= LOCAL_MAX_REPLICAS
        and observation.min_replicas <= observation.desired <= observation.max_replicas
        and observation.available <= observation.ready <= observation.current
    ):
        return CapacityRecommendation("blocked", "invalid_hpa_replica_state")
    if observation.able_to_scale is not True or observation.scaling_active is not True:
        return CapacityRecommendation("blocked", "hpa_unavailable_or_inactive")
    if observation.desired > observation.current:
        return CapacityRecommendation("already_in_progress", "hpa_already_requested_capacity")
    if observation.current >= observation.max_replicas:
        return CapacityRecommendation("blocked", "hpa_at_max_replicas")
    if observation.scaling_limited is not False:
        return CapacityRecommendation("blocked", "hpa_scaling_limited_or_unknown")
    if observation.desired < observation.current:
        return CapacityRecommendation("wait", "hpa_scale_down_in_progress")
    if observation.pending or observation.available < observation.current:
        return CapacityRecommendation("wait", "replicas_not_yet_available")
    return CapacityRecommendation("recommend", "one_replica_within_hpa_bounds", observation.current + 1)
