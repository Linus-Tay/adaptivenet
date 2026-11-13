"""In-memory HPA emulator for deterministic local scale-out experiments."""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import inf
from typing import Literal

from .scaling import CapacityRecommendation, HpaObservation, WorkloadTarget, recommend_capacity

ScaleState = Literal["requested", "pending", "ready", "failed", "timed_out"]
PodEvent = Literal["pending", "ready", "failed"]


@dataclass(frozen=True)
class EmulatedScaleAction:
    action_id: str
    requested_replicas: int
    requested_at: float
    state: ScaleState
    ready_at: float | None = None


class EmulatedHpa:
    """Owns synthetic replica counts for one configured workload."""

    def __init__(
        self, region: str, target: WorkloadTarget, initial: HpaObservation, *,
        readiness_timeout_sec: float = 30,
    ):
        if target != initial.target:
            raise ValueError("initial observation does not match configured workload")
        if not 0 < readiness_timeout_sec < inf:
            raise ValueError("readiness timeout must be positive and finite")
        self._region = region
        self._allowlist = {region: target}
        self._observation = initial
        self._last_now = initial.observed_at
        self._timeout_sec = readiness_timeout_sec
        self._actions: dict[str, EmulatedScaleAction] = {}
        self._latest: EmulatedScaleAction | None = None

    def observe(self) -> HpaObservation:
        """Read synthetic state without advancing the clock or refreshing metrics."""
        return self._observation

    def observe_metrics(self, *, observed_at: float) -> None:
        """Feed a new synthetic metric sample. Reading the adapter never refreshes it."""
        self._advance_clock(observed_at)
        self._observation = replace(
            self._observation, observed_at=observed_at, metrics_observed_at=observed_at,
        )

    def assess(self, region: str | None, *, now: float) -> CapacityRecommendation:
        return recommend_capacity(region, allowlist=self._allowlist, observation=self.observe(), now=now)

    def request_capacity(self, action_id: str, *, replicas: int, now: float) -> EmulatedScaleAction:
        if not action_id or type(replicas) is not int:
            raise ValueError("capacity request needs an ID and integer replica count")
        previous = self._actions.get(action_id)
        if previous is not None:
            if previous.requested_replicas != replicas:
                raise ValueError("action ID already has a different replica target")
            return previous
        if self._latest is not None and self._latest.state != "ready":
            raise ValueError("previous scaling action is unresolved")

        self._advance_clock(now)
        recommendation = self.assess(self._region, now=now)
        if recommendation.state != "recommend":
            raise ValueError(recommendation.reason)
        if replicas != recommendation.requested_replicas:
            raise ValueError("capacity request does not match current recommendation")

        action = EmulatedScaleAction(action_id, replicas, now, "requested")
        self._observation = replace(self._observation, desired=replicas, observed_at=now)
        self._save(action)
        return action

    def poll_action(self, action_id: str, *, now: float) -> EmulatedScaleAction | None:
        if self._latest is not None and self._latest.action_id == action_id:
            return self.advance(now=now)
        return self._actions.get(action_id)

    def advance(self, *, now: float, pod_event: PodEvent | None = None) -> EmulatedScaleAction:
        """Apply a fixture event or check whether the latest request timed out."""
        action = self._latest
        if action is None:
            raise ValueError("no scaling action to advance")
        if action.state in ("ready", "failed", "timed_out"):
            return action
        if pod_event not in (None, "pending", "ready", "failed"):
            raise ValueError("unknown pod event")
        self._advance_clock(now)
        if now >= action.requested_at + self._timeout_sec:
            action = replace(action, state="timed_out")
        elif pod_event is not None:
            action = replace(action, state=pod_event)
            if pod_event == "pending":
                self._observation = replace(
                    self._observation, current=action.requested_replicas, pending=1, observed_at=now,
                )
            elif pod_event == "ready":
                action = replace(action, ready_at=now)
                self._observation = replace(
                    self._observation, current=action.requested_replicas,
                    ready=action.requested_replicas, available=action.requested_replicas,
                    pending=0, observed_at=now,
                )
        self._save(action)
        return action

    def _advance_clock(self, now: float) -> None:
        if type(now) not in (int, float) or not self._last_now <= now < inf:
            raise ValueError("simulation time must be finite and nondecreasing")
        self._last_now = now

    def _save(self, action: EmulatedScaleAction) -> None:
        self._actions[action.action_id] = action
        self._latest = action
