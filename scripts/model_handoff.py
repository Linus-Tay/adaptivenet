#!/usr/bin/env python3
"""Simple local teammate CLI: model input, conservative demo, policy review.

All commands are read-only except --out writing a local JSON file. No AWS calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))

from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.handoff import build_model_input, conservative_rule_baseline
from adaptivenet.ingestion.decisions import review_proposal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("input", "demo", "review"))
    parser.add_argument("--db", default=str(ROOT / "data/adaptivenet-ingestion.db"))
    parser.add_argument("--mode", choices=("internal_only", "combined"), default="internal_only")
    parser.add_argument("--proposal", help="Model-provided JSON proposal, required for review")
    parser.add_argument("--out", help="Write JSON file (default: stdout)")
    args = parser.parse_args(argv)
    snapshot = Store(args.db).latest_snapshot()
    if snapshot is None:
        parser.error("no snapshot yet; run python scripts/ingestion.py once")

    if args.command == "review":
        if not args.proposal:
            parser.error("--proposal JSON file required")
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        result = review_proposal(snapshot, proposal)
    else:
        model_input = build_model_input(snapshot, mode=args.mode)
        if args.command == "input":
            result = model_input
        else:
            proposal = conservative_rule_baseline(model_input)
            result = {
                "snapshot_id": snapshot["snapshot_id"],
                "mode": args.mode,
                "ready_for_inference": model_input["ready_for_inference"],
                "proposal": proposal,
                "decision_review": review_proposal(snapshot, proposal),
            }
    data = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        print("Wrote", path)
    else:
        print(data, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
