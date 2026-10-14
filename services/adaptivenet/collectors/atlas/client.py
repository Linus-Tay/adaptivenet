"""Read-only RIPE Atlas measurement ingestion.

No arbitrary URL fetching: the upstream host is fixed. Measurement IDs are
operator-supplied, and fresh observations are never inferred from stale data.
"""
from datetime import datetime, timezone
import math
import os
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from ...gateway import authorize

API_BASE = "https://atlas.ripe.net/api/v2"
router = APIRouter(prefix="/telemetry/atlas", tags=["External telemetry"])


def configured_measurements() -> dict[str, int]:
    """ATLAS_MEASUREMENTS='region-a=12345,region-b=67890'."""
    raw = os.getenv("ATLAS_MEASUREMENTS", "").strip()
    if not raw:
        return {}
    result = {}
    for part in raw.split(","):
        pair = part.strip().split("=", 1)
        if len(pair) != 2 or not pair[0] or not pair[1].isdigit() or int(pair[1]) < 1:
            raise ValueError("ATLAS_MEASUREMENTS must use region=positive_measurement_id pairs")
        region, measurement = pair[0].strip(), int(pair[1])
        if not region or region in result:
            raise ValueError("Duplicate or empty Atlas region")
        result[region] = measurement
    return result


def normalize_ping(measurement_id: int, region: str, raw: dict[str, Any], now: datetime | None = None) -> dict:
    """Normalize a probe's ping result and preserve raw timing for freshness checks."""
    now = now or datetime.now(timezone.utc)
    timestamp = raw.get("timestamp")
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
        raise ValueError("Missing/invalid Atlas timestamp")
    observed = datetime.fromtimestamp(timestamp, timezone.utc)
    replies = [v["rtt"] for v in raw.get("result", []) if isinstance(v, dict)
               and isinstance(v.get("rtt"), (int, float)) and math.isfinite(v["rtt"]) and v["rtt"] >= 0]
    packets = raw.get("sent", len(raw.get("result", [])))
    packets = packets if isinstance(packets, int) and packets >= len(replies) else len(raw.get("result", []))
    loss = (1 - len(replies) / packets) * 100 if packets else None
    age = (now - observed).total_seconds()
    # RIPE latest responses may be cached for ~5 minutes; never treat old data as live.
    stale = age > int(os.getenv("ATLAS_MAX_AGE_SECONDS", "900")) or age < -300
    return {"region": region, "measurement_id": measurement_id, "probe_id": raw.get("prb_id"),
            "observed_at": observed.isoformat(), "age_seconds": round(age, 1),
            "rtt_ms": round(sum(replies) / len(replies), 3) if replies else None,
            "packet_loss_percent": round(loss, 2) if loss is not None else None,
            "sent": packets, "received": len(replies),
            "status": "stale" if stale else ("ok" if replies else "timeout"), "source": "ripe_atlas"}


async def fetch_latest(measurement_id: int, region: str, client: httpx.AsyncClient) -> dict:
    response = await client.get(f"{API_BASE}/measurements/{measurement_id}/latest/", params={"versions": 1})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected RIPE Atlas latest payload")
    samples = []
    for values in payload.values():
        if not isinstance(values, list) or not values or not isinstance(values[0], dict):
            continue
        try:
            samples.append(normalize_ping(measurement_id, region, values[0]))
        except (ValueError, OverflowError, TypeError, KeyError):
            continue
    return {"region": region, "measurement_id": measurement_id, "sample_count": len(samples),
            "status": "observed" if any(sample["status"] != "stale" for sample in samples) else "no_fresh_data",
            "probes": samples}


@router.get("/latest")
async def latest(x_lab_token: str | None = Header(default=None), region: str | None = Query(default=None)):
    authorize(x_lab_token)
    try:
        configured = configured_measurements()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    if region:
        if region not in configured:
            raise HTTPException(404, "No measurement configured for region")
        configured = {region: configured[region]}
    if not configured:
        return {"status": "not_configured", "regions": [], "automation": "disabled"}
    results = []
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for name, measurement in configured.items():
            try:
                results.append(await fetch_latest(measurement, name, client))
            except (httpx.HTTPError, ValueError) as exc:
                results.append({"region": name, "measurement_id": measurement,
                                "status": "upstream_error", "detail": type(exc).__name__, "probes": []})
    return {"status": "ok" if all(r["status"] == "observed" for r in results) else "partial",
            "regions": results, "automation": "disabled"}
