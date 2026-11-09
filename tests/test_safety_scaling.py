from dataclasses import replace
import math
import sqlite3

import pytest

from adaptivenet.controller.emulated_hpa import EmulatedHpa
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.recovery import ClientWindow, HttpMeasurement, RecoveryWindow
from adaptivenet.controller.safety import SafetyController
from adaptivenet.controller.scaling import (
    CapacityRecommendation,
    HpaObservation,
    WorkloadTarget,
    recommend_capacity,
)
from adaptivenet.ingestion.decisions import review_proposal

NOW = 1000
TARGET = WorkloadTarget("fixture-sg", "adaptivenet", "catalog", "catalog")
ALLOWLIST = {"region-a": TARGET}
HEALTHY = HpaObservation(
    target=TARGET, observed_at=NOW - 2, metrics_observed_at=NOW - 10,
    min_replicas=2, max_replicas=6, current=2, desired=2, ready=2, available=2,
    pending=0, able_to_scale=True, scaling_active=True, scaling_limited=False,
)


def test_scale_recommendation_adds_one_within_observed_limits():
    assert recommend_capacity(
        "region-a", allowlist=ALLOWLIST, observation=HEALTHY, now=NOW,
    ) == CapacityRecommendation("recommend", "one_replica_within_hpa_bounds", 3)
    almost_full = replace(HEALTHY, current=5, desired=5, ready=5, available=5)
    assert recommend_capacity(
        "region-a", allowlist=ALLOWLIST, observation=almost_full, now=NOW,
    ).requested_replicas == 6


@pytest.mark.parametrize(("changes", "state", "reason"), [
    ({"desired": 6, "pending": 1, "scaling_limited": True},
     "already_in_progress", "hpa_already_requested_capacity"),
    ({"current": 6, "desired": 6, "ready": 6, "available": 6},
     "blocked", "hpa_at_max_replicas"),
    ({"current": 3, "desired": 3, "pending": 1},
     "wait", "replicas_not_yet_available"),
    ({"available": 1}, "wait", "replicas_not_yet_available"),
    ({"current": 3}, "wait", "hpa_scale_down_in_progress"),
    ({"observed_at": NOW - 61}, "blocked", "stale_or_invalid_hpa_evidence"),
    ({"metrics_observed_at": NOW - 61}, "blocked", "stale_or_invalid_hpa_evidence"),
    ({"metrics_observed_at": NOW + 1}, "blocked", "stale_or_invalid_hpa_evidence"),
    ({"metrics_observed_at": math.nan}, "blocked", "stale_or_invalid_hpa_evidence"),
    ({"max_replicas": 7}, "blocked", "invalid_hpa_replica_state"),
    ({"available": 3}, "blocked", "invalid_hpa_replica_state"),
    ({"scaling_active": False}, "blocked", "hpa_unavailable_or_inactive"),
    ({"scaling_limited": None}, "blocked", "hpa_scaling_limited_or_unknown"),
    ({"target": replace(TARGET, namespace="other")}, "blocked", "hpa_target_mismatch"),
])
def test_unsafe_or_already_changing_hpa_never_recommends_more(changes, state, reason):
    assert recommend_capacity(
        "region-a", allowlist=ALLOWLIST, observation=replace(HEALTHY, **changes), now=NOW,
    ) == CapacityRecommendation(state, reason)


def test_recommendation_requires_a_known_region_and_observation():
    for region in (None, "region-b", ["region-a"]):
        assert recommend_capacity(
            region, allowlist=ALLOWLIST, observation=HEALTHY, now=NOW,
        ).reason == "unknown_affected_region"
    assert recommend_capacity(
        "region-a", allowlist=ALLOWLIST, observation=None, now=NOW,
    ).reason == "missing_hpa_observation"


def test_emulated_hpa_distinguishes_requested_pending_and_ready_capacity():
    emulator = EmulatedHpa("region-a", TARGET, HEALTHY)
    recommendation = recommend_capacity(
        "region-a", allowlist=ALLOWLIST, observation=emulator.observe(), now=NOW,
    )
    assert recommendation.requested_replicas == 3
    request = emulator.request_capacity("action-1", replicas=recommendation.requested_replicas, now=NOW)
    assert request.state == "requested"
    assert emulator.observe() == replace(HEALTHY, desired=3, observed_at=NOW)
    assert emulator.request_capacity("action-1", replicas=3, now=NOW) == request

    pending = emulator.advance(now=NOW + 5, pod_event="pending")
    assert pending.state == "pending"
    observed = emulator.observe()
    assert (observed.current, observed.desired, observed.ready, observed.available, observed.pending) == (3, 3, 2, 2, 1)
    with pytest.raises(ValueError, match="previous scaling action is unresolved"):
        emulator.request_capacity("action-2", replicas=4, now=NOW + 6)

    ready = emulator.advance(now=NOW + 10, pod_event="ready")
    assert ready.state == "ready"
    assert emulator.observe() == replace(
        HEALTHY, current=3, desired=3, ready=3, available=3, observed_at=NOW + 10,
    )
    assert emulator.request_capacity("action-1", replicas=3, now=NOW + 11) == ready
    with pytest.raises(ValueError, match="different replica target"):
        emulator.request_capacity("action-1", replicas=4, now=NOW + 11)


@pytest.mark.parametrize(("elapsed", "event", "outcome"), [
    (10, "failed", "failed"),
    (30, None, "timed_out"),
    (30, "ready", "timed_out"),
])
def test_emulated_failure_and_timeout_freeze_capacity(elapsed, event, outcome):
    emulator = EmulatedHpa("region-a", TARGET, HEALTHY)
    emulator.request_capacity("action-1", replicas=3, now=NOW)
    emulator.advance(now=NOW + 5, pod_event="pending")
    terminal = emulator.advance(now=NOW + elapsed, pod_event=event)
    assert terminal.state == outcome
    assert emulator.observe().ready == 2
    assert emulator.advance(now=NOW + 31, pod_event="ready") == terminal
    with pytest.raises(ValueError, match="previous scaling action is unresolved"):
        emulator.request_capacity("action-2", replicas=4, now=NOW + 31)


@pytest.mark.parametrize(("initial", "replicas", "reason"), [
    (HEALTHY, 4, "does not match current recommendation"),
    (replace(HEALTHY, desired=3), 3, "hpa_already_requested_capacity"),
    (replace(HEALTHY, max_replicas=2), 3, "hpa_at_max_replicas"),
    (replace(HEALTHY, metrics_observed_at=NOW - 61), 3, "stale_or_invalid_hpa_evidence"),
])
def test_emulator_rechecks_capacity_before_accepting_a_request(initial, replicas, reason):
    emulator = EmulatedHpa("region-a", TARGET, initial)
    with pytest.raises(ValueError, match=reason):
        emulator.request_capacity("action-1", replicas=replicas, now=NOW)
    assert emulator.observe() == initial


def test_polling_emulator_does_not_refresh_metrics_or_allow_reordered_events():
    emulator = EmulatedHpa("region-a", TARGET, HEALTHY)
    requested = emulator.request_capacity("action-1", replicas=3, now=NOW)
    assert emulator.advance(now=NOW + 10) == requested
    assert emulator.observe().metrics_observed_at == HEALTHY.metrics_observed_at
    with pytest.raises(ValueError, match="nondecreasing"):
        emulator.advance(now=NOW + 9, pod_event="ready")


class UnusedRoute:
    def read_weight(self):
        pytest.fail("scaling must not read the route")


@pytest.fixture
def controller_inputs(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: clock[0])
    journal = ActionJournal(tmp_path / "controller.db")
    hpa = EmulatedHpa("region-a", TARGET, HEALTHY)
    snapshot = {"snapshot_id": "fixture", "computed_at_unix": NOW, "ready_for_model": True}
    proposal = {
        "contract_version": "adaptivenet.proposal.v1", "snapshot_id": "fixture",
        "action": "scale_out", "affected_region": "region-a", "confidence": 0.9,
        "evidence": ["fixture_cpu_overload"],
    }
    return clock, journal, hpa, snapshot, proposal


def qualify_scale(controller, inputs):
    clock, _, hpa, snapshot, proposal = inputs
    results = []
    for offset in (1, 1, 2, 3):
        clock[0] = NOW + offset
        hpa.observe_metrics(observed_at=clock[0])
        results.append(controller.step_scale(snapshot, proposal, compute_overloaded=True))
    return results


def test_scale_proposal_is_journaled_before_emulator_changes_desired_capacity(controller_inputs, monkeypatch):
    clock, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    request_capacity = hpa.request_capacity

    def check_journal(action_id, *, replicas, now):
        pending = journal.pending()
        assert pending.action_id == action_id
        assert pending.before["desired"] == 2
        assert pending.expected["replicas"] == replicas == 3
        return request_capacity(action_id, replicas=replicas, now=now)

    monkeypatch.setattr(hpa, "request_capacity", check_journal)
    *early, requested = qualify_scale(controller, controller_inputs)
    assert [result.qualifying_count for result in early] == [1, 1, 2]
    assert requested.state == "action_pending"
    assert hpa.observe().desired == 3
    route_step = controller.step(snapshot, proposal, observed_at=None, source_degraded=False)
    assert route_step.reasons == ("scale_action_pending",)

    clock[0] = NOW + 5
    hpa.advance(now=clock[0], pod_event="pending")
    restarted = SafetyController(ActionJournal(journal.path), UnusedRoute(), mode="local", emulated_hpa=hpa)
    assert restarted.step_scale(snapshot, proposal, compute_overloaded=True).reasons == ("emulated_scale_pending",)
    clock[0] = NOW + 8
    hpa.advance(now=clock[0], pod_event="ready")
    result = restarted.step_scale(snapshot, proposal, compute_overloaded=True)
    assert (result.state, result.action_id) == ("verifying", requested.action_id)
    assert result.reasons == ("emulated_replicas_ready_client_check_required",)
    assert journal.pending().action_id == requested.action_id
    assert journal.outcome(requested.action_id) is None


def test_scaling_shadow_mode_qualifies_without_mutating_hpa_or_journal(controller_inputs):
    _, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), emulated_hpa=hpa)
    assert qualify_scale(controller, controller_inputs)[-1].state == "shadow_qualified"
    assert journal.pending() is None
    assert hpa.observe().desired == 2
    shadow_review = review_proposal(snapshot, proposal)
    assert shadow_review["executed"] is False
    assert shadow_review["rejection_reasons"] == ["kubernetes_scale_adapter_not_implemented"]


@pytest.mark.parametrize(("changes", "reason"), [
    ({"confidence": 0.7}, "insufficient_confidence"),
    ({"snapshot_id": "other"}, "snapshot_id_mismatch"),
    ({"affected_region": None}, "unknown_affected_region"),
    ({"evidence": []}, "missing_model_evidence"),
    ({"evidence": [" "]}, "missing_model_evidence"),
])
def test_scaling_rejects_unsafe_proposals(controller_inputs, changes, reason):
    _, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    result = controller.step_scale(snapshot, {**proposal, **changes}, compute_overloaded=True)
    assert result.reasons == (reason,)
    assert journal.pending() is None
    assert hpa.observe().desired == 2


def test_failed_scaling_reservation_does_not_mutate_hpa(controller_inputs, monkeypatch):
    _, journal, hpa, _, _ = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)

    def fail_write(**_kwargs):
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(journal, "record_pending", fail_write)
    assert qualify_scale(controller, controller_inputs)[-1].reasons == ("journal_reservation_failed",)
    assert hpa.observe().desired == 2


@pytest.mark.parametrize(("snapshot_changes", "overloaded", "reason"), [
    ({"computed_at_unix": NOW - 181}, True, "feature_snapshot_expired_or_invalid"),
    ({"ready_for_model": "true"}, True, "telemetry_quality_gate_failed"),
    ({}, False, "compute_evidence_missing_or_stale"),
])
def test_scale_proposal_alone_cannot_qualify(controller_inputs, snapshot_changes, overloaded, reason):
    _, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    result = controller.step_scale({**snapshot, **snapshot_changes}, proposal, compute_overloaded=overloaded)
    assert result.reasons == (reason,)
    assert journal.pending() is None


@pytest.mark.parametrize(("failure", "reason"), [
    ("timeout", "emulated_scale_timed_out"),
    ("failed", "emulated_scale_failed"),
    ("emulator_lost", "emulated_scale_outcome_unknown"),
])
def test_unresolved_scaling_blocks_across_restart(controller_inputs, failure, reason):
    clock, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    requested = qualify_scale(controller, controller_inputs)[-1]
    if failure == "failed":
        hpa.advance(now=NOW + 6, pod_event="failed")
    elif failure == "emulator_lost":
        hpa = EmulatedHpa("region-a", TARGET, HEALTHY)
    clock[0] = NOW + 33
    restarted = SafetyController(ActionJournal(journal.path), UnusedRoute(), mode="local", emulated_hpa=hpa)
    result = restarted.step_scale(snapshot, proposal, compute_overloaded=True)
    assert (result.state, result.reasons, result.action_id) == ("blocked", (reason,), requested.action_id)
    assert journal.outcome(requested.action_id).reason == reason
    assert restarted.step_scale(snapshot, proposal, compute_overloaded=True) == result


def test_pending_route_shift_prevents_scaling(controller_inputs):
    _, journal, hpa, snapshot, proposal = controller_inputs
    assert journal.record_pending(
        action_id="route-1", intent="shift:100->75",
        before={"region_a_percent": 100}, expected={"region_a_percent": 75},
    )
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    result = controller.step_scale(snapshot, proposal, compute_overloaded=True)
    assert (result.state, result.reasons) == ("blocked", ("action_pending",))
    assert hpa.observe().desired == 2


@pytest.fixture
def scale_verification(controller_inputs):
    clock, journal, hpa, snapshot, proposal = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    request = qualify_scale(controller, controller_inputs)[-1]
    ready_at = NOW + 8
    clock[0] = ready_at
    ready = hpa.advance(now=ready_at, pod_event="ready")
    assert ready.ready_at == ready_at
    clock[0] = ready_at + 12
    start = ready_at + 1
    requests = tuple(
        HttpMeasurement(str(index), start + index, start + index + 0.2, "region-a", 200)
        for index in range(11)
    )
    return controller, ClientWindow(request.action_id, start, requests)


def test_post_readiness_client_window_closes_scaling_action_and_starts_cooldown(
    scale_verification, controller_inputs,
):
    controller, window = scale_verification
    _, journal, hpa, snapshot, proposal = controller_inputs
    result = controller.verify_scale(window)
    assert (result.state, result.reasons) == ("recovered", ("emulated_scale_client_window_met_deadline",))
    assert journal.pending() is None
    assert journal.outcome(window.action_id).state == "recovered"
    restarted = SafetyController(ActionJournal(journal.path), UnusedRoute(), mode="local", emulated_hpa=hpa)
    assert restarted.verify_scale(window) == result
    assert restarted.step_scale(snapshot, proposal, compute_overloaded=True).state == "cooldown"
    assert restarted.verify(RecoveryWindow(window.action_id, window.started_at, (), None)).reasons == (
        "verification_action_mismatch",
    )


def test_ready_replicas_and_short_client_window_keep_scaling_pending(scale_verification, controller_inputs):
    controller, window = scale_verification
    _, journal, _, _, _ = controller_inputs
    result = controller.verify_scale(replace(window, requests=window.requests[:10]))
    assert (result.state, result.reasons) == ("verifying", ("insufficient_scale_recovery_evidence",))
    assert journal.pending().action_id == window.action_id
    assert journal.outcome(window.action_id) is None


@pytest.mark.parametrize(("change", "reason"), [
    ("slow", "scale_client_deadline_missed"),
    ("http_error", "scale_client_deadline_missed"),
    ("wrong_region", "unexpected_client_serving_region"),
    ("before_ready", "invalid_scale_verification_start"),
    ("duplicate", "duplicate_client_measurement"),
])
def test_bad_client_evidence_blocks_scaling_and_survives_restart(
    scale_verification, controller_inputs, change, reason,
):
    controller, window = scale_verification
    _, journal, hpa, snapshot, proposal = controller_inputs
    first, *rest = window.requests
    if change == "slow":
        window = replace(window, requests=(replace(first, completed_at=first.started_at + 0.65), *rest))
    elif change == "http_error":
        window = replace(window, requests=(replace(first, status_code=503), *rest))
    elif change == "wrong_region":
        window = replace(window, requests=(replace(first, region="region-b"), *rest))
    elif change == "before_ready":
        window = replace(window, started_at=NOW + 7)
    else:
        window = replace(window, requests=(first, first, *rest))
    result = controller.verify_scale(window)
    assert (result.state, result.reasons) == ("blocked", (reason,))
    assert journal.pending().action_id == window.action_id
    restarted = SafetyController(ActionJournal(journal.path), UnusedRoute(), mode="local", emulated_hpa=hpa)
    assert restarted.step_scale(snapshot, proposal, compute_overloaded=True) == result


def test_scale_verification_times_out_without_client_data(scale_verification, controller_inputs):
    controller, window = scale_verification
    clock, journal, _, snapshot, proposal = controller_inputs
    clock[0] = NOW + 39
    result = controller.step_scale(snapshot, proposal, compute_overloaded=True)
    assert (result.state, result.reasons) == ("blocked", ("scale_client_verification_timeout",))
    assert journal.outcome(window.action_id).reason == "scale_client_verification_timeout"


def test_scale_client_verification_still_needs_ready_replicas(controller_inputs):
    _, journal, hpa, _, _ = controller_inputs
    controller = SafetyController(journal, UnusedRoute(), mode="local", emulated_hpa=hpa)
    requested = qualify_scale(controller, controller_inputs)[-1]
    result = controller.verify_scale(ClientWindow(requested.action_id, NOW + 3, ()))
    assert (result.state, result.reasons) == ("action_pending", ("emulated_scale_requested",))
    assert journal.outcome(requested.action_id) is None
