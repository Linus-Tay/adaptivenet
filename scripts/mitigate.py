#!/usr/bin/env python3
"""One predictive-mitigation cycle: latest snapshot -> predict -> review -> (maybe) act -> verify.

Default mode is shadow (no writes). Every mode reads live route state, so all need LAB_TOKEN and a
running local gateway. --mode enforce also needs INTERNAL_REGION_A_URL/INTERNAL_REGION_B_URL set and
reachable from where this runs; without them recovery can't be verified and every shift rolls back.
Optional Kubernetes scaling: --k8s-targets targets.json (allowlisted deployments per region).
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services")); sys.path.insert(0, str(ROOT))

from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.collectors.internal import TARGETS, collect
from adaptivenet.mitigation.actuators import GatewayActuator, KubernetesActuator
from adaptivenet.mitigation.audit import AuditLog
from adaptivenet.mitigation.orchestrator import Mitigator, Policy

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data/adaptivenet-ingestion.db"))
    ap.add_argument("--audit", default=str(ROOT / "data/mitigation-audit.jsonl"))
    ap.add_argument("--mode", choices=("shadow", "dry_run", "enforce"), default="shadow")
    ap.add_argument("--input-mode", choices=("internal_only", "combined"), default="internal_only")
    ap.add_argument("--k8s-targets", help="JSON {region: {namespace, deployment, hpa?}}")
    ap.add_argument("--settle-seconds", type=float, default=0,
                    help="how long a route change takes to reach clients (0 for the local gateway; DNS TTL for weighted DNS)")
    ap.add_argument("--proposal", help="use this proposal JSON instead of ml.predictor.predict")
    args = ap.parse_args(argv)
    if args.mode == "enforce" and not all(os.getenv(name) for name in TARGETS.values()):
        ap.error("--mode enforce needs " + " and ".join(TARGETS.values()) + " to verify recovery")
    snapshot = Store(args.db).latest_snapshot()
    if snapshot is None:
        ap.error("no snapshot yet; run scripts/ingestion.py")
    model_input = build_model_input(snapshot, mode=args.input_mode)
    if args.proposal:
        proposal = json.loads(Path(args.proposal).read_text())
    else:
        from ml.predictor import predict
        proposal = predict(model_input)
    scaler = KubernetesActuator(json.loads(Path(args.k8s_targets).read_text())) if args.k8s_targets else None
    policy = Policy(mode=args.mode, kill_switch_file=str(ROOT / "data/mitigation.disabled"), input_mode=args.input_mode,
                    actuator_settle_s=args.settle_seconds)
    m = Mitigator(GatewayActuator(scaler=scaler), lambda: asyncio.run(collect()), AuditLog(args.audit), policy)
    print(json.dumps(m.run_cycle(snapshot, proposal), indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
