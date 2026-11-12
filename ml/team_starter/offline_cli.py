"""Offline ML foundation. No AWS calls, mutations to infrastructure, or generated labels."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .dataset import build_from_files, read_jsonl, unlabeled_window_rows
from .verified_labels import unix
from .training import train_and_evaluate
from .shadow import shadow_predict, shadow_review
from .paired_incident_report import load_paired_report


def _save(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def replay_window(models: list[Path], snapshots: list[dict], witnesses: list[dict],
                  start: float, end: float) -> dict:
    """Count what each trusted local model predicts over an unreviewed window.

    Output is descriptive only: the window has no reviewed label, so these are
    not errors or false alarms until a human reviews the window.
    """
    import pickle
    from collections import Counter
    from .training import vector
    rows = unlabeled_window_rows(snapshots, witnesses, start, end)
    counts = {}
    for path in models:
        with path.open("rb") as f:
            artifact = pickle.load(f)  # trusted locally generated artifact only
        group, c = artifact["group"], Counter()
        for row in rows:
            if not row["views"][group]["ready_for_shadow_assessment"]:
                c["abstain_inputs_not_ready"] += 1
                continue
            proba = artifact["model"].predict_proba([vector(row, group, artifact["feature_names"])])[0]
            i = max(range(len(proba)), key=lambda j: proba[j])
            c[str(artifact["classes"][i]) if proba[i] >= artifact["min_confidence"] else "abstain_low_confidence"] += 1
        counts[artifact["model_id"]] = dict(sorted(c.items()))
    return {"report_version": "adaptivenet.unreviewed_window_replay.v1", "window_start_unix": start,
            "window_end_unix": end, "snapshots": len(rows), "prediction_counts": counts,
            "scored": False, "note": "window is unreviewed; counts are not errors or false alarms"}


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
    train.add_argument("--split", choices=("chronological", "class_stratified"), default="chronological",
                       help="class_stratified holds out the latest run of each class (weaker than chronological)")
    pair = sub.add_parser("compare", help="Compare native trained arms on identical held-out episodes")
    pair.add_argument("--dataset", type=Path, required=True)
    pair.add_argument("--predictions-dir", type=Path, required=True)
    pair.add_argument("--out", type=Path, required=True)
    rep = sub.add_parser("replay", help="Prediction counts of trained models over an UNREVIEWED window (never a score)")
    rep.add_argument("--models", type=Path, nargs="+", required=True)
    rep.add_argument("--snapshots", type=Path, required=True)
    rep.add_argument("--witness", type=Path, required=True)
    rep.add_argument("--start-utc", required=True)
    rep.add_argument("--end-utc", required=True)
    rep.add_argument("--out", type=Path, required=True)
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
                                         embargo_seconds=args.embargo_seconds,
                                         split_strategy=args.split) for g in groups}
        _save(args.out / "evaluation-report.json", results)
        for g, res in results.items():
            print(g, res["status"], "; ".join(res.get("blockers", [])))
        return 0 if any(res["status"] == "trained" for res in results.values()) else 2
    if args.command == "compare":
        report = load_paired_report(args.dataset, args.predictions_dir)
        _save(args.out, report)
        print(report["status"], "; ".join(report.get("blockers", [])))
        return 0 if report["status"] == "evaluated" else 2
    if args.command == "replay":
        report = replay_window(args.models, read_jsonl(args.snapshots), read_jsonl(args.witness),
                               unix(args.start_utc), unix(args.end_utc))
        _save(args.out, report)
        for model, counts in report["prediction_counts"].items():
            print(model, counts)
        return 0
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
