"""Time-aware fusion into immutable, inspectable, leakage-resistant features.

Uses dictionaries for O(1) signal buckets, bounded deques for rolling windows,
and one newest observation per Atlas probe to avoid double-counting cached results.
"""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import statistics
from .models import Observation, REGIONS

VERSION = "adaptivenet.features.v1"
AMP_FRESH_SEC = 180
# AWS AMP instant-query samples can be several seconds ahead of the Mac clock.
# Allow ONLY a small bounded skew, preserve original source timestamps, and
# record the skew in provenance. It is never used to freshen RIPE measurements.
AMP_CLOCK_SKEW_SEC = 30
ATLAS_FRESH_SEC = 2100  # 15 min measurements + up to 5 min cache and jitter
WINDOW_SEC = 600
MIN_PROBES = 3
REQUIRED_INTERNAL = ("app_up", "memory_available_bytes", "memory_total_bytes")


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    arr = sorted(values)
    idx = (len(arr)-1)*p
    lo, hi = math.floor(idx), math.ceil(idx)
    return arr[lo] + (arr[hi]-arr[lo])*(idx-lo)


def ewma(values: list[float], alpha: float = .35) -> float | None:
    if not values:
        return None
    current = values[0]
    for item in values[1:]:
        current = alpha * item + (1-alpha) * current
    return round(current, 4)


def latest_eligible(items: list[Observation], now: float, freshness: int) -> Observation | None:
    fresh = [o for o in items if -AMP_CLOCK_SKEW_SEC <= now-o.observed_at <= freshness and o.collected_at <= now]
    return max(fresh, key=lambda o: (o.observed_at, o.event_id)) if fresh else None


def robust_zscore(history: list[float]) -> float | None:
    if len(history) < 5:
        return None
    baseline = history[:-1]
    med = statistics.median(baseline)
    mad = statistics.median(abs(v-med) for v in baseline)
    if mad == 0:
        return 0.0 if history[-1] == med else None
    return round(.6745*(history[-1]-med)/mad, 3)


def make_snapshot(events: list[Observation], now: float, *, errors: list[str] | None = None,
                  configured_probes=5, cloudfront_distribution: str | None = None) -> dict:
    errors = errors or []
    # No event from the future and no source timestamp exceeding supported history.
    buckets: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for event in events:
        if event.region in REGIONS and -AMP_CLOCK_SKEW_SEC <= now-event.observed_at <= ATLAS_FRESH_SEC and event.collected_at <= now:
            buckets[(event.region, event.metric)].append(event)
    regions = {}
    for region in REGIONS:
        metrics: dict[str, float | int | None] = {}
        latest_source_time = {}
        missing = []
        provenance = {}
        for metric in ("app_up", "http_rps", "http_5xx_rps", "http_p95_ms",
                       "cpu_busy_pct", "memory_available_bytes", "memory_total_bytes", "node_load1"):
            items = buckets[(region, metric)]
            obs = latest_eligible(items, now, AMP_FRESH_SEC)
            metrics[metric] = round(obs.value, 4) if obs else None
            if obs:
                latest_source_time[metric] = iso(obs.observed_at)
                provenance[metric] = {"source": "aws_amp", "observed_at": iso(obs.observed_at),
                                      "age_seconds": max(0, round(now-obs.observed_at)),
                                      "clock_skew_ahead_seconds": round(max(0, obs.observed_at-now), 3)}
        mem = metrics["memory_available_bytes"]
        total = metrics["memory_total_bytes"]
        metrics["memory_available_pct"] = round(100*mem/total, 3) if mem is not None and total and total>0 and 0<=mem<=total else None
        count = metrics["http_rps"]
        err = metrics["http_5xx_rps"]
        # Never interpret absent denominator as zero errors.
        metrics["http_error_ratio"] = round(err/count, 5) if count is not None and count > 0 and err is not None and 0 <= err <= count*1.001 else None
        # Atlas: one *latest* valid ping result per probe, not repeated polling observations.
        latest_loss = {}
        latest_rtt = {}
        for metric, target in (("packet_loss_pct", latest_loss), ("network_rtt_ms", latest_rtt)):
            for item in buckets[(region, metric)]:
                if item.source != "ripe_atlas" or not 0 <= now-item.observed_at <= ATLAS_FRESH_SEC:
                    continue
                previous = target.get(item.source_id)
                if previous is None or item.observed_at > previous.observed_at:
                    target[item.source_id] = item
        # Prevent RTT from a different timestamp being combined with the loss event.
        rtts = [item.value for key,item in latest_rtt.items()
                if key in latest_loss and abs(latest_loss[key].observed_at-item.observed_at) < 0.01]
        losses = [item.value for item in latest_loss.values()]
        probe_count = len(latest_loss)
        metrics["ripe_probes_reporting"] = probe_count
        metrics["network_rtt_p50_ms"] = round(statistics.median(rtts), 3) if rtts else None
        metrics["network_rtt_p95_ms"] = round(percentile(rtts, .95), 3) if rtts else None
        metrics["packet_loss_pct"] = round(statistics.mean(losses), 3) if losses else None
        last_atlas = max((o.observed_at for o in latest_loss.values()), default=None)
        if last_atlas is not None:
            provenance["ripe_atlas"] = {"source": "ripe_atlas", "latest_observed_at": iso(last_atlas),
                                        "oldest_age_seconds": round(max(now-o.observed_at for o in latest_loss.values())),
                                        "distinct_probes": probe_count}
        # Rolling internal gauges/counters, ordered by observed_at; duplicated queries deduped by timestamp.
        for metric, feature_name in (("cpu_busy_pct", "cpu_busy_ewma_pct"),
                                     ("http_p95_ms", "http_p95_ewma_ms")):
            timeseries = {}
            for o in buckets[(region, metric)]:
                if o.source == "aws_amp" and -AMP_CLOCK_SKEW_SEC <= now-o.observed_at <= WINDOW_SEC and o.collected_at <= now:
                    timeseries[o.observed_at] = o.value
            ordered = [v for _,v in sorted(timeseries.items())]
            # Keep only last 120 points in memory even if a faulty collector floods us.
            queue = deque(ordered[-120:], maxlen=120)
            metrics[feature_name] = ewma(list(queue))
            if metric == "http_p95_ms":
                metrics["http_p95_robust_z"] = robust_zscore(list(queue))
        for required in REQUIRED_INTERNAL:
            if metrics[required] is None:
                missing.append(required)
        internal_quality = "fresh" if not missing else "missing"
        external_quality = ("fresh" if probe_count >= MIN_PROBES and rtts else
                            "loss_only" if probe_count >= MIN_PROBES else "insufficient_or_stale")
        regions[region] = {
            "features": metrics,
            "quality": {"internal": internal_quality, "external": external_quality,
                        "required_missing": missing, "ripe_probes": probe_count,
                        "ripe_minimum_probes": MIN_PROBES, "allowed_aws_clock_skew_seconds": AMP_CLOCK_SKEW_SEC, "freshness_seconds": {"aws_amp": AMP_FRESH_SEC, "ripe_atlas": ATLAS_FRESH_SEC}},
            "provenance": provenance,
        }
    # Global CloudFront metrics are separate from regional AMP/RIPE metrics.
    # They are optional to the current ML readiness gates.
    from .cloudfront import STANDARD, FRESH_SECONDS
    cf_metrics = {name: None for name in STANDARD}
    cf_provenance = {}
    for metric in STANDARD:
        values = [e for e in events if e.region == "global" and e.source == "aws_cloudwatch"
                  and e.metric == metric and e.source_id == cloudfront_distribution
                  and 0 <= now-e.observed_at <= FRESH_SECONDS and e.collected_at <= now]
        if values:
            latest = max(values, key=lambda e:(e.observed_at,e.event_id))
            cf_metrics[metric] = round(latest.value, 4)
            cf_provenance[metric] = {"observed_at": iso(latest.observed_at),
                                     "age_seconds": round(now-latest.observed_at),
                                     "source": "aws_cloudwatch"}
    cf_status = ("not_configured" if not cloudfront_distribution else
                 "fresh" if any(v is not None for v in cf_metrics.values()) else "no_fresh_metrics")
    cloudfront = {"status": cf_status, "distribution_id": cloudfront_distribution,
                  "aws_region": "us-east-1", "metric_scope": "global",
                  "freshness_seconds": FRESH_SECONDS, "features": cf_metrics,
                  "provenance": cf_provenance}
    a, b = (regions[key]["features"] for key in REGIONS)
    def delta(key):
        return round(a[key]-b[key], 3) if a[key] is not None and b[key] is not None else None
    comparison = {
        "rtt_p50_a_minus_b_ms": delta("network_rtt_p50_ms"),
        "packet_loss_a_minus_b_pct": delta("packet_loss_pct"),
        "cpu_busy_a_minus_b_pct": delta("cpu_busy_pct"),
        "memory_available_a_minus_b_pct": delta("memory_available_pct"),
        "http_p95_a_minus_b_ms": delta("http_p95_ms"),
    }
    ready = all(regions[r]["quality"]["internal"] == "fresh" and
                regions[r]["quality"]["external"] == "fresh" for r in REGIONS)
    reasons = []
    for r in REGIONS:
        for source in ("internal", "external"):
            if regions[r]["quality"][source] != "fresh":
                reasons.append(f"{r}:{source}:{regions[r]['quality'][source]}")
    if errors:
        # An absent Prometheus series is data missingness, not a failed HTTP/API fetch.
        # Preserve no_series diagnostics for the UI, but don't label them as outages.
        if any(e.endswith(":no_series") for e in errors):
            reasons.append("one_or_more_prometheus_series_absent")
        if any(not e.endswith(":no_series") for e in errors):
            reasons.append("one_or_more_upstream_fetches_failed")
    internal_only_ready = all(regions[r]["quality"]["internal"] == "fresh" for r in REGIONS)
    snapshot = {"contract_version": VERSION, "computed_at": iso(now), "computed_at_unix": now,
                "pipeline_mode": "read_only", "ready_for_model": ready,
                "ready_for_internal_only_model": internal_only_ready,
                "ready_for_automatic_action": False, "quality_reasons": reasons,
                "regions": regions, "comparison": comparison, "cloudfront": cloudfront,
                "source_errors": errors, "model_notes": [
                    "Null means unavailable, never zero; preserve missingness in model preprocessing.",
                    "RIPE ICMP RTT differs from HTTP request duration; compare trends, not absolute values.",
                    "AWS source clock skew up to 30s is allowed and annotated; source timestamps are never rewritten.",
                    "Readiness is a data gate, not approval for route changes."]}
    digest = sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    snapshot["snapshot_id"] = f"features-{digest}"
    return snapshot
