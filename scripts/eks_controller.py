#!/usr/bin/env python3
"""Check the safety controller connection to an existing EKS workload."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from adaptivenet.controller.kubectl_connection import KubectlConnection
from adaptivenet.controller.kubernetes_hpa import KubernetesHpaReader, KubernetesReadError

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "scale"))
    parser.add_argument("--site", required=True, choices=("sg", "us"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--snapshot", help="Fresh features.v1 JSON for a scale request")
    parser.add_argument("--proposal", help="Matching scale_out proposal JSON")
    parser.add_argument("--journal", default="adaptivenet-eks-actions.db")
    parser.add_argument("--max-polls", type=int, default=12)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if args.command == "scale":
        if not args.snapshot or not args.proposal:
            parser.error("scale requires --snapshot and --proposal")
        if not 1 <= args.max_polls <= 36 or not 1 <= args.interval <= 10:
            parser.error("max-polls must be 1..36 and interval 1..10 seconds")
    try:
        connection = KubectlConnection(args.site, args.kubeconfig)
        if args.command == "scale":
            return run_scale(args, connection)
        reader = KubernetesHpaReader({connection.region: connection.target},
                                    readers={connection.target.cluster: connection})
        result = reader.assess(connection.region)
        connected = result.observation is not None
        output = {"connected": connected, "executed": False,
                  "cluster": connection.target.cluster, "region": connection.region,
                  "capacity": asdict(result)}
    except (KubernetesReadError, ValueError, OSError) as exc:
        connected = False
        output = {"connected": False, "executed": False, "error": str(exc)}
    print(json.dumps(output, indent=2, allow_nan=False))
    return 0 if connected else 1

def run_scale(args, connection):
    import time
    from adaptivenet.controller.journal import ActionJournal
    from adaptivenet.controller.live_hpa import LiveHpa
    from adaptivenet.controller.safety import SafetyController
    journal = ActionJournal(args.journal)
    adapter = LiveHpa(connection, journal)
    controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
    for index in range(args.max_polls):
        # Read originals again so a producer can atomically replace them with new evidence.
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        observation = adapter.observe()
        overloaded = (type(observation.cpu_current_pct) is int and type(observation.cpu_target_pct) is int
                      and observation.cpu_current_pct >= observation.cpu_target_pct)
        result = controller.step_scale(snapshot, proposal, compute_overloaded=overloaded)
        print(json.dumps(asdict(result), allow_nan=False), flush=True)
        if result.state in ("blocked", "verifying", "cooldown"):
            return 0 if result.state == "verifying" else 1
        if index + 1 < args.max_polls:
            time.sleep(args.interval)
    return 2  # Evidence/readiness incomplete; retain the journal for reconciliation.

if __name__ == "__main__":
    raise SystemExit(main())
