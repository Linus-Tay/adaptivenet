"""Read-only, timestamp-aware fast HTTP witness features for SHADOW ML research.

This evidence is from the collector's own network, NOT multiple independent ASNs.
Never infer a global outage or automatically actuate from this single-vantage feed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import statistics
import time

from .witness import recent_probes

VERSION = "adaptivenet.fast_signals.v1"
SCOPES = ("cloudfront", "region-a", "region-b")
WINDOWS = (60, 300, 900)
MAX_FRESH_AGE_SEC = 45


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * .95
    lower = math.floor(index)
    upper = math.ceil(index)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 2)


def build_fast_signals(events: list[dict], *, now: float | None = None) -> dict:
    """Build 1/5/15-minute *observation* windows, never deduce duration from polls."""
    clock = time.time() if now is None else float(now)
    samples: dict[str, list[tuple[float, bool, float | None]]] = {s: [] for s in SCOPES}
    for event in events:
        if not isinstance(event, dict):
            continue
        scope = event.get("scope")
        stamp = _timestamp(event.get("timestamp"))
        if scope not in samples or stamp is None or not 0 <= clock - stamp <= max(WINDOWS):
            continue
        outcome, code = event.get("outcome"), event.get("http_status")
        valid = outcome in ("ok", "http_error", "network_error", "invalid_response", "unexpected_region")
        if not valid:
            continue
        success = outcome == "ok" and type(code) is int and 200 <= code < 300
        latency = event.get("latency_ms")
        if type(latency) not in (int, float) or not math.isfinite(latency) or not 0 <= latency <= 60000:
            latency = None
        samples[scope].append((stamp, success, latency if success else None))

    scopes = {}
    for scope, measurements in samples.items():
        measurements.sort(key=lambda row: row[0])
        latest = measurements[-1][0] if measurements else None
        age = round(clock - latest, 2) if latest is not None else None
        windows = {}
        for seconds in WINDOWS:
            selected = [r for r in measurements if clock - r[0] <= seconds]
            successes = sum(r[1] for r in selected)
            failures = len(selected) - successes
            response_times = [r[2] for r in selected if r[2] is not None]
            windows[f"{seconds}s"] = {
                "count": len(selected),
                "success_count": successes,
                "failure_count": failures,
                "failure_ratio": round(failures / len(selected), 4) if selected else None,
                "http_latency_p95_ms": _p95(response_times),
            }
        consecutive_failures = 0
        for _, success, _ in reversed(measurements):
            if success:
                break
            consecutive_failures += 1
        scopes[scope] = {
            "last_observed_at_unix": latest,
            "age_seconds": age,
            "fresh": age is not None and age <= MAX_FRESH_AGE_SEC,
            "consecutive_failed_requests": consecutive_failures,
            "windows": windows,
        }
    return {
        "contract_version": VERSION,
        "generated_at_unix": clock,
        "vantage_type": "collector_network",
        "independent_vantage_count": 1,
        "limitation": "single_network_vantage_not_proof_of_regional_or_widespread_outage",
        "ready_for_fast_assessment": all(scopes[s]["fresh"] for s in SCOPES),
        "scopes": scopes,
    }


def load_fast_signals(path, *, now: float | None = None) -> dict:
    # Bound file reads by the existing witness journal rotation limit.
    return build_fast_signals(recent_probes(path, limit=1000), now=now)
