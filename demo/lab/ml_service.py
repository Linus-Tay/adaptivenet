"""Demo ML inference service (shadow only, read-only, no actuation).

``GET /v1/assessment`` builds the existing ``adaptivenet.detection_input.v1``
from the latest stored snapshot and HTTP witness journal, then reports:
* the ``ml/predictor.py`` proposal (rule baseline, or a trained artifact when
  ``ADAPTIVENET_SHADOW_MODEL`` names one) and its independent safety review;
* optional trained proposals per feature group from ``DEMO_MODELS_DIR``
  (``aws_only.pkl``, ``aws_ripe.pkl``, ``aws_ripe_http.pkl``);
* the lab rule detector for each feature group, side by side.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from adaptivenet.ingestion.decisions import review_proposal
from adaptivenet.ingestion.fast_signals import load_fast_signals
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.store import Store
from ml import predictor
from ml.team_starter.feature_views import GROUPS

from .detectors import assess_group

app = FastAPI(title="AdaptiveNet demo ML (shadow)")


def data_dir() -> Path:
    return Path(os.environ.get("DEMO_DATA_DIR", "/app/data"))


def detection_input(snapshot: dict, now: float) -> dict:
    slow = build_model_input(snapshot, mode="combined", now=now)
    fast = load_fast_signals(data_dir() / "probe-events.jsonl", now=now)
    return {"contract_version": "adaptivenet.detection_input.v1", "generated_at_unix": now,
            "slow": slow, "fast": fast,
            "ready_for_shadow_assessment": slow["ready_for_inference"] and fast["ready_for_fast_assessment"],
            "automatic_actions_enabled": False}


def trained_proposals(payload: dict) -> dict:
    folder = os.environ.get("DEMO_MODELS_DIR")
    out = {}
    if not folder:
        return out
    from ml.team_starter.shadow import shadow_predict
    for group in GROUPS:
        path = Path(folder) / f"{group}.pkl"
        if path.is_file():
            try:
                out[group] = shadow_predict(payload, path)
            except Exception as exc:  # report, never crash the demo on a bad artifact
                out[group] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def assess(snapshot: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    payload = detection_input(snapshot, now)
    model_arg = payload if os.environ.get("ADAPTIVENET_SHADOW_MODEL") else payload["slow"]
    try:
        proposal = predictor.predict(model_arg)
        review = review_proposal(snapshot, proposal)
    except Exception as exc:
        proposal, review = {"error": f"{type(exc).__name__}: {exc}"}, None
    return {
        "contract_version": "adaptivenet.demo_assessment.v1",
        "generated_at_unix": now,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_age_seconds": payload["slow"]["age_seconds"],
        "data_origin": (snapshot.get("demo") or {}).get("mode", "local_lab"),
        "ready_for_shadow_assessment": payload["ready_for_shadow_assessment"],
        "quality_reasons": payload["slow"]["quality_reasons"],
        "predictor": {"proposal": proposal, "safety_review": review},
        "trained": trained_proposals(payload),
        "detectors": {group: assess_group(payload, group) for group in GROUPS},
        "shadow_only": True,
    }


@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "shadow_only"}


@app.get("/v1/assessment")
def assessment():
    snapshot = Store(data_dir() / "adaptivenet-ingestion.db").latest_snapshot()
    if snapshot is None:
        return JSONResponse({"error": "no feature snapshot yet"}, status_code=404)
    return assess(snapshot)


@app.get("/v1/snapshot")
def latest_snapshot():
    snapshot = Store(data_dir() / "adaptivenet-ingestion.db").latest_snapshot()
    if snapshot is None:
        return JSONResponse({"error": "no feature snapshot yet"}, status_code=404)
    return snapshot
