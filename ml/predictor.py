"""Stable ML entry point. Offline-trained inference is opt-in and shadow-only.

ADAPTIVENET_SHADOW_MODEL must name a LOCAL trusted artifact. Without it the
unchanged conservative rule baseline is used. No AWS or Kubernetes writes.
"""
from __future__ import annotations
import os
from pathlib import Path
from adaptivenet.ingestion.handoff import conservative_rule_baseline


def predict(model_input: dict) -> dict:
    """Return adaptivenet.proposal.v1; independent reviewer retains final veto."""
    model = os.environ.get("ADAPTIVENET_SHADOW_MODEL")
    if not model:
        return conservative_rule_baseline(model_input)
    from ml.team_starter.shadow import shadow_predict
    return shadow_predict(model_input, Path(model))
