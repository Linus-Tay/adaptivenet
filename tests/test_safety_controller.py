import sqlite3
from dataclasses import replace

import pytest

from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.recovery import HttpMeasurement, RecoveryWindow
from adaptivenet.controller.safety import SafetyController

NOW = 1000


class FakeGateway:
    weight = 100
    calls = 0
    failure = None

    def __init__(self, journal):
        self.journal = journal

    def read_weight(self):
        return self.weight

    def request_weight(self, *, original, target):
        self.calls += 1
        pending = self.journal.pending()
        assert pending.before == {"region_a_percent": original}
        assert pending.expected == {"region_a_percent": target}
        assert self.weight == original
        if self.failure == "external_change":
            self.weight = 95
            raise OSError("route changed")
        if self.failure == "before_update":
            raise TimeoutError()
        self.weight = target
        if self.failure == "after_update":
            raise TimeoutError()


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW)
    journal = ActionJournal(tmp_path / "controller.db")
    gateway = FakeGateway(journal)
    snapshot = {
        "snapshot_id": "snapshot",
        "computed_at_unix": NOW - 1,
        "ready_for_model": True,
        "regions": {
            "region-b": {
                "features": {"app_up": 1, "memory_available_pct": 60, "cpu_busy_pct": 30},
                "quality": {"internal": "fresh"},
            },
        },
    }
    proposal = {
        "contract_version": "adaptivenet.proposal.v1",
        "snapshot_id": "snapshot",
        "action": "shift_traffic",
        "confidence": 0.95,
        "target_region_a_percent": 75,
        "evidence": ["fixture"],
    }
    return journal, gateway, snapshot, proposal


def qualify(controller, snapshot, proposal):
    return [
        controller.step(snapshot, proposal, observed_at=at, source_degraded=True)
        for at in (NOW - 3, NOW - 3, NOW - 2, NOW - 1)
    ]


def test_local_shift_waits_for_three_distinct_samples_and_keeps_action_pending(scenario):
    journal, gateway, snapshot, proposal = scenario
    controller = SafetyController(journal, gateway, mode="local")
    *early, result = qualify(controller, snapshot, proposal)
    assert [step.qualifying_count for step in early] == [1, 1, 2]
    assert result.state == "verifying"
    assert result.reasons == ("target_observed",)
    assert result.action_id == journal.pending().action_id
    assert (gateway.weight, gateway.calls) == (75, 1)


@pytest.mark.parametrize(("mode", "state"), [
    ("disabled", "disabled"),
])
def test_modes_without_execution_never_reserve_or_write_route(scenario, mode, state):
    journal, gateway, snapshot, proposal = scenario
    controller = SafetyController(journal, gateway, mode=mode)
    assert qualify(controller, snapshot, proposal)[-1].state == state
    assert journal.pending() is None
    assert (gateway.weight, gateway.calls) == (100, 0)


def test_unsafe_destination_blocks_local_shift(scenario):
    journal, gateway, snapshot, proposal = scenario
    snapshot["regions"]["region-b"]["features"]["cpu_busy_pct"] = 90
    controller = SafetyController(journal, gateway, mode="local")
    result = qualify(controller, snapshot, proposal)[-1]
    assert result.reasons == ("destination_high_or_unknown_cpu",)
    assert journal.pending() is None
    assert gateway.calls == 0


def test_stale_future_or_healthy_samples_do_not_qualify(scenario):
    journal, gateway, snapshot, proposal = scenario
    controller = SafetyController(journal, gateway, mode="local")
    for at, degraded in [(NOW - 46, True), (NOW + 100, True), (NOW - 5, False)]:
        result = controller.step(snapshot, proposal, observed_at=at, source_degraded=degraded)
        assert result.reasons == ("degradation_evidence_missing_or_stale",)
    assert qualify(controller, snapshot, proposal)[-1].state == "verifying"


@pytest.mark.parametrize(("failure", "outcome", "state"), [
    ("before_update", "original_observed", "blocked"),
    ("after_update", "target_observed", "verifying"),
    ("external_change", "unexpected_route", "blocked"),
])
def test_restart_reads_pending_outcome_without_resending(scenario, failure, outcome, state):
    journal, gateway, snapshot, proposal = scenario
    gateway.failure = failure
    result = qualify(SafetyController(journal, gateway, mode="local"), snapshot, proposal)[-1]
    assert result.reasons == ("route_request_failed", outcome)
    restarted = SafetyController(ActionJournal(journal.path), gateway, mode="local")
    resumed = restarted.step(snapshot, proposal, observed_at=None, source_degraded=False)
    assert (resumed.action_id, resumed.state, resumed.reasons) == (result.action_id, state, (outcome,))
    assert gateway.calls == 1


def test_journal_write_failure_blocks_route_change(scenario, monkeypatch):
    journal, gateway, snapshot, proposal = scenario

    def fail_reservation(**_kwargs):
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(journal, "record_pending", fail_reservation)
    controller = SafetyController(journal, gateway, mode="local")
    result = qualify(controller, snapshot, proposal)[-1]
    assert result.reasons == ("journal_reservation_failed",)
    assert gateway.calls == 0


@pytest.fixture
def verification(scenario, monkeypatch):
    journal, gateway, snapshot, proposal = scenario
    controller = SafetyController(journal, gateway, mode="local")
    action = qualify(controller, snapshot, proposal)[-1]
    start = NOW + 1
    requests = tuple(
        HttpMeasurement(str(index), start + index, start + index + 0.2, "region-b", 200)
        for index in range(11)
    )
    probe = HttpMeasurement("probe", start + 10, start + 10.1, "region-b", 200)
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: start + 11)
    return controller, gateway, RecoveryWindow(action.action_id, start, requests, probe)


def test_full_client_window_records_recovery_and_enforces_cooldown(verification, scenario):
    controller, gateway, window = verification
    result = controller.verify(window)
    assert (result.state, result.reasons) == ("recovered", ("client_window_met_deadline",))
    assert gateway.journal.pending() is None
    assert gateway.journal.outcome(result.action_id).state == "recovered"
    journal, _, snapshot, proposal = scenario
    restarted = SafetyController(ActionJournal(journal.path), gateway, mode="local")
    assert restarted.step(snapshot, proposal, observed_at=None, source_degraded=False).state == "cooldown"
    assert restarted.verify(window) == result
    assert gateway.calls == 1


@pytest.mark.parametrize(("region", "status", "latency", "state", "reason"), [
    ("region-a", 200, 0.9, "continue_candidate", "source_still_missing_deadline"),
    ("region-b", 200, 0.65, "blocked", "destination_client_deadline_missed"),
    ("region-b", 503, 0.2, "blocked", "destination_client_deadline_missed"),
    (None, None, 0.9, "blocked", "unexpected_client_serving_region"),
])
def test_verification_distinguishes_source_misses_from_destination_failure(
    verification, region, status, latency, state, reason,
):
    controller, gateway, window = verification
    first, *rest = window.requests
    sample = replace(first, region=region, status_code=status, completed_at=first.started_at + latency)
    result = controller.verify(replace(window, requests=(sample, *rest)))
    assert (result.state, result.reasons) == (state, (reason,))
    assert gateway.calls == 1


def test_insufficient_client_samples_do_not_prove_recovery(verification):
    controller, _, window = verification
    for incomplete in (
        replace(window, requests=window.requests[:5]),
        replace(window, requests=window.requests[:10]),
        replace(window, destination_probe=None),
    ):
        assert controller.verify(incomplete).state == "verifying"


def test_failed_destination_probe_blocks_even_before_window_is_complete(verification):
    controller, _, window = verification
    failed_probe = replace(window.destination_probe, status_code=503)
    result = controller.verify(replace(window, requests=(), destination_probe=failed_probe))
    assert (result.state, result.reasons) == ("blocked", ("destination_probe_failed",))


def test_duplicate_http_samples_do_not_count(verification):
    controller, _, window = verification
    result = controller.verify(replace(window, requests=(*window.requests, window.requests[0])))
    assert (result.state, result.reasons) == ("blocked", ("duplicate_client_measurement",))


def test_verification_timeout_remains_blocked_after_restart(verification, scenario, monkeypatch):
    controller, gateway, window = verification
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW + 31)
    assert controller.verify(window).reasons == ("verification_timeout",)
    journal, _, snapshot, proposal = scenario
    restarted = SafetyController(ActionJournal(journal.path), gateway, mode="local")
    result = restarted.step(snapshot, proposal, observed_at=None, source_degraded=False)
    assert (result.state, result.reasons) == ("blocked", ("verification_timeout",))
    assert gateway.journal.pending().action_id == window.action_id
    assert gateway.calls == 1


def test_external_route_change_stops_verification(verification):
    controller, gateway, window = verification
    gateway.weight = 60
    result = controller.verify(window)
    assert (result.state, result.reasons) == ("blocked", ("unexpected_route",))
    assert gateway.calls == 1


@pytest.fixture
def continuation(verification, scenario):
    controller, gateway, window = verification
    first, *rest = window.requests
    miss = replace(first, region="region-a", completed_at=first.started_at + 0.9)
    outcome = controller.verify(replace(window, requests=(miss, *rest)))
    assert outcome.state == "continue_candidate"
    journal, _, snapshot, proposal = scenario
    assert journal.pending() is None
    restarted = SafetyController(ActionJournal(journal.path), gateway, mode="local")
    return restarted, gateway, snapshot, {**proposal, "target_region_a_percent": 50}


def test_next_step_needs_three_new_observations_and_fresh_destination_probes(continuation, monkeypatch):
    controller, gateway, snapshot, proposal = continuation
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW + 18)
    for at, responsive, reason in (
        (NOW + 10, True, "degradation_evidence_missing_or_stale"),
        (NOW + 13, False, "fresh_destination_probe_required"),
    ):
        result = controller.step(
            snapshot, proposal, observed_at=at, source_degraded=True, destination_responsive=responsive,
        )
        assert result.reasons == (reason,)
    results = [
        controller.step(
            snapshot, proposal, observed_at=at, source_degraded=True, destination_responsive=True,
        )
        for at in (NOW + 14, NOW + 14, NOW + 15, NOW + 16)
    ]
    assert [result.qualifying_count for result in results[:3]] == [1, 1, 2]
    assert results[-1].state == "verifying"
    assert (gateway.weight, gateway.calls) == (50, 2)


@pytest.mark.parametrize(("weight", "target", "reason"), [
    (70, 50, "route_changed_since_verification"),
    (75, 90, "continuation_reverses_direction"),
    (75, 40, "route_delta_above_25_percent_safety_limit"),
])
def test_unsafe_continuations_are_rejected(continuation, monkeypatch, weight, target, reason):
    controller, gateway, snapshot, proposal = continuation
    gateway.weight = weight
    snapshot["regions"]["region-a"] = snapshot["regions"]["region-b"]
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW + 18)
    result = controller.step(
        snapshot, {**proposal, "target_region_a_percent": target},
        observed_at=NOW + 16, source_degraded=True, destination_responsive=True,
    )
    assert result.reasons == (reason,)
    assert gateway.calls == 1


def test_verification_write_failure_leaves_action_pending(verification, monkeypatch):
    controller, gateway, window = verification

    def fail_resolution(*_args, **_kwargs):
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(gateway.journal, "resolve", fail_resolution)
    result = controller.verify(window)
    assert (result.state, result.reasons) == ("blocked", ("verification_journal_failed",))
    assert gateway.journal.pending().action_id == window.action_id


def test_controller_requires_explicit_mode_and_rejects_removed_shadow_mode(scenario):
    journal, gateway, _, _ = scenario
    with pytest.raises(TypeError, match="mode"):
        SafetyController(journal, gateway)
    with pytest.raises(ValueError, match="invalid controller mode"):
        SafetyController(journal, gateway, mode="shadow")
    assert journal.pending() is None
    assert gateway.calls == 0


def test_blocked_step_retries_on_its_own_after_the_wait(verification, scenario, monkeypatch):
    from adaptivenet.controller.safety import BLOCK_RETRY_SEC
    controller, gateway, window = verification
    monkeypatch.setattr("adaptivenet.controller.journal.time.time", lambda: NOW + 12)
    gateway.weight = 60
    assert controller.verify(window).state == "blocked"
    journal, _, snapshot, proposal = scenario
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: NOW + 12 + BLOCK_RETRY_SEC - 1)
    assert controller.step(snapshot, proposal, observed_at=None, source_degraded=False).state == "blocked"
    later = NOW + 12 + BLOCK_RETRY_SEC + 1
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: later)
    result = controller.step(snapshot, proposal, observed_at=later - 1, source_degraded=True)
    assert result.state in ("observing", "qualifying")
    assert journal.pending() is None


def test_stuck_pending_action_is_journaled_then_released(scenario, monkeypatch):
    from adaptivenet.controller.safety import BLOCK_RETRY_SEC
    journal, gateway, snapshot, proposal = scenario
    controller = SafetyController(journal, gateway, mode="local")
    monkeypatch.setattr("adaptivenet.controller.journal.time.time", lambda: NOW)
    gateway.failure = "before_update"
    assert qualify(controller, snapshot, proposal)[-1].state == "blocked"
    t = NOW + BLOCK_RETRY_SEC + 1
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: t)
    monkeypatch.setattr("adaptivenet.controller.journal.time.time", lambda: t)
    assert controller.step(snapshot, proposal, observed_at=None, source_degraded=False).state == "blocked"
    assert journal.outcome().state == "blocked"
    monkeypatch.setattr("adaptivenet.controller.safety.time.time", lambda: t + BLOCK_RETRY_SEC + 1)
    assert controller.step(snapshot, proposal, observed_at=None, source_degraded=False).state == "observing"
    assert journal.pending() is None
