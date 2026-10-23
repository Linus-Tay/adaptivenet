"""Deterministic cause classifier over adaptivenet.model_input.v1.

The AdaptiveNet argument: combining the user-path (external: RIPE RTT / loss) and
in-region (internal: AMP app, CPU, memory, HTTP) views separates causes that one
view conflates.

  external bad, internal healthy  -> network_degradation  (gray failure: health checks pass)
  internal bad (any external)     -> compute_overload     (the region itself is slow / saturated)
  app down or errors from both    -> critical_failure
  a view missing or stale         -> unknown              (never acted on)

This is the rules baseline a trained model must beat (see ml/train.py). Labels
are never used as features. Thresholds are absolute defaults for the local lab;
calibrate RTT thresholds against each region's healthy RIPE baseline before
trusting them on real Atlas data (ADAPTIVENET_RTT_DEGRADED_MS).
"""
from __future__ import annotations
from dataclasses import dataclass
import os

REGIONS = ("region-a", "region-b")
CAUSES = ("healthy", "network_degradation", "compute_overload", "critical_failure", "unknown")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Thresholds:
    rtt_degraded_ms: float = _env_float("ADAPTIVENET_RTT_DEGRADED_MS", 800)
    packet_loss_degraded_pct: float = _env_float("ADAPTIVENET_LOSS_DEGRADED_PCT", 20)
    packet_loss_critical_pct: float = _env_float("ADAPTIVENET_LOSS_CRITICAL_PCT", 90)
    http_p95_degraded_ms: float = _env_float("ADAPTIVENET_P95_DEGRADED_MS", 800)
    error_ratio_degraded: float = _env_float("ADAPTIVENET_ERROR_DEGRADED", 0.20)
    error_ratio_critical: float = _env_float("ADAPTIVENET_ERROR_CRITICAL", 0.90)
    cpu_degraded_pct: float = _env_float("ADAPTIVENET_CPU_DEGRADED_PCT", 85)
    memory_low_pct: float = _env_float("ADAPTIVENET_MEMORY_LOW_PCT", 15)
    min_probes: int = 3


def external_view(f: dict, q: dict, t: Thresholds) -> tuple[str, list[str]]:
    """'healthy' | 'degraded' | 'critical' | 'unknown' from the user-path columns."""
    if q.get("external") not in ("fresh", "loss_only") or (f.get("ripe_probes_reporting") or 0) < t.min_probes:
        return "unknown", ["external_view_missing_or_stale"]
    loss, rtt = f.get("packet_loss_pct"), f.get("network_rtt_p50_ms")
    evidence = []
    if loss is not None and loss >= t.packet_loss_critical_pct:
        return "critical", [f"packet_loss_pct={loss}"]
    if loss is not None and loss >= t.packet_loss_degraded_pct:
        evidence.append(f"packet_loss_pct={loss}")
    if rtt is not None and rtt >= t.rtt_degraded_ms:
        evidence.append(f"network_rtt_p50_ms={rtt}")
    return ("degraded" if evidence else "healthy"), evidence


def internal_view(f: dict, q: dict, t: Thresholds) -> tuple[str, list[str]]:
    if q.get("internal") != "fresh":
        return "unknown", ["internal_view_missing_or_stale"]
    if f.get("app_up") != 1:
        return "critical", ["app_up!=1"]
    ratio, p95, cpu, mem = f.get("http_error_ratio"), f.get("http_p95_ms"), f.get("cpu_busy_pct"), f.get("memory_available_pct")
    if ratio is not None and ratio >= t.error_ratio_critical:
        return "critical", [f"http_error_ratio={ratio}"]
    evidence = []
    if ratio is not None and ratio >= t.error_ratio_degraded:
        evidence.append(f"http_error_ratio={ratio}")
    if p95 is not None and p95 >= t.http_p95_degraded_ms:
        evidence.append(f"http_p95_ms={p95}")
    if cpu is not None and cpu >= t.cpu_degraded_pct:
        evidence.append(f"cpu_busy_pct={cpu}")
    if mem is not None and mem < t.memory_low_pct:
        evidence.append(f"memory_available_pct={mem}")
    return ("degraded" if evidence else "healthy"), evidence


def classify_region(features: dict, quality: dict, t: Thresholds = Thresholds()) -> dict:
    ext, ext_ev = external_view(features, quality, t)
    inn, inn_ev = internal_view(features, quality, t)
    evidence = [f"external:{e}" for e in ext_ev] + [f"internal:{e}" for e in inn_ev]
    if inn == "critical":
        # A dead or error-saturated app is critical whatever the user path says: ICMP from
        # RIPE still answers when the host is up but the service is down.
        confidence = 0.9 if ext == "healthy" else 0.95
        return {"cause": "critical_failure", "confidence": confidence, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    if inn == "degraded" and ext == "unknown":
        # In-region saturation is visible without the user-path view (internal_only mode).
        return {"cause": "compute_overload", "confidence": 0.8, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    if inn == "unknown" or ext == "unknown":
        return {"cause": "unknown", "confidence": 0.0, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    if ext == "critical" and inn == "healthy":
        # Users cannot reach a region that is fine inside: a network failure, not compute.
        return {"cause": "network_degradation", "confidence": 0.95, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    if inn == "degraded":
        confidence = 0.85 if ext != "healthy" else 0.8
        return {"cause": "compute_overload", "confidence": confidence, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    if ext == "degraded":
        return {"cause": "network_degradation", "confidence": 0.9, "views": {"external": ext, "internal": inn}, "evidence": evidence}
    return {"cause": "healthy", "confidence": 0.9, "views": {"external": ext, "internal": inn}, "evidence": ["within_thresholds"]}


def classify(model_input: dict, t: Thresholds = Thresholds()) -> dict:
    return {region: classify_region(model_input["features"][region], model_input["quality"].get(region, {}), t)
            for region in REGIONS}
