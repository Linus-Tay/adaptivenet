"""Debug view of the decision pipeline, served by the detector service (lab-ml).

The main dashboard (:3000) reads ``/api/state``; ``/`` is a raw debug page.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import secrets
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
import httpx

from adaptivenet.ingestion.fast_signals import load_fast_signals

from .file_queue import FileQueue

router = APIRouter()
HTML = (Path(__file__).with_name("view.html")).read_text(encoding="utf-8")


def data_dir() -> Path:
    return Path(os.environ.get("DEMO_DATA_DIR", "/app/data"))


def tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _get(client: httpx.Client, url: str, token: str | None = None):
    try:
        response = client.get(url, timeout=3, headers={"X-Lab-Token": token} if token else None)
        return response.json() if response.status_code == 200 else {"status": response.status_code}
    except (httpx.HTTPError, ValueError):
        return None


@router.get("/", response_class=HTMLResponse)
def index():
    return HTML


@router.get("/api/state")
def state():
    from .ml_service import assess, latest
    data = data_dir()
    token = os.environ.get("LAB_TOKEN", "")
    snapshot = latest()
    try:
        assessment = assess(snapshot) if snapshot else None
    except Exception as exc:  # show the error on the page, never crash the view
        assessment = {"error": f"{type(exc).__name__}: {exc}"}
    with httpx.Client(trust_env=False) as client:
        faults = {}
        if os.environ.get("OBSERVER_GATEWAY_URL"):
            gw = _get(client, os.environ["OBSERVER_GATEWAY_URL"].rstrip("/") + "/admin/state", token)
            faults["gateway"] = gw
            faults["region_a_percent"] = (gw or {}).get("region_a_percent")
        for region, var in (("region-a", "REGION_A_URL"), ("region-b", "REGION_B_URL")):
            if os.environ.get(var):
                metrics = _get(client, os.environ[var].rstrip("/") + "/lab/metrics")
                faults[region] = (metrics or {}).get("active_fault")
    queue = FileQueue(data / "queue").depth()
    outbox = len(list((data / "outbox").glob("*.json"))) if (data / "outbox").exists() else 0
    return {
        "mode": os.environ.get("DEMO_MODE", "live_lab"),
        "assessment": assessment,
        "snapshot": snapshot,
        "fast": load_fast_signals(data / "probe-events.jsonl"),
        "decisions": tail_jsonl(data / "decisions.jsonl", 40)[::-1],
        "fault_log": tail_jsonl(data / "fault-log.jsonl", 20)[::-1],
        "active_faults": faults,
        "pipeline": {"queue": queue, "outbox_pending": outbox},
    }


@router.post("/api/reset")
def reset(x_lab_token: str | None = Header(default=None)):
    """Ask the lab controller to start a fresh journal (lab gateway only)."""
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(x_lab_token or "", expected):
        raise HTTPException(401, "Lab token required")
    (data_dir() / "reset-request").write_text(str(time.time()))
    return {"status": "requested"}
