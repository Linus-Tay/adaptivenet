"""Offline ML foundation. No AWS calls, mutations to infrastructure, or generated labels."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .dataset import build_from_files
from .training import train_and_evaluate
from .shadow import shadow_predict, shadow_review
from .paired_incident_report import load_paired_report


def _save(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ds = sub.add_parser("dataset", help="As-of join real snapshot/witness archives to reviewed labels")
    ds.add_argument("--snapshots", type=Path, required=True)
    ds.add_argument("--witness", type=Path, required=True)
    ds.add_argument("--labels", type=Path, required=True)
    ds.add_argument("--out", type=Path, required=True)
    train = sub.add_parser("train", help="Fit only with sufficient verified episodes")
    train.add_argument("--dataset", type=Path, required=True)
    train.add_argument("--group", choices=("aws_only", "aws_ripe", "aws_ripe_http", "all"), default="all")
    train.add_argument("--out", type=Path, required=True)
    train.add_argument("--embargo-seconds", type=float, default=900)
    pair = sub.add_parser("compare", help="Compare native trained arms on identical held-out episodes")
    pair.add_argument("--dataset", type=Path, required=True)
    pair.add_argument("--predictions-dir", type=Path, required=True)
    pair.add_argument("--out", type=Path, required=True)
    smoke = sub.add_parser("shadow", help="Invoke local trusted model and independent safety reviewer")
    smoke.add_argument("--input", type=Path, required=True)
    smoke.add_argument("--model", type=Path, required=True)
    smoke.add_argument("--snapshot", type=Path, required=True, help="Matching original feature snapshot (required for independent review)")
    smoke.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "dataset":
        obj = build_from_files(args.snapshots, args.witness, args.labels)
        _save(args.out, obj)
        print(f"Labeled {len(obj['rows'])} real rows; skipped {obj['skipped_unlabeled_snapshots']} unlabeled snapshots")
        return 0
    if args.command == "train":
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
        groups = ("aws_only", "aws_ripe", "aws_ripe_http") if args.group == "all" else (args.group,)
        results = {g: train_and_evaluate(dataset, args.out, group=g,
                                         embargo_seconds=args.embargo_seconds) for g in groups}
        _save(args.out / "evaluation-report.json", results)
        for g, res in results.items():
            print(g, res["status"], "; ".join(res.get("blockers", [])))
        return 0 if any(res["status"] == "trained" for res in results.values()) else 2
    if args.command == "compare":
        report = load_paired_report(args.dataset, args.predictions_dir)
        _save(args.out, report)
        print(report["status"], "; ".join(report.get("blockers", [])))
        return 0 if report["status"] == "evaluated" else 2
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    proposal = shadow_predict(payload, args.model)
    result = {"proposal": proposal, "shadow_only": True}
    snap = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result["independent_review"] = shadow_review(snap, proposal)
    if args.out:
        _save(args.out, result)
    else:
        print(json.dumps(result, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
