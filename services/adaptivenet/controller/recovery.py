"""Interpret independent client HTTP measurements after a local routing step."""
from __future__ import annotations

from dataclasses import dataclass
from math import inf

from adaptivenet.ingestion.fast_signals import MAX_FRESH_AGE_SEC

MIN_WINDOW_SEC = 10
MIN_REQUESTS = 10
DEADLINE_SEC = 0.5
SCALE_CLIENT_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class HttpMeasurement:
    request_id: str
    started_at: float
    completed_at: float
    region: str | None
    status_code: int | None

    def met_deadline(self) -> bool:
        return (
            type(self.status_code) is int
            and 200 <= self.status_code < 300
            and self.completed_at <= self.started_at + DEADLINE_SEC
        )


@dataclass(frozen=True)
class ClientWindow:
    action_id: str
    started_at: float
    requests: tuple[HttpMeasurement, ...]


@dataclass(frozen=True)
class RecoveryWindow(ClientWindow):
    """Start after reading back the target route, then collect independent requests."""

    destination_probe: HttpMeasurement | None


def _measurement_in_window(sample: HttpMeasurement, start: float, now: float) -> bool:
    return (
        bool(sample.request_id)
        and type(sample.started_at) in (int, float)
        and type(sample.completed_at) in (int, float)
        and start <= sample.started_at <= sample.completed_at <= now
    )


def _has_sample_coverage(requests: tuple[HttpMeasurement, ...]) -> bool:
    return (
        len(requests) >= MIN_REQUESTS
        and max(sample.completed_at for sample in requests)
        - min(sample.started_at for sample in requests) >= MIN_WINDOW_SEC
    )


def evaluate_recovery(
    window: RecoveryWindow, *, original_weight: int, target_weight: int,
    recorded_at: float, now: float, timeout_sec: float = 30,
) -> tuple[str, str]:
    """Return a state and reason. A further shift still needs fresh policy checks."""
    if not MIN_WINDOW_SEC <= timeout_sec < inf:
        raise ValueError("verification timeout must allow the minimum sample window")
    if type(window.started_at) not in (int, float) or not recorded_at <= window.started_at <= now:
        return "blocked", "invalid_verification_start"
    if now > recorded_at + timeout_sec:
        return "blocked", "verification_timeout"

    source, destination = (
        ("region-a", "region-b") if target_weight < original_weight else ("region-b", "region-a")
    )
    source_weight = target_weight if source == "region-a" else 100 - target_weight
    probe = window.destination_probe
    if probe is not None and (
        not _measurement_in_window(probe, window.started_at, now)
        or probe.region != destination
        or not probe.met_deadline()
    ):
        return "blocked", "destination_probe_failed"

    request_ids = set()
    destination_requests = 0
    source_misses = 0
    for sample in window.requests:
        if not _measurement_in_window(sample, window.started_at, now):
            return "blocked", "invalid_client_measurement"
        if sample.request_id in request_ids:
            return "blocked", "duplicate_client_measurement"
        request_ids.add(sample.request_id)
        if sample.region == destination:
            if not sample.met_deadline():
                return "blocked", "destination_client_deadline_missed"
            destination_requests += 1
        elif sample.region != source or source_weight == 0:
            return "blocked", "unexpected_client_serving_region"
        elif not sample.met_deadline():
            source_misses += 1

    if (
        not _has_sample_coverage(window.requests)
        or not destination_requests
        or probe is None
        or now - probe.completed_at > MAX_FRESH_AGE_SEC
    ):
        return "verifying", "insufficient_recovery_evidence"
    if source_misses:
        return "continue_candidate", "source_still_missing_deadline"
    return "recovered", "client_window_met_deadline"


def evaluate_scale_recovery(
    window: ClientWindow, *, region: str, ready_at: float, now: float,
) -> tuple[str, str]:
    """Check the synthetic client stream after the emulator reports ready capacity."""
    if type(window.started_at) not in (int, float) or not ready_at <= window.started_at <= now:
        return "blocked", "invalid_scale_verification_start"
    request_ids = set()
    for sample in window.requests:
        if not _measurement_in_window(sample, window.started_at, now):
            return "blocked", "invalid_client_measurement"
        if sample.request_id in request_ids:
            return "blocked", "duplicate_client_measurement"
        request_ids.add(sample.request_id)
        if sample.region != region:
            return "blocked", "unexpected_client_serving_region"
        if not sample.met_deadline():
            return "blocked", "scale_client_deadline_missed"
    if not _has_sample_coverage(window.requests):
        return "verifying", "insufficient_scale_recovery_evidence"
    return "recovered", "emulated_scale_client_window_met_deadline"
