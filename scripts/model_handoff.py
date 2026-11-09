#!/usr/bin/env python3
"""Model input, conservative demo, policy review, and independent shadow assessment.

Shadow assessment may read configured Kubernetes endpoints and never attaches an actuator.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import json
import sqlite3
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))

from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.handoff import build_model_input, conservative_rule_baseline
from adaptivenet.ingestion.decisions import review_proposal


def _shadow_snapshot(args) -> dict | None:
    if args.snapshot:
        return json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    uri = Path(args.db).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("SELECT body_json FROM feature_snapshots ORDER BY computed_at DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def _configured_hpa(config_path: Path, stack: ExitStack):
    import httpx
    from adaptivenet.controller.kubernetes_shadow import KubernetesShadowHpa
    from adaptivenet.controller.scaling import WorkloadTarget

    config = json.loads(config_path.read_text(encoding="utf-8"))
    allowlist = {region: WorkloadTarget(**target) for region, target in config["workloads"].items()}
    clients = {}
    for cluster in {target.cluster for target in allowlist.values()}:
        settings = config["clusters"][cluster]
        endpoint = urlsplit(settings["server"])
        if (
            endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
        ):
            raise ValueError("expected an HTTPS Kubernetes API origin")
        token = (config_path.parent / settings["token_file"]).read_text(encoding="utf-8").strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("invalid token file")
        ca_file = config_path.parent / settings["ca_file"]
        clients[cluster] = stack.enter_context(httpx.Client(
            base_url=settings["server"], timeout=5, trust_env=False,
            verify=ssl.create_default_context(cafile=str(ca_file)),
            headers={"Authorization": f"Bearer {token}"},
        ))
    return KubernetesShadowHpa(allowlist, clients)


def _run_shadow(args, parser) -> dict:
    from adaptivenet.controller.shadow import assess_shadow

    try:
        snapshot = _shadow_snapshot(args)
        if snapshot is None:
            parser.error("no feature snapshot available")
        if args.proposal:
            proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))

            def predict(_model_input):
                return proposal
        else:
            sys.path.insert(0, str(ROOT))
            from ml.predictor import predict
        with ExitStack() as stack:
            hpa = _configured_hpa(Path(args.hpa_config), stack) if args.hpa_config else None
            return assess_shadow(
                snapshot, predict, mode=args.mode, hpa=hpa,
                current_region_a_percent=args.current_a_percent,
            )
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError):
        parser.error("cannot read the snapshot, proposal, or HPA configuration")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("input", "demo", "review", "shadow"))
    parser.add_argument("--db", default=str(ROOT / "data/adaptivenet-ingestion.db"))
    parser.add_argument("--mode", choices=("internal_only", "combined"), default="internal_only")
    parser.add_argument("--proposal", help="Model-provided JSON proposal, required for review and optional for shadow")
    parser.add_argument("--snapshot", help="Read a features.v1 JSON file instead of SQLite in shadow mode")
    parser.add_argument("--current-a-percent", type=int, help="Observed route weight for a shadow shift review")
    parser.add_argument("--hpa-config", help="Allowlisted Kubernetes API connections for shadow scale review")
    parser.add_argument("--out", help="Write JSON file (default: stdout)")
    args = parser.parse_args(argv)
    if args.command == "shadow":
        result = _run_shadow(args, parser)
    else:
        snapshot = Store(args.db).latest_snapshot()
        if snapshot is None:
            parser.error("no snapshot yet; run python scripts/ingestion.py once")

    if args.command == "review":
        if not args.proposal:
            parser.error("--proposal JSON file required")
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        result = review_proposal(snapshot, proposal)
    elif args.command != "shadow":
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
