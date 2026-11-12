"""Plain-language overview of the REAL AdaptiveNet telemetry (read-only).

Source, in order:
1. ``live``: the running ingestion API (``scripts/live_monitor.py --sqs`` on the
   host, :8766), via ``INGESTION_API_URL``;
2. ``recorded``: the latest snapshot in a read-only copy of the real data folder
   (``REAL_DATA_DIR`` with ``adaptivenet-ingestion.db`` and ``probe-events.jsonl``).
Recorded data is evaluated *as of its own timestamp* and is always labelled with
that time; it is never presented as current. Nothing here writes anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import json
import time

import httpx

from adaptivenet.ingestion.fast_signals import build_fast_signals
from adaptivenet.ingestion.handoff import build_model_input
from ml.team_starter.feature_views import GROUPS

from .detectors import assess_group

REGIONS = ("region-a", "region-b")
NAMES = {"region-a": "Singapore", "region-b": "Virginia", "cloudfront": "CloudFront"}


def _live() -> tuple[dict, dict] | None:
    base = os.environ.get("INGESTION_API_URL")
    if not base:
        return None
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            snap = client.get(base.rstrip("/") + "/v1/features/latest")
            fast = client.get(base.rstrip("/") + "/v1/fast-signals/latest")
        if snap.status_code == 200 and fast.status_code == 200:
            return snap.json(), fast.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _recorded(at: float | None = None) -> tuple[dict, dict] | None:
    folder = Path(os.environ.get("REAL_DATA_DIR", "/real"))
    db = folder / "adaptivenet-ingestion.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT body_json FROM feature_snapshots WHERE computed_at <= ? "
                              "ORDER BY computed_at DESC LIMIT 1", (at if at is not None else 1e12,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    snapshot = json.loads(row[0])
    at = snapshot["computed_at_unix"]
    return snapshot, build_fast_signals(_witness_window(folder, at), now=at)


def _witness_window(folder: Path, at: float, span: float = 900) -> list[dict]:
    """HTTP witness events in the 15 minutes up to ``at`` from the journal and the archive."""
    out, seen = [], set()
    for path in (folder / "probe-events.jsonl", folder / "ml-archive" / "http_witness.jsonl"):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                    ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                key = (event.get("timestamp"), event.get("scope"))
                if at - span <= ts <= at and key not in seen:
                    seen.add(key)
                    out.append(event)
    return out


def _fresh(age: float | None, limit: float) -> str:
    if age is None:
        return "missing"
    return "fresh" if age <= limit else "stale"


def _age(iso: str | None, at: float) -> float | None:
    if not iso:
        return None
    try:
        return round(at - datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def recorded_range() -> dict | None:
    db = Path(os.environ.get("REAL_DATA_DIR", "/real")) / "adaptivenet-ingestion.db"
    if not db.is_file():
        return None
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        lo, hi, n = con.execute("SELECT min(computed_at), max(computed_at), count(*) FROM feature_snapshots").fetchone()
    finally:
        con.close()
    return {"first_unix": lo, "last_unix": hi, "count": n} if n else None


def verified_episodes() -> list[dict]:
    """Reviewed episodes (labels made by people from independent evidence), for quick navigation."""
    path = Path(os.environ.get("REAL_DATA_DIR", "/real")) / "verified_episodes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    out = []
    for e in data.get("episodes") or []:
        try:
            start = datetime.fromisoformat(e["verified_start_utc"].replace("Z", "+00:00")).timestamp()
            end = datetime.fromisoformat(e["verified_end_utc"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            continue
        out.append({"id": e.get("episode_id"), "label": e.get("label"), "region": e.get("region"),
                    "scenario": e.get("scenario"), "start_unix": start, "end_unix": end,
                    "reviewed": e.get("reviewed") is True})
    return sorted(out, key=lambda e: e["start_unix"])


def overview(at: float | None = None) -> dict:
    """``at`` (unix seconds) picks a recorded moment; otherwise live first, then latest recorded."""
    live = None if at is not None else _live()
    source = "live" if live else "recorded"
    data = live or _recorded(at)
    if data is None:
        return {"source": "none", "message": "No real telemetry: the live pipeline (:8766) is not running "
                "and no recorded data folder is mounted (REAL_DATA_DIR)."}
    snapshot, fast = data
    at = snapshot["computed_at_unix"] if source == "recorded" else time.time()
    slow = build_model_input(snapshot, mode="combined", now=at)
    payload = {"contract_version": "adaptivenet.detection_input.v1", "slow": slow, "fast": fast}
    regions = {}
    for region in REGIONS:
        bundle = snapshot["regions"][region]
        f, q, prov = bundle["features"], bundle["quality"], bundle.get("provenance") or {}
        amp_ages = [_age(p.get("observed_at"), at) for k, p in prov.items() if k != "ripe_atlas"]
        amp_age = min((a for a in amp_ages if a is not None), default=None)
        ripe = prov.get("ripe_atlas") or {}
        w = ((fast.get("scopes") or {}).get(region) or {}).get("windows", {}).get("300s", {})
        regions[region] = {
            "name": NAMES[region],
            "aws": {"status": q.get("internal"), "age_seconds": amp_age,
                    "app_up": f.get("app_up"), "cpu_pct": f.get("cpu_busy_pct"),
                    "memory_free_pct": f.get("memory_available_pct"), "p95_ms": f.get("http_p95_ms"),
                    "error_ratio": f.get("http_error_ratio"), "missing": q.get("required_missing") or []},
            "ripe": {"status": q.get("external"), "age_seconds": ripe.get("oldest_age_seconds"),
                     "rtt_ms": f.get("network_rtt_p50_ms"), "loss_pct": f.get("packet_loss_pct"),
                     "probes": f.get("ripe_probes_reporting")},
            "http": {"checks": w.get("count"), "failure_ratio": w.get("failure_ratio"),
                     "p95_ms": w.get("http_latency_p95_ms"),
                     "age_seconds": ((fast.get("scopes") or {}).get(region) or {}).get("age_seconds")},
        }
    cf = snapshot.get("cloudfront") or {}
    cw = ((fast.get("scopes") or {}).get("cloudfront") or {})
    return {
        "source": source,
        "recorded_range": recorded_range(),
        "episodes": verified_episodes(),
        "as_of_unix": at,
        "as_of": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "snapshot_id": snapshot.get("snapshot_id"),
        "ready_for_model": snapshot.get("ready_for_model"),
        "quality_reasons": snapshot.get("quality_reasons") or [],
        "regions": regions,
        "cloudfront": {"status": cf.get("status"), "metrics": cf.get("features") or {},
                       "http": cw.get("windows", {}).get("300s", {}), "http_age_seconds": cw.get("age_seconds")},
        "detectors": {g: assess_group(payload, g) for g in GROUPS},
        "witness_vantage": fast.get("independent_vantage_count"),
        "notes": ["Recorded data is shown as of its own time, not as current." if source == "recorded"
                  else "Live data from the running ingestion API.",
                  "RIPE Atlas results arrive about every 15 minutes; HTTP checks every few seconds.",
                  "The HTTP witness has one network vantage point."],
    }
