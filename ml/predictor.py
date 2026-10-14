"""ML team entry point. Replace the rule baseline with trained inference.

This file is not wired into live routing. Do not call AWS from a model.
"""
from adaptivenet.ingestion.handoff import conservative_rule_baseline


def predict(model_input: dict) -> dict:
    """Return an adaptivenet.proposal.v1 object for controller review."""
    return conservative_rule_baseline(model_input)  # Safe example, NOT trained ML.
