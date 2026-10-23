"""ML team entry point: predict(model_input) -> adaptivenet.proposal.v1.

The controller (services/adaptivenet/controller/mitigation.py) calls this on
every tick. A proposal is *untrusted input*: the controller re-validates it,
applies hysteresis, cooldown and bounded steps, and verifies recovery. This
module never calls AWS, Kubernetes or the gateway.

Two predictors share the same proposal mapping:
  * trained model, loaded from ADAPTIVENET_MODEL_PATH (a pickle written by
    ml/train.py, holding an estimator with predict_proba and classes_);
  * deterministic rules (ml/rules.py) when no model file is configured.
Both produce one cause per region; `proposal_from_causes` turns that into an action.
"""
from __future__ import annotations
import math
import os
import pickle
from pathlib import Path

from adaptivenet.ingestion.handoff import PROPOSAL_VERSION, FEATURE_COLUMNS
from . import rules

REGIONS = rules.REGIONS
MODEL_ENV = "ADAPTIVENET_MODEL_PATH"
_loaded: dict = {}


def feature_vector(model_input: dict) -> list[float]:
    """34 values (NaN when missing) + 34 masks, same order as training (ml/train.py)."""
    values, masks = [], []
    for region in REGIONS:
        row = model_input["features"][region]
        for name in FEATURE_COLUMNS:
            v = row.get(name)
            values.append(math.nan if v is None else float(v))
            masks.append(1.0 if v is None else 0.0)
    return values + masks


def load_model(path: str | None = None):
    path = path or os.getenv(MODEL_ENV)
    if not path:
        return None
    if _loaded.get("path") != path:
        with open(Path(path), "rb") as handle:
            bundle = pickle.load(handle)
        if not hasattr(bundle.get("estimator"), "predict_proba") or bundle.get("feature_columns") != list(FEATURE_COLUMNS):
            raise ValueError("model bundle does not match the model_input.v1 feature columns")
        _loaded.update(path=path, bundle=bundle)
    return _loaded["bundle"]


def causes_from_model(model_input: dict, bundle: dict) -> dict:
    """Label vocabulary from ml/train.py: 'healthy' or '<region>:<cause>'."""
    estimator = bundle["estimator"]
    probabilities = estimator.predict_proba([feature_vector(model_input)])[0]
    best = max(range(len(probabilities)), key=lambda i: probabilities[i])
    label, confidence = str(estimator.classes_[best]), float(probabilities[best])
    out = {r: {"cause": "healthy", "confidence": confidence, "evidence": [f"model:{label}"]} for r in REGIONS}
    if ":" in label:
        region, cause = label.split(":", 1)
        if region in REGIONS and cause in rules.CAUSES:
            out[region] = {"cause": cause, "confidence": confidence, "evidence": [f"model:{label}"]}
    return out


def proposal_from_causes(model_input: dict, causes: dict, model_id: str) -> dict:
    """Map per-region causes to one proposal. Worst region wins; ties favour region-a."""
    order = {"critical_failure": 3, "network_degradation": 2, "compute_overload": 2, "unknown": 1, "healthy": 0}
    region = max(REGIONS, key=lambda r: order[causes[r]["cause"]])
    verdict = causes[region]
    evidence = [f"{r}:{causes[r]['cause']}:{e}" for r in REGIONS for e in causes[r].get("evidence", [])]
    other = REGIONS[1 - REGIONS.index(region)]
    away = 0 if region == "region-a" else 100  # route weight that sends everything to `other`
    base = {"contract_version": PROPOSAL_VERSION, "snapshot_id": model_input["snapshot_id"],
            "model_id": model_id, "assessment": causes, "affected_region": region,
            "confidence": round(float(verdict["confidence"]), 3), "evidence": evidence}
    reasons = list(model_input.get("quality_reasons", []))
    if "feature_snapshot_expired_or_future_dated" in reasons:
        # Per-view gaps are handled by the cause rules (unknown -> investigate) and by the
        # reviewer's readiness gate; a stale snapshot as a whole is never acted on.
        return {**base, "action": "investigate", "confidence": 0.5, "affected_region": None,
                "evidence": ["model_input_not_ready"] + reasons}
    cause = verdict["cause"]
    if cause == "critical_failure":
        return {**base, "action": "failover", "target_region_a_percent": away,
                "evidence": evidence + [f"destination:{other}:{causes[other]['cause']}"]}
    if cause == "network_degradation":
        return {**base, "action": "shift_traffic", "target_region_a_percent": away,
                "evidence": evidence + [f"destination:{other}:{causes[other]['cause']}"]}
    if cause == "compute_overload":
        return {**base, "action": "scale_out"}
    if cause == "unknown":
        return {**base, "action": "investigate", "confidence": 0.5}
    return {**base, "action": "hold", "affected_region": None}


def predict(model_input: dict) -> dict:
    """Return an adaptivenet.proposal.v1 object for controller review."""
    bundle = load_model()
    if bundle is not None:
        return proposal_from_causes(model_input, causes_from_model(model_input, bundle), bundle.get("model_id", "trained"))
    return proposal_from_causes(model_input, rules.classify(model_input), "dual_view_rules_v1")
