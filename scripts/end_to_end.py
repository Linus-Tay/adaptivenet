#!/usr/bin/env python3
"""Proposal -> safety reviewer -> EKS decision (scale, failover or no-op).

``decide`` is a dry run and the default. It reads inputs (and, with a
kubeconfig, read-only HPA state) and prints the decision. It never writes.

``execute`` is the only path that can change AWS. It runs only for a
``scale`` decision, only with ``--approve-live-write <site>`` matching the
planned site, and then hands over to the existing SafetyController EKS loop
(three fresh observations, journal, atomic HPA patch). Failover has no EKS
actuator and is always refused here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT))

from adaptivenet.controller.end_to_end import (  # noqa: E402
    REGIONS, SITE_BY_REGION, decide, map_model_output, observation_from_dict,
)


def _load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def _model_proposal(args) -> dict:
    if args.proposal:
        return _load(args.proposal)
    # Trusted, locally trained artifacts only (pickle). See ml/team_starter/shadow.py.
    from ml.team_starter.shadow import shadow_predict
    return shadow_predict(_load(args.model_input), Path(args.model))


def _observations(args) -> dict:
    observations = {}
    for item in args.hpa or ():
        region, _, path = item.partition("=")
        if region not in REGIONS or not path:
            raise ValueError("--hpa must be region-a=PATH or region-b=PATH")
        observations[region] = observation_from_dict(_load(path))
    for site, kubeconfig in (("sg", args.kubeconfig_sg), ("us", args.kubeconfig_us)):
        if not kubeconfig:
            continue
        from adaptivenet.controller.kubectl_connection import KubectlConnection
        from adaptivenet.controller.kubernetes_hpa import KubernetesHpaReader
        connection = KubectlConnection(site, kubeconfig)
        reader = KubernetesHpaReader({connection.region: connection.target},
                                     readers={connection.target.cluster: connection})
        observations[connection.region] = reader.assess(connection.region).observation
    return observations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("decide", "execute"):
        p = sub.add_parser(name)
        p.add_argument("--snapshot", required=True, help="features.v1 snapshot JSON")
        source = p.add_mutually_exclusive_group(required=True)
        source.add_argument("--proposal", help="proposal.v1 JSON (fixture or model output)")
        source.add_argument("--model", help="trusted local model artifact (.pkl); needs --model-input")
        p.add_argument("--model-input", help="model_input.v1 or detection_input.v1 JSON for --model")
        p.add_argument("--current-region-a-percent", type=int, default=50)
        p.add_argument("--hpa", action="append", metavar="REGION=PATH",
                       help="HPA observation fixture (asdict(HpaObservation) JSON)")
        p.add_argument("--kubeconfig-sg", help="read-only HPA state for region-a")
        p.add_argument("--kubeconfig-us", help="read-only HPA state for region-b")
        p.add_argument("--out", help="also write the decision JSON here")
        if name == "execute":
            p.add_argument("--approve-live-write", metavar="SITE", choices=tuple(SITE_BY_REGION.values()),
                           help="required: the site the user approved for a live HPA change")
            p.add_argument("--journal", default="adaptivenet-eks-actions.db")
            p.add_argument("--proposal-out", default="data/end-to-end/mapped-proposal.json")
            p.add_argument("--max-polls", type=int, default=12)
            p.add_argument("--interval", type=float, default=5)
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.model and not args.model_input:
        parser.error("--model requires --model-input")
    try:
        snapshot = _load(args.snapshot)
        model_proposal = _model_proposal(args)
        decision = decide(snapshot, model_proposal,
                          current_region_a_percent=args.current_region_a_percent,
                          hpa_observations=_observations(args))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"decision": "no-op", "executed": False, "error": str(exc)}), file=sys.stderr)
        return 1
    text = json.dumps(decision, indent=2, allow_nan=False, default=str)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if args.command == "decide":
        return 0
    return _execute(args, snapshot, model_proposal, decision)


def _execute(args, snapshot: dict, model_proposal: dict, decision: dict) -> int:
    if decision["decision"] != "scale":
        reason = ("eks_failover_actuator_not_implemented" if decision["decision"] == "failover"
                  else "nothing_to_execute")
        print(json.dumps({"executed": False, "reason": reason}), file=sys.stderr)
        return 1
    site = decision["plan"]["site"]
    if args.approve_live_write != site:
        print(json.dumps({"executed": False, "reason": "live_write_not_approved_for_site", "site": site}),
              file=sys.stderr)
        return 3
    kubeconfig = args.kubeconfig_sg if site == "sg" else args.kubeconfig_us
    if not kubeconfig:
        print(json.dumps({"executed": False, "reason": f"--kubeconfig-{site}_required"}), file=sys.stderr)
        return 1
    if not 1 <= args.max_polls <= 36 or not 1 <= args.interval <= 10:
        print(json.dumps({"executed": False, "reason": "invalid_poll_bounds"}), file=sys.stderr)
        return 1
    # The EKS loop rereads the proposal file on each poll, so persist the mapped one.
    proposal, _ = map_model_output(snapshot, model_proposal,
                                   current_region_a_percent=args.current_region_a_percent)
    out = Path(args.proposal_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposal, allow_nan=False), encoding="utf-8")
    from adaptivenet.controller.kubectl_connection import KubectlConnection
    from scripts.eks_controller import run_scale
    run_args = argparse.Namespace(snapshot=args.snapshot, proposal=str(out), journal=args.journal,
                                  max_polls=args.max_polls, interval=args.interval)
    return run_scale(run_args, KubectlConnection(site, kubeconfig))


if __name__ == "__main__":
    raise SystemExit(main())
