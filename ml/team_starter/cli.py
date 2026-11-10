"""Offline, read-only ML team demo: inspect ablations and run existing shadow baseline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from adaptivenet.ingestion.handoff import conservative_rule_baseline
from .feature_views import GROUPS, project


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True, help="Local model_input.v1 or detection_input.v1 JSON")
    p.add_argument("--group", choices=GROUPS, default="aws_only")
    p.add_argument("--out", type=Path, help="Optional output JSON, never written into source data")
    args = p.parse_args(argv)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    view = project(source, args.group)
    slow = source["slow"] if source.get("contract_version") == "adaptivenet.detection_input.v1" else source
    # Explicitly the EXISTING AWS-rule baseline, not a fitted model or an ablation result.
    proposal = conservative_rule_baseline(slow)
    if not view["ready_for_shadow_assessment"]:
        proposal = {**proposal, "action": "investigate", "confidence": 0.5,
                    "evidence": ["selected_feature_group_not_ready"],
                    "model_id": "unready_hold_v1"}
    result = {
        "starter_version": "adaptivenet.ml_team_starter.v1",
        "feature_group": args.group,
        "feature_count": len(view["values"]),
        "missing_count": sum(view["missing"].values()),
        "ready_for_shadow_assessment": view["ready_for_shadow_assessment"],
        "not_trained_model": True,
        "rule_baseline_uses_aws_inputs_only": True,
        "proposal": proposal,
        "automatic_actions_enabled": False,
    }
    data = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(data, encoding="utf-8")
    else:
        print(data, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
