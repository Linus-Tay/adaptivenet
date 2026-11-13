"""Read-only prediction and independent safety-review demonstration."""
from __future__ import annotations
import pickle
from pathlib import Path
import time

from adaptivenet.ingestion.decisions import review_proposal
from adaptivenet.ingestion.handoff import conservative_rule_baseline
from .feature_views import project
from .dataset import _asof_snapshot


def shadow_predict(payload: dict, model_path: Path) -> dict:
    """Safe model proposal only: hold or investigate; never actuate.

    Load only self-generated trusted local pickle models. Untrusted pickle files
    can execute arbitrary code; do not download unknown model artifacts.
    """
    detection = payload.get("contract_version") == "adaptivenet.detection_input.v1"
    slow = payload.get("slow") if detection else payload
    if not isinstance(slow, dict) or slow.get("contract_version") != "adaptivenet.model_input.v1":
        raise ValueError("expected model_input.v1 or detection_input.v1")
    ident = slow.get("snapshot_id")
    if not isinstance(ident, str) or not ident.startswith("features-"):
        raise ValueError("invalid snapshot_id")
    with model_path.open("rb") as f:
        artifact = pickle.load(f)  # trusted locally generated artifact only
    if artifact.get("artifact_version") != "adaptivenet.offline_model.v1":
        raise ValueError("unrecognized trained artifact")
    import sklearn
    if artifact.get("scikit_learn_version") != sklearn.__version__:
        raise ValueError("inference scikit-learn version mismatch; use training environment")
    model_id, group = artifact["model_id"], artifact["group"]
    def proposal(action, confidence, evidence):
        return {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": ident,
                "action": action, "confidence": confidence, "evidence": evidence,
                "model_id": model_id}
    # Strict live freshness independent of source historical-ready flags.
    stamp = slow.get("computed_at_unix")
    if (isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or
        not 0 <= time.time() - stamp <= 180):
        return proposal("investigate", .5, ["stale_or_invalid_model_input; abstained"])
    # Enforce the same future-source masks used for offline model development.
    cleaned_slow, asof_notes = _asof_snapshot(slow)
    cleaned = {**payload, "slow": cleaned_slow} if detection else cleaned_slow
    view = project(cleaned, group)
    quality = cleaned_slow.get("quality") or {}
    required = ("app_up", "memory_available_bytes", "memory_total_bytes")
    internal_ok = all((quality.get(r) or {}).get("internal") == "fresh"
                      and all(cleaned_slow["features"][r].get(c) is not None for c in required)
                      for r in ("region-a", "region-b"))
    # A "fresh" flag cannot vouch for RIPE values that were just masked as future-dated.
    external_ok = all((quality.get(r) or {}).get("external") == "fresh"
                      and f"masked_future_ripe_source_time:{r}" not in asof_notes
                      for r in ("region-a", "region-b"))
    fresh_ok = internal_ok and (group == "aws_only" or external_ok)
    if group == "aws_ripe_http":
        fresh_ok = fresh_ok and (payload.get("fast") or {}).get("ready_for_fast_assessment") is True
    if not fresh_ok:
        return proposal("investigate", .5, ["source_quality_not_ready; abstained"])
    from .training import vector
    import math
    names = artifact["feature_names"]
    row = {"views": {group: view}}
    proba = artifact["model"].predict_proba([vector(row, group, names)])[0].tolist()
    idx = max(range(len(proba)), key=lambda j: proba[j])
    confidence = float(proba[idx])
    condition = artifact["classes"][idx]
    if not math.isfinite(confidence) or confidence < artifact["min_confidence"]:
        return proposal("investigate", .5, ["prediction_confidence_below_threshold; abstained"])
    action = "hold" if condition in ("healthy", "normal", "recovery", "transient") else "investigate"
    return proposal(action, confidence, [f"shadow_condition:{condition}",
                                          f"feature_group:{group}",
                                          "not_approved_for_execution"])


def shadow_review(snapshot: dict, proposal: dict) -> dict:
    reviewed = review_proposal(snapshot, proposal)
    if reviewed["approved_for_execution"] or reviewed["executed"] or reviewed["effective_action"] != "hold":
        raise AssertionError("independent safety boundary violated")
    if not reviewed["policy_valid"]:
        raise ValueError(f"independent safety reviewer rejected proposal: {reviewed['rejection_reasons']}")
    return reviewed
