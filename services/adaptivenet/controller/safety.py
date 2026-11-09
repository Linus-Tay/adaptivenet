"""Single-runner controller for bounded local mitigation steps."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import sqlite3
import time
from typing import Literal, Protocol
from uuid import uuid4

from adaptivenet.ingestion.decisions import review_proposal, scale_proposal_rejection_reasons
from adaptivenet.ingestion.fast_signals import MAX_FRESH_AGE_SEC

from .emulated_hpa import EmulatedHpa
from .evidence import EvidenceStreak
from .journal import ActionJournal, PendingAction
from .recovery import (
    SCALE_CLIENT_TIMEOUT_SEC, ClientWindow, RecoveryWindow, evaluate_recovery,
    evaluate_scale_recovery,
)
from .reconcile import reconcile_route
from .scaling import HPA_MAX_AGE_SEC

Mode = Literal["disabled", "shadow", "local"]
COOLDOWN_SEC = 120


class LocalRoute(Protocol):
    """Read authoritative state and change it only if the original still matches."""

    def read_weight(self) -> int: ...

    def request_weight(self, *, original: int, target: int) -> None: ...


@dataclass(frozen=True)
class ControllerResult:
    mode: Mode
    state: str
    reasons: tuple[str, ...] = ()
    action_id: str | None = None
    qualifying_count: int = 0


class SafetyController:
    def __init__(
        self, journal: ActionJournal, route: LocalRoute, *,
        mode: Mode = "shadow", emulated_hpa: EmulatedHpa | None = None,
    ):
        if mode not in ("disabled", "shadow", "local"):
            raise ValueError("invalid controller mode")
        self.mode = mode
        self._journal = journal
        self._route = route
        self._evidence = EvidenceStreak()
        self._hpa = emulated_hpa
        self._scale_evidence = EvidenceStreak()

    def step(
        self, snapshot: dict, proposal: dict, *,
        observed_at: float | None, source_degraded: bool,
        destination_responsive: bool | None = None,
    ) -> ControllerResult:
        """Evaluate a shift using a controller-owned HTTP observation.

        The caller supplies degradation and source time from the selected
        measurement stream for the region that would lose traffic. Continuing
        a sequence also requires a fresh destination probe paired with that sample.
        """
        if self.mode == "disabled":
            return ControllerResult(self.mode, "disabled")
        try:
            pending = self._journal.pending()
            previous = self._journal.outcome(pending.action_id if pending else None)
        except (OSError, sqlite3.Error, ValueError):
            return ControllerResult(self.mode, "blocked", ("journal_read_failed",))
        if previous is not None and previous.state == "blocked":
            return ControllerResult(self.mode, "blocked", (previous.reason,), previous.action_id)
        if pending is not None:
            if pending.intent.startswith("scale:"):
                return ControllerResult(self.mode, "blocked", ("scale_action_pending",), pending.action_id)
            return self._reconcile(pending)

        now = time.time()
        evidence_after = None
        if previous is not None:
            evidence_after = previous.resolved_at
            if previous.state == "recovered":
                evidence_after += COOLDOWN_SEC
                if now < evidence_after:
                    return ControllerResult(self.mode, "cooldown", ("recent_recovery",), previous.action_id)
        if (
            type(observed_at) not in (int, float)
            or not now - MAX_FRESH_AGE_SEC <= observed_at <= now
            or (evidence_after is not None and observed_at <= evidence_after)
        ):
            observed_at = None

        try:
            original = self._route.read_weight()
        except OSError:
            self._evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, "blocked", ("route_read_failed",))

        review = review_proposal(snapshot, proposal, current_region_a_percent=original)
        reasons = tuple(review["rejection_reasons"])
        if not review["eligible_for_operator_review"]:
            self._evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, "observing", reasons)
        if source_degraded is not True or observed_at is None:
            self._evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, "observing", ("degradation_evidence_missing_or_stale",))

        target = proposal["target_region_a_percent"]
        if previous is not None and previous.state == "continue_candidate":
            if original != previous.target_weight:
                self._evidence.observe(observed_at=observed_at, qualified_intent=None)
                return ControllerResult(self.mode, "blocked", ("route_changed_since_verification",))
            if (target - original) * (original - previous.original_weight) <= 0:
                self._evidence.observe(observed_at=observed_at, qualified_intent=None)
                return ControllerResult(self.mode, "blocked", ("continuation_reverses_direction",))
            if destination_responsive is not True:
                self._evidence.observe(observed_at=observed_at, qualified_intent=None)
                return ControllerResult(self.mode, "observing", ("fresh_destination_probe_required",))
        intent = f"shift:{original}->{target}"
        count = self._evidence.observe(observed_at=observed_at, qualified_intent=intent)
        if count < self._evidence.required:
            return ControllerResult(self.mode, "qualifying", qualifying_count=count)
        if self.mode == "shadow":
            return ControllerResult(self.mode, "shadow_qualified", qualifying_count=count)

        pending = self._reserve(
            intent, before={"region_a_percent": original}, expected={"region_a_percent": target},
        )
        if isinstance(pending, ControllerResult):
            return pending

        try:
            self._route.request_weight(original=original, target=target)
        except OSError:
            return self._reconcile(pending, ("route_request_failed",))
        return self._reconcile(pending)

    def step_scale(
        self, snapshot: dict, proposal: dict, *, compute_overloaded: bool,
    ) -> ControllerResult:
        """Qualify a scale proposal using the emulator's controller-owned metric time."""
        if self.mode == "disabled":
            return ControllerResult(self.mode, "disabled")
        try:
            pending = self._journal.pending()
            previous = self._journal.outcome(pending.action_id if pending else None)
        except (OSError, sqlite3.Error, ValueError):
            return ControllerResult(self.mode, "blocked", ("journal_read_failed",))
        if previous is not None and previous.state == "blocked":
            return ControllerResult(self.mode, "blocked", (previous.reason,), previous.action_id)

        now = time.time()
        if pending is not None:
            if pending.intent.startswith("scale:") and self.mode == "local":
                return self._reconcile_scale(pending, now)
            return ControllerResult(self.mode, "blocked", ("action_pending",), pending.action_id)
        if previous is not None and previous.state == "continue_candidate":
            return ControllerResult(self.mode, "blocked", ("routing_sequence_in_progress",), previous.action_id)
        evidence_after = previous.resolved_at + COOLDOWN_SEC if previous is not None else 0
        if now < evidence_after:
            return ControllerResult(self.mode, "cooldown", ("recent_recovery",), previous.action_id)
        if self._hpa is None:
            return ControllerResult(self.mode, "blocked", ("emulated_hpa_unavailable",))

        observation = self._hpa.observe()
        at = observation.metrics_observed_at
        observed_at = (
            at if type(at) in (int, float) and now - HPA_MAX_AGE_SEC <= at <= now and at > evidence_after
            else None
        )
        reasons = scale_proposal_rejection_reasons(snapshot, proposal)
        if reasons:
            self._scale_evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, "observing", tuple(reasons))

        region = proposal.get("affected_region")
        recommendation = self._hpa.assess(region, now=now)
        replicas = recommendation.requested_replicas
        if replicas is None:
            self._scale_evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, recommendation.state, (recommendation.reason,))
        if compute_overloaded is not True or observed_at is None:
            self._scale_evidence.observe(observed_at=observed_at, qualified_intent=None)
            return ControllerResult(self.mode, "observing", ("compute_evidence_missing_or_stale",))

        intent = f"scale:{region}:{observation.current}->{replicas}"
        count = self._scale_evidence.observe(observed_at=observed_at, qualified_intent=intent)
        if count < self._scale_evidence.required:
            return ControllerResult(self.mode, "qualifying", qualifying_count=count)
        if self.mode == "shadow":
            return ControllerResult(self.mode, "shadow_qualified", qualifying_count=count)

        pending = self._reserve(
            intent,
            before=asdict(observation),
            expected={"region": region, "target": asdict(observation.target), "replicas": replicas},
        )
        if isinstance(pending, ControllerResult):
            return pending
        try:
            self._hpa.request_capacity(pending.action_id, replicas=replicas, now=now)
        except ValueError:
            return self._block_scale(pending, "emulated_scale_request_failed")
        return ControllerResult(self.mode, "action_pending", ("emulated_capacity_requested",), pending.action_id)

    def verify_scale(self, window: ClientWindow) -> ControllerResult:
        """Check post-readiness client evidence for the pending emulated scale action."""
        if self.mode == "disabled":
            return ControllerResult(self.mode, "disabled")
        if self.mode != "local":
            return ControllerResult(self.mode, "observing", ("emulated_client_verification_requires_local_mode",))
        try:
            previous = self._journal.outcome(window.action_id)
            pending = self._journal.pending()
        except (OSError, sqlite3.Error, ValueError):
            return ControllerResult(self.mode, "blocked", ("journal_read_failed",))
        if previous is not None and previous.intent.startswith("scale:"):
            return ControllerResult(self.mode, previous.state, (previous.reason,), previous.action_id)
        if pending is None or pending.action_id != window.action_id or not pending.intent.startswith("scale:"):
            return ControllerResult(self.mode, "blocked", ("verification_action_mismatch",))
        return self._reconcile_scale(pending, time.time(), window)

    def _reconcile_scale(
        self, pending: PendingAction, now: float, window: ClientWindow | None = None,
    ) -> ControllerResult:
        if self._hpa is None:
            return self._block_scale(pending, "emulated_scale_outcome_unknown")
        try:
            action = self._hpa.poll_action(pending.action_id, now=now)
        except ValueError:
            return self._block_scale(pending, "emulated_scale_outcome_unknown")
        expected = pending.expected or {}
        observation = self._hpa.observe()
        if (
            action is None
            or expected.get("target") != asdict(observation.target)
            or expected.get("replicas") != action.requested_replicas
        ):
            return self._block_scale(pending, "emulated_scale_outcome_unknown")
        if action.state in ("failed", "timed_out"):
            return self._block_scale(pending, f"emulated_scale_{action.state}")
        if action.state == "ready":
            if (
                not observation.current == observation.desired == observation.ready
                == observation.available == action.requested_replicas
                or observation.pending != 0
            ):
                return self._block_scale(pending, "emulated_replica_state_unexpected")
            ready_at = action.ready_at
            region = expected.get("region")
            if ready_at is None or now < ready_at or not isinstance(region, str):
                return self._block_scale(pending, "emulated_scale_outcome_unknown")
            if now > ready_at + SCALE_CLIENT_TIMEOUT_SEC:
                return self._block_scale(pending, "scale_client_verification_timeout")
            if window is not None:
                state, reason = evaluate_scale_recovery(window, region=region, ready_at=ready_at, now=now)
                return self._record_verification(pending, state, reason)
            return ControllerResult(
                self.mode, "verifying", ("emulated_replicas_ready_client_check_required",), pending.action_id,
            )
        return ControllerResult(self.mode, "action_pending", (f"emulated_scale_{action.state}",), pending.action_id)

    def _block_scale(self, pending: PendingAction, reason: str) -> ControllerResult:
        return self._record_verification(pending, "blocked", reason)

    def _reserve(self, intent: str, *, before: dict, expected: dict) -> PendingAction | ControllerResult:
        action_id = uuid4().hex
        try:
            if not self._journal.record_pending(action_id=action_id, intent=intent, before=before, expected=expected):
                return ControllerResult(self.mode, "blocked", ("pending_action_exists",))
            pending = self._journal.pending()
        except (OSError, sqlite3.Error, ValueError):
            return ControllerResult(self.mode, "blocked", ("journal_reservation_failed",))
        if pending is None or pending.action_id != action_id:
            return ControllerResult(self.mode, "blocked", ("pending_action_unavailable",))
        return pending

    def verify(self, window: RecoveryWindow, *, timeout_sec: float = 30) -> ControllerResult:
        """Check a pending step and journal the outcome before accepting further work."""
        if self.mode == "disabled":
            return ControllerResult(self.mode, "disabled")
        try:
            previous = self._journal.outcome(window.action_id)
            pending = self._journal.pending()
        except (OSError, sqlite3.Error, ValueError):
            return ControllerResult(self.mode, "blocked", ("journal_read_failed",))
        if previous is not None and not previous.intent.startswith("scale:"):
            return ControllerResult(self.mode, previous.state, (previous.reason,), previous.action_id)
        if pending is None or pending.action_id != window.action_id or pending.intent.startswith("scale:"):
            return ControllerResult(self.mode, "blocked", ("verification_action_mismatch",))
        reconciled = self._reconcile(pending)
        if reconciled.state == "verifying":
            state, reason = evaluate_recovery(
                window,
                original_weight=pending.before["region_a_percent"],
                target_weight=pending.expected["region_a_percent"],
                recorded_at=pending.recorded_at,
                now=time.time(),
                timeout_sec=timeout_sec,
            )
        else:
            state, reason = reconciled.state, reconciled.reasons[0]
        return self._record_verification(pending, state, reason)

    def _record_verification(self, pending: PendingAction, state: str, reason: str) -> ControllerResult:
        if state != "verifying" and self.mode == "local":
            try:
                if not self._journal.resolve(pending.action_id, state=state, reason=reason):
                    return ControllerResult(self.mode, "blocked", ("verification_outcome_conflict",), pending.action_id)
            except (OSError, sqlite3.Error, ValueError):
                return ControllerResult(self.mode, "blocked", ("verification_journal_failed",), pending.action_id)
        return ControllerResult(self.mode, state, (reason,), pending.action_id)

    def _reconcile(
        self, pending: PendingAction, reasons: tuple[str, ...] = (),
    ) -> ControllerResult:
        try:
            observed = self._route.read_weight()
        except OSError:
            observed = None
        outcome = reconcile_route(pending, observed_region_a_percent=observed)
        state = "verifying" if outcome == "target_observed" else "blocked"
        return ControllerResult(self.mode, state, (*reasons, outcome), pending.action_id)
