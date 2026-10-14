"""Fixed-host, read-only upstream adapters. AWS SigV4 via existing awscurl SSO."""
from __future__ import annotations
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError
import re
import ssl
from pathlib import Path
import json
import math
import os
import statistics
import subprocess
import time
from .models import Observation
from .config import RegionConfig

# Explicit PromQL: no user-supplied expressions or URLs can reach subprocess.
# Server exposes status="200" and region labels, as seen in deployed demo_metrics.py.
PROMQL = {
    "app_up": ('max(adaptivenet_demo_up{region="{name}",project="adaptivenet"})', "bool"),
    "http_rps": ('sum(rate(adaptivenet_http_requests_total{route="/api/catalog",region="{name}",project="adaptivenet"}[5m]))', "requests/s"),
    "http_5xx_rps": ('sum(rate(adaptivenet_http_requests_total{route="/api/catalog",status=~"5..",region="{name}",project="adaptivenet"}[5m])) or (0 * sum(rate(adaptivenet_http_requests_total{route="/api/catalog",region="{name}",project="adaptivenet"}[5m])))', "requests/s"),
    "http_p95_ms": ('1000 * histogram_quantile(0.95, sum by (le) (rate(adaptivenet_http_request_duration_seconds_bucket{route="/api/catalog",region="{name}",project="adaptivenet"}[5m])))', "ms"),
    "memory_available_bytes": ('max(node_memory_MemAvailable_bytes{job="node",project="adaptivenet"})', "bytes"),
    "memory_total_bytes": ('max(node_memory_MemTotal_bytes{job="node",project="adaptivenet"})', "bytes"),
    "cpu_busy_pct": ('100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle",job="node",project="adaptivenet"}[5m])))', "%"),
    "node_load1": ('max(node_load1{job="node",project="adaptivenet"})', "load"),
}

@dataclass
class Collection:
    observations: list[Observation]
    status: str
    errors: list[str]


class UpstreamDataError(ValueError):
    """A stable, safe diagnostic code; never includes raw remote responses."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def safe_token(value: object) -> str:
    """Allow a bounded provider error TYPE, never raw AWS/RIPE error details."""
    token = str(value or "unknown")[:48]
    return re.sub(r"[^A-Za-z0-9_-]", "_", token)


def amp_error_code(payload: object) -> str:
    """Diagnose a provider error without revealing user/role IDs or freeform response text."""
    if not isinstance(payload, dict):
        return "amp_response_not_object"
    typed = (payload.get("errorType") or payload.get("__type") or
             payload.get("code") or payload.get("Code") or payload.get("type"))
    if typed:
        return "amp_api_error_" + safe_token(typed)
    message = str(payload.get("message") or payload.get("Message") or
                  payload.get("error") or "").lower()
    if any(s in message for s in ("accessdenied", "not authorized", "unauthorized", "not allowed", "permission")):
        return "amp_access_denied"
    if any(s in message for s in ("signature", "credential", "security token", "invalid token", "expired token")):
        return "amp_authentication_failed"
    if "missing authentication token" in message or "not found" in message or "unknown operation" in message:
        return "amp_endpoint_or_method_error"
    if any(s in message for s in ("query", "parse", "bad_data", "syntax", "invalid parameter", "invalid request")):
        return "amp_query_rejected"
    if any(s in message for s in ("throttl", "too many request", "rate limit")):
        return "amp_rate_limited"
    if any(s in message for s in ("timeout", "temporar", "unavailable")):
        return "amp_provider_unavailable"
    return "amp_api_error_unclassified"


def parse_prometheus(payload: dict, now: float, max_age: int = 180) -> tuple[float, float] | None:
    if not isinstance(payload, dict):
        raise UpstreamDataError("amp_response_not_object")
    if payload.get("status") != "success":
        # AWS may return a Prometheus errorType or an AWS-service error shape.
        # Never expose the freeform message, which may contain request details.
        raise UpstreamDataError(amp_error_code(payload))
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise UpstreamDataError("amp_data_not_object")
    if data.get("resultType") != "vector":
        raise UpstreamDataError("amp_result_type_" + safe_token(data.get("resultType")))
    if not isinstance(data.get("result", []), list):
        raise UpstreamDataError("amp_result_not_list")
    valid = []
    for series in data.get("result", []):
        pair = series.get("value", [])
        if len(pair) != 2:
            continue
        try:
            timestamp, val = float(pair[0]), float(pair[1])
        except (ValueError, TypeError):
            continue
        if (not math.isfinite(timestamp) or not math.isfinite(val) or timestamp > now + 60
            or timestamp < now - max_age):
            continue
        valid.append((timestamp, val))
    if not valid:
        return None
    # PromQL expressions are all scalar-reduced; more series signals a contract break.
    if len(valid) != 1:
        raise UpstreamDataError("amp_multiple_series")
    return valid[0]


def query_amp(cfg: RegionConfig, metric: str, profile: str, now: float, timeout=25) -> Observation | None:
    expression, unit = PROMQL[metric]
    expression = expression.replace("{name}", cfg.name)
    # AWS documents signed, form-encoded POST requests for Prometheus queries.
    # More robust for quotes/braces/regex than URL query strings. Read-only operation.
    url = (f"https://aps-workspaces.{cfg.aws_region}.amazonaws.com/workspaces/"
           f"{cfg.workspace}/api/v1/query")
    cmd = ["awscurl", "--region", cfg.aws_region, "--service", "aps",
           "-X", "POST", url,
           "--header", "Content-Type: application/x-www-form-urlencoded",
           "-d", urlencode({"query": expression})]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            check=False, env={**os.environ, "AWS_PROFILE": profile, "AWS_PAGER": ""})
    if result.returncode:
        # Never return stdout/stderr: external utilities can include session details.
        raise RuntimeError(f"AMP {metric}: signed request failed (exit {result.returncode})")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise UpstreamDataError("amp_non_json_response") from exc
    parsed = parse_prometheus(payload, now=now)
    if parsed is None:
        return None  # Missing data must stay missing, not zero.
    timestamp, value = parsed
    if metric in ("app_up",) and value not in (0, 1):
        raise UpstreamDataError("amp_invalid_app_up")
    if metric == "cpu_busy_pct" and not (-0.01 <= value <= 100.01):
        raise UpstreamDataError("amp_invalid_cpu")
    if metric in ("http_rps", "http_5xx_rps", "http_p95_ms", "memory_available_bytes", "memory_total_bytes", "node_load1") and value < 0:
        raise UpstreamDataError("amp_negative_value")
    try:
        return Observation(cfg.name, "aws_amp", metric, value, unit, timestamp, now,
                           source_id=f"{cfg.aws_region}/{cfg.workspace}")
    except ValueError as exc:
        raise UpstreamDataError("amp_observation_validation") from exc


def parse_atlas_latest(payload: dict | list, cfg: RegionConfig, now: float) -> list[Observation]:
    """Normalize RIPE latest results returned as probe-keyed maps OR flat lists.

    Latest documented as {probe_id: [result]}, but real provider responses may
    be a list of per-probe ping records. Keep only newest result per probe;
    outdated or stopped measurements are retained in storage, never 'freshened'.
    """
    candidates: list[tuple[object, dict]] = []
    if isinstance(payload, dict):
        for key, records in payload.items():
            if isinstance(records, dict):
                records = [records]
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict):
                        candidates.append((key, record))
    elif isinstance(payload, list):
        for record in payload:
            if isinstance(record, dict):
                candidates.append((record.get("prb_id"), record))
    else:
        raise UpstreamDataError("atlas_response_not_object_or_list")

    latest_by_probe: dict[int, tuple[float, int, list[float]]] = {}
    # Bounded processing in case the upstream mistakenly sends a huge response.
    for probe_key, rec in candidates[:10000]:
        try:
            probe_id = int(rec.get("prb_id", probe_key))
            timestamp = float(rec["timestamp"])
            sent = int(rec.get("sent", len(rec.get("result", []))))
            if "msm_id" in rec and int(rec["msm_id"]) != cfg.measurement:
                continue
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if (probe_id <= 0 or not math.isfinite(timestamp) or timestamp <= 0
                or timestamp > now + 300 or sent <= 0 or sent > 64):
            continue
        packets = rec.get("result", [])
        if not isinstance(packets, list):
            continue
        replies = []
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            rtt = packet.get("rtt")
            if (isinstance(rtt, (int, float)) and not isinstance(rtt, bool)
                    and math.isfinite(rtt) and 0 <= rtt <= 120000):
                replies.append(float(rtt))
        if len(replies) > sent:
            continue
        if probe_id not in latest_by_probe or timestamp > latest_by_probe[probe_id][0]:
            latest_by_probe[probe_id] = (timestamp, sent, replies)

    events: list[Observation] = []
    for probe_id, (timestamp, sent, replies) in sorted(latest_by_probe.items()):
        source_id = f"{cfg.measurement}/{probe_id}"
        labels = {"probe_id": str(probe_id), "measurement_id": str(cfg.measurement)}
        loss = (sent - len(replies)) / sent * 100
        events.append(Observation(cfg.name, "ripe_atlas", "packet_loss_pct", loss, "%", timestamp, now,
                                  source_id=source_id, labels=labels))
        if replies:
            events.append(Observation(cfg.name, "ripe_atlas", "network_rtt_ms",
                                      statistics.median(replies), "ms", timestamp, now,
                                      source_id=source_id, labels=labels))
    return events


def atlas_tls_context() -> ssl.SSLContext:
    """Verified trust store; never disable certificate verification."""
    explicit = os.environ.get("SSL_CERT_FILE", "").strip()
    if explicit:
        ca = Path(explicit).expanduser()
        if not ca.is_file():
            raise UpstreamDataError("atlas_ca_file_missing")
        return ssl.create_default_context(cafile=str(ca))
    # macOS python.org Python may not inherit Homebrew's trust bundle.
    for ca in (
        "/opt/homebrew/opt/ca-certificates/share/ca-certificates/cacert.pem",
        "/usr/local/opt/ca-certificates/share/ca-certificates/cacert.pem",
    ):
        if Path(ca).is_file():
            return ssl.create_default_context(cafile=ca)
    return ssl.create_default_context()


def query_atlas(cfg: RegionConfig, now: float, timeout=18) -> list[Observation]:
    url = f"https://atlas.ripe.net/api/v2/measurements/{cfg.measurement}/latest/?versions=1"
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "AdaptiveNet-Ingestion/1.0"})
    with urlopen(req, timeout=timeout, context=atlas_tls_context()) as res:
        return parse_atlas_latest(json.load(res), cfg, now)


def collect_all(regions: list[RegionConfig], profile: str, now: float | None = None, max_workers=3) -> Collection:
    now = time.time() if now is None else now
    observations: list[Observation] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for cfg in regions:
            futures[pool.submit(query_atlas, cfg, now)] = (cfg.name, "atlas")
            for metric in PROMQL:
                futures[pool.submit(query_amp, cfg, metric, profile, now)] = (cfg.name, metric)
        for future in as_completed(futures):
            region, name = futures[future]
            try:
                result = future.result()
                if isinstance(result, list):
                    observations.extend(result)
                elif result is not None:
                    observations.append(result)
                else:
                    errors.append(f"{region}:{name}:no_series")
            except UpstreamDataError as exc:
                errors.append(f"{region}:{name}:{exc.code}")
            except subprocess.TimeoutExpired:
                errors.append(f"{region}:{name}:upstream_timeout")
            except URLError as exc:
                # Include only the exception type, never the potentially sensitive URL.
                reason = safe_token(type(getattr(exc, "reason", None)).__name__)
                errors.append(f"{region}:{name}:network_{reason}")
            except TimeoutError:
                errors.append(f"{region}:{name}:upstream_timeout")
            except (Exception) as exc:
                # Unexpected errors remain type-only; no response bodies or credentials.
                errors.append(f"{region}:{name}:{type(exc).__name__}")
    return Collection(observations, "ok" if not errors else "partial", sorted(errors))
