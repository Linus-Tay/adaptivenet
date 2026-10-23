"""Build an adaptivenet.features.v1 snapshot from the local lab's HTTP probe views.

The AWS path builds snapshots from AMP + RIPE Atlas + CloudFront. The local lab
has no AWS, so the same columns are filled from measured HTTP probes and the
workload's /internal/stats; provenance says so (source: local_http_*). Nothing
is invented: a column with no measurement stays None, and the quality gates
then keep the controller from acting on it.

Column mapping (local lab -> features.v1):
  internal (direct probes + host stats): app_up, http_rps, http_5xx_rps, http_error_ratio,
      http_p95_ms (median of 5 probes), cpu_busy_pct, memory_*, node_load1
  external (gateway, pinned region): network_rtt_p50_ms/p95_ms (HTTP round trip, not ICMP),
      packet_loss_pct (failed probes), ripe_probes_reporting (probe count)
  global (gateway, unpinned): cloudfront.features.cloudfront_5xx_pct / cloudfront_requests
"""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
import json

from ..ingestion.features import VERSION, MIN_PROBES
from ..ingestion.handoff import FEATURE_COLUMNS

REGIONS = ("region-a", "region-b")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _usable(view: dict | None) -> bool:
    return bool(view) and view.get("status") == "ok" and view.get("samples", 0) >= MIN_PROBES


def region_bundle(internal: dict | None, external: dict | None, now: float) -> dict:
    features: dict = {name: None for name in FEATURE_COLUMNS}
    provenance: dict = {}
    if _usable(internal):
        host = internal.get("host") or {}
        samples = internal["samples"]
        failed = round(internal["error_ratio"] * samples)
        # A probe that fails entirely (connection refused / timeout) is an app-down signal
        # only when every probe failed; partial errors are degradation.
        features.update(app_up=0 if internal["error_ratio"] >= 1 and internal.get("latency_ms") is None else 1,
                        http_rps=float(samples), http_5xx_rps=float(failed),
                        http_error_ratio=round(internal["error_ratio"], 5),
                        http_p95_ms=internal.get("latency_ms"),
                        cpu_busy_pct=host.get("cpu_busy_pct"), node_load1=host.get("node_load1"),
                        memory_available_bytes=host.get("memory_available_bytes"),
                        memory_total_bytes=host.get("memory_total_bytes"))
        mem, total = features["memory_available_bytes"], features["memory_total_bytes"]
        features["memory_available_pct"] = round(100 * mem / total, 3) if mem is not None and total else None
        provenance["internal"] = {"source": "local_http_blackbox", "observed_at": internal["observed_at"],
                                  "age_seconds": 0, "latency_stat": internal.get("latency_stat"),
                                  "host_stats": bool(host)}
    if _usable(external):
        features.update(network_rtt_p50_ms=external.get("latency_ms"), network_rtt_p95_ms=external.get("max_ms"),
                        packet_loss_pct=round(100 * external["error_ratio"], 3),
                        ripe_probes_reporting=external["samples"])
        provenance["external"] = {"source": "local_http_user_path", "observed_at": external["observed_at"],
                                  "age_seconds": 0, "note": "HTTP round trip through the gateway, not ICMP"}
    required_missing = [n for n in ("app_up", "memory_available_bytes", "memory_total_bytes") if features[n] is None]
    internal_quality = "fresh" if _usable(internal) and not required_missing else "missing"
    external_quality = ("fresh" if _usable(external) and features["network_rtt_p50_ms"] is not None
                        else "loss_only" if _usable(external) else "insufficient_or_stale")
    return {"features": features,
            "quality": {"internal": internal_quality, "external": external_quality,
                        "required_missing": required_missing,
                        "ripe_probes": features["ripe_probes_reporting"] or 0, "ripe_minimum_probes": MIN_PROBES},
            "provenance": provenance}


def make_local_snapshot(internal: dict, external: dict, global_view: dict | None, now: float) -> dict:
    regions = {r: region_bundle(internal.get(r), external.get(r), now) for r in REGIONS}
    cf_features = {"cloudfront_requests": None, "cloudfront_5xx_pct": None, "cloudfront_total_error_pct": None}
    if _usable(global_view):
        cf_features = {"cloudfront_requests": float(global_view["samples"]),
                       "cloudfront_5xx_pct": round(100 * global_view["error_ratio"], 3),
                       "cloudfront_total_error_pct": round(100 * global_view["error_ratio"], 3),
                       "user_path_latency_p50_ms": global_view.get("latency_ms"),
                       "user_path_latency_max_ms": global_view.get("max_ms")}
    a, b = regions["region-a"]["features"], regions["region-b"]["features"]
    def delta(key):
        return round(a[key] - b[key], 3) if a[key] is not None and b[key] is not None else None
    ready = all(regions[r]["quality"]["internal"] == "fresh" and regions[r]["quality"]["external"] == "fresh" for r in REGIONS)
    internal_ready = all(regions[r]["quality"]["internal"] == "fresh" for r in REGIONS)
    reasons = [f"{r}:{s}:{regions[r]['quality'][s]}" for r in REGIONS for s in ("internal", "external")
               if regions[r]["quality"][s] != "fresh"]
    snapshot = {"contract_version": VERSION, "computed_at": _iso(now), "computed_at_unix": now,
                "pipeline_mode": "local_lab", "ready_for_model": ready,
                "ready_for_internal_only_model": internal_ready,
                "ready_for_automatic_action": False, "quality_reasons": reasons,
                "regions": regions,
                "comparison": {"rtt_p50_a_minus_b_ms": delta("network_rtt_p50_ms"),
                               "packet_loss_a_minus_b_pct": delta("packet_loss_pct"),
                               "cpu_busy_a_minus_b_pct": delta("cpu_busy_pct"),
                               "memory_available_a_minus_b_pct": delta("memory_available_pct"),
                               "http_p95_a_minus_b_ms": delta("http_p95_ms")},
                "cloudfront": {"status": "fresh" if _usable(global_view) else "no_fresh_metrics",
                               "distribution_id": None, "metric_scope": "global",
                               "source": "local_http_user_path", "features": cf_features,
                               "provenance": {}, "last_known": {}},
                "source_errors": [],
                "model_notes": ["Local lab: columns filled from HTTP probes and /internal/stats; see provenance."]}
    digest = sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    snapshot["snapshot_id"] = f"features-{digest}"
    return snapshot
