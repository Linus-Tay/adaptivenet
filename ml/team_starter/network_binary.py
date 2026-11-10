"""Evidence-gated, offline NETWORK-degradation detector (separate from application health).

No AWS calls, no fabricated positive labels. Labels come from a reviewed network
annotation file, not from HTTP errors, fault commands, or a failed RIPE probe.
Train/validation/test split by complete experiment runs, chronological embargo.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import pickle
import statistics

from .feature_views import GROUPS
from .training import vector
from .verified_labels import unix, validate_manifest

REVIEW_VERSION = "adaptivenet.network_binary_reviews.v1"
DATASET_VERSION = "adaptivenet.offline_dataset.v1"
TARGETS = ("normal", "network_degradation")


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def validate_reviews(reviews: dict, verified_manifest: dict) -> tuple[dict, list[str]]:
    """Reviewer must explicitly establish *network* state, independently from app labels."""
    originals = {e["episode_id"]: e for e in validate_manifest(verified_manifest)}
    if reviews.get("contract_version") != REVIEW_VERSION or not isinstance(reviews.get("reviews"), list):
        raise ValueError("invalid network_binary_reviews contract")
    output = {}
    warnings = []
    for item in reviews["reviews"]:
        if not isinstance(item, dict):
            raise ValueError("network review must be an object")
        ident = item.get("episode_id")
        if not isinstance(ident, str) or ident not in originals or ident in output:
            raise ValueError("unknown or duplicate reviewed episode_id")
        original = originals[ident]
        target = item.get("network_state")
        if target not in TARGETS:
            raise ValueError("network_state must be normal or network_degradation")
        if item.get("reviewed") is not True:
            raise ValueError(f"network-specific review required: {ident}")
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip():
            raise ValueError(f"named reviewer missing: {ident}")
        try:
            reviewed_at = unix(item.get("reviewed_at_utc"))
        except ValueError as exc:
            raise ValueError(f"reviewed_at_utc missing/invalid: {ident}") from exc
        if reviewed_at < original["start_unix"]:
            raise ValueError(f"review date precedes incident: {ident}")
        refs = item.get("network_evidence_refs")
        if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or not x.strip() for x in refs):
            raise ValueError(f"network-specific evidence references missing: {ident}")
        # One Windows/Mac HTTP client is only one network vantage, regardless
        # of how many regional endpoints were contacted from that client.
        vantage = item.get("independent_network_vantage_count")
        if type(vantage) is not int or vantage < 2:
            raise ValueError(f"need >=2 independent network vantages: {ident}")
        if target == "network_degradation":
            if original["label"] == "healthy":
                raise ValueError(f"contradictory healthy and degraded reviews: {ident}")
            if item.get("observed_network_impact_verified") is not True:
                raise ValueError(f"verified network impact required: {ident}")
        elif original["label"] in ("network_degradation", "partial_outage"):
            raise ValueError(f"cannot silently relabel existing incident as normal: {ident}")
        run_id = item.get("experiment_run_id", original.get("experiment_run_id"))
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"experiment_run_id required to prevent phase leakage: {ident}")
        if original.get("experiment_run_id") and run_id != original["experiment_run_id"]:
            raise ValueError(f"experiment_run_id disagrees with source manifest: {ident}")
        output[ident] = {"label": target, "experiment_run_id": run_id,
                         "network_evidence_refs": refs, "reviewer": item["reviewer"],
                         "vantages": vantage, "reviewed_at_unix": reviewed_at}
    if not output:
        warnings.append("No network-specific reviews: do not infer network-normal from application success")
    return output, warnings


def task_data(dataset: dict, manifest: dict, reviews: dict) -> dict:
    """Only include source rows whose network state was separately reviewed."""
    if dataset.get("contract_version") != DATASET_VERSION:
        raise ValueError("not an offline_dataset.v1")
    originals = {e["episode_id"]: e for e in validate_manifest(manifest)}
    annotations, notes = validate_reviews(reviews, manifest)
    out = []
    skipped = Counter()
    seen: dict[str, str] = {}
    for row in dataset.get("rows", []):
        eid = row.get("episode_id")
        if eid not in originals:
            raise ValueError(f"dataset episode absent from reviewed manifest: {eid}")
        episode = originals[eid]
        if row.get("label") != episode["label"]:
            raise ValueError(f"label mismatch versus reviewed source: {eid}")
        stamp = unix(row.get("at_unix"))
        if not episode["start_unix"] <= stamp < episode["end_unix"]:
            raise ValueError(f"snapshot outside reviewed evidence interval: {eid}")
        if eid not in annotations:
            skipped["network_review_missing"] += 1
            continue
        # A snapshot is one feature observation even if multiple reviewed
        # regional labels overlap. Reject duplicates with ambiguous targets.
        key = row.get("snapshot_id")
        if key in seen:
            if seen[key] != annotations[eid]["label"]:
                raise ValueError(f"conflicting network states for same feature snapshot: {key}")
            skipped["duplicate_snapshot_id"] += 1
            continue
        seen[key] = annotations[eid]["label"]
        if not isinstance(row.get("views"), dict) or not all(g in row["views"] for g in GROUPS):
            raise ValueError(f"missing feature-group projections: {eid}")
        original = annotations[eid]
        out.append({**row, "source_label": row["label"],
                    "label": original["label"], "experiment_run_id": original["experiment_run_id"]})
    episodes = [{"episode_id": e["episode_id"],
                 "experiment_run_id": annotations[e["episode_id"]]["experiment_run_id"],
                 "label": annotations[e["episode_id"]]["label"],
                 "start_unix": e["start_unix"], "end_unix": e["end_unix"]}
                for e in originals.values() if e["episode_id"] in annotations]
    return {"rows": out, "episodes": episodes, "reviews": annotations,
            "skipped": dict(skipped), "warnings": notes,
            "network_verified_class_counts": dict(Counter(e["label"] for e in episodes)),
            "limitations": ["single_http_vantage_not_independent_network_confirmation",
                            "existing_archived_snapshot_source_receipt_limitations",
                            "no_forecasting_horizon; detection_only"]}


def chronological_run_split(episodes: list[dict], embargo_seconds: float = 900) -> dict:
    """Never split one run's phases; also merge overlapping runs in time."""
    if embargo_seconds < 0:
        raise ValueError("embargo must be >= 0")
    runs = {}
    for ep in episodes:
        run = ep["experiment_run_id"]
        if run not in runs:
            runs[run] = {"start": ep["start_unix"], "end": ep["end_unix"], "ids": [ep["episode_id"]]}
        else:
            runs[run]["start"] = min(runs[run]["start"], ep["start_unix"])
            runs[run]["end"] = max(runs[run]["end"], ep["end_unix"])
            runs[run]["ids"].append(ep["episode_id"])
    blocks = []
    for one in sorted(runs.values(), key=lambda v: (v["start"], v["end"])):
        if blocks and one["start"] < blocks[-1]["end"]:
            blocks[-1]["end"] = max(blocks[-1]["end"], one["end"])
            blocks[-1]["ids"].extend(one["ids"])
        else:
            blocks.append({**one, "ids": list(one["ids"])})
    if len(blocks) < 3:
        raise ValueError("need >=3 independent chronological experiment blocks")
    n = len(blocks)
    cut1 = max(1, int(.6 * n))
    cut2 = min(n - 1, max(cut1 + 1, int(.8 * n)))
    groups = [blocks[:cut1], blocks[cut1:cut2], blocks[cut2:]]
    excluded = []
    for j in (1, 2):
        end = groups[j-1][-1]["end"]
        while groups[j] and groups[j][0]["start"] - end < embargo_seconds:
            excluded.extend(groups[j].pop(0)["ids"])
        if not groups[j]:
            raise ValueError("not enough separated experiment runs after embargo")
    split = {k: [eid for block in arr for eid in block["ids"]]
             for k, arr in zip(("train", "validation", "test"), groups)}
    split["excluded_for_embargo"] = excluded
    positions = {eid: k for k in ("train", "validation", "test") for eid in split[k]}
    for run, value in runs.items():
        if len({positions[eid] for eid in value["ids"] if eid in positions}) > 1:
            raise AssertionError(f"experiment run leakage: {run}")
    return split


def rule_candidate(row: dict, group: str) -> str | None:
    """Heuristic only: candidate, never reviewer-confirmed network root cause."""
    view = row["views"][group]
    if not view.get("ready_for_shadow_assessment"):
        return None
    v = view["values"]
    for r in ("region-a", "region-b"):
        app_up = v.get(f"{r}.app_up")
        if app_up is None or app_up == 0:
            return None  # Application failure can mimic network failure.
    if group != "aws_only":
        for r in ("region-a", "region-b"):
            # Aggregate loss can reflect one permanently unreachable probe.
            # Hence this is ONLY a baseline candidate, not evidence or a label.
            loss, n = v.get(f"{r}.packet_loss_pct"), v.get(f"{r}.ripe_probes_reporting")
            if loss is not None and n is not None and n >= 3 and loss >= 50:
                return "network_degradation"
    else:
        for r in ("region-a", "region-b"):
            latency, err = v.get(f"{r}.http_p95_ms"), v.get(f"{r}.http_error_ratio")
            if latency is not None and err is not None and latency >= 750 and err < .2:
                return "network_degradation"  # Hypothesis, not root-cause proof.
    return "normal"


def score(rows: list[dict], predictions: list[str | None], onset: dict[str, float]) -> dict:
    if len(rows) != len(predictions):
        raise ValueError("row/prediction mismatch")
    if not rows:
        return {"status": "no_verified_rows", "samples": 0}
    positive = "network_degradation"
    tp = sum(r["label"] == positive and p == positive for r, p in zip(rows, predictions))
    fp = sum(r["label"] != positive and p == positive for r, p in zip(rows, predictions))
    fn = sum(r["label"] == positive and p != positive for r, p in zip(rows, predictions))
    tn = sum(r["label"] == "normal" and p == "normal" for r, p in zip(rows, predictions))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    by_ep = defaultdict(list)
    for r, p in zip(rows, predictions):
        by_ep[r["episode_id"]].append((r["at_unix"], p, r["label"]))
    delays, misses, false_transitions, healthy_seconds = [], 0, 0, 0.0
    for eid, items in by_ep.items():
        items.sort()
        actual = items[0][2]
        if actual == positive:
            hits = [t for t, p, _ in items if p == positive]
            if hits:
                delays.append(max(0., hits[0] - onset[eid]))
            else:
                misses += 1
        else:
            prev = False
            for _, p, _ in items:
                alert = p == positive
                if alert and not prev:
                    false_transitions += 1
                prev = alert
            if len(items) >= 2:
                healthy_seconds += items[-1][0] - items[0][0]
    return {"status": "evaluated", "samples": len(rows), "positive_samples": tp+fn,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0,
            "abstentions": sum(p is None for p in predictions),
            "coverage": sum(p is not None for p in predictions)/len(predictions),
            "missed_incidents": misses,
            "detection_delay_median_seconds": statistics.median(delays) if delays else None,
            "false_alert_transitions": false_transitions,
            "false_alerts_per_24h_observed_normal": false_transitions*86400/healthy_seconds if healthy_seconds else None,
            "observed_normal_hours": healthy_seconds/3600,
            "limitations": ["false_alarm_rate_denominator_is_observed_normal_time",
                            "abstentions_count_as_missed_for_recall"]}


def run(dataset: dict, manifest: dict, reviews: dict, out: Path, *, embargo_seconds: float = 900,
        min_confidence: float = .55) -> dict:
    if not 0.5 <= min_confidence < 1:
        raise ValueError("invalid confidence threshold")
    task = task_data(dataset, manifest, reviews)
    report = {"report_version": "adaptivenet.network_binary_evaluation.v1",
              "task": "binary_network_degradation_detection", "forecasting_horizon_seconds": 0,
              "status": "not_trained", "source_rows": len(dataset.get("rows", [])),
              "network_labeled_rows": len(task["rows"]),
              "network_verified_class_counts": task["network_verified_class_counts"],
              "skipped": task["skipped"], "limitations": task["limitations"] + task["warnings"],
              "groups": {}, "blockers": []}
    classes = task["network_verified_class_counts"]
    if classes.get("network_degradation", 0) < 4:
        report["blockers"].append("need >=4 independently reviewed network-degradation episodes across train/validation/test")
    if classes.get("normal", 0) < 4:
        report["blockers"].append("need >=4 independently reviewed network-normal episodes across train/validation/test")
    try:
        split = chronological_run_split(task["episodes"], embargo_seconds)
    except ValueError as exc:
        report["blockers"].append(str(exc))
        split = None
    if split:
        report["split"] = split
        sets = {p: sorted((r for r in task["rows"] if r["episode_id"] in set(split[p])),
                          key=lambda r: (r["at_unix"], r["episode_id"]))
                for p in ("train", "validation", "test")}
        report["partition_rows"] = {p: len(sets[p]) for p in sets}
        for p in sets:
            counts = Counter(r["experiment_run_id"] for r in sets[p] if r["label"] == "network_degradation")
            normals = Counter(r["experiment_run_id"] for r in sets[p] if r["label"] == "normal")
            if p == "train" and (len(counts) < 2 or len(normals) < 2):
                report["blockers"].append("training needs >=2 separate experiment runs per network class")
            if p != "train" and (not counts or not normals):
                report["blockers"].append(f"{p} must have real network-positive AND network-normal rows")
        if len(sets["train"]) < 10:
            report["blockers"].append("need >=10 reviewed training snapshots")
        onset = {e["episode_id"]: e["start_unix"] for e in task["episodes"]}
        for group in GROUPS:
            report["groups"][group] = {"fixed_rule_test": score(sets["test"],
                [rule_candidate(r, group) for r in sets["test"]], onset)}
    if report["blockers"]:
        return report
    try:
        import sklearn
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        report["blockers"] = ["install optional sklearn dependency: python -m pip install -e '.[ml]'"]
        return report
    for group in GROUPS:
        eligible = {p: [r for r in sets[p] if r["views"][group]["ready_for_shadow_assessment"]]
                    for p in sets}
        if (len(eligible["train"]) < 10 or
            len({r["experiment_run_id"] for r in eligible["train"] if r["label"] == "network_degradation"}) < 2 or
            len({r["experiment_run_id"] for r in eligible["train"] if r["label"] == "normal"}) < 2):
            report["groups"][group]["status"] = "not_trained"
            report["groups"][group]["blockers"] = ["insufficient fresh, reviewed training rows per class"]
            continue
        names = sorted(eligible["train"][0]["views"][group]["values"])
        model = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                          ("scale", StandardScaler()),
                          ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced", random_state=42))])
        model.fit([vector(r, group, names) for r in eligible["train"]],
                  [r["label"] for r in eligible["train"]])
        classes0 = list(model.classes_)
        def predict(rows0):
            result, probabilities = [], []
            for row in rows0:
                if not row["views"][group]["ready_for_shadow_assessment"]:
                    result.append(None); probabilities.append(None); continue
                p = model.predict_proba([vector(row, group, names)])[0].tolist()
                j = max(range(len(p)), key=lambda idx: p[idx])
                result.append(classes0[j] if p[j] >= min_confidence else None)
                probabilities.append(p)
            return result, probabilities
        # Validation is diagnostic; no threshold tuning on held-out test.
        val_preds, _ = predict(sets["validation"])
        test_preds, test_prob = predict(sets["test"])
        test_positive = {r["experiment_run_id"] for r in sets["test"] if r["label"] == "network_degradation"}
        trained_id = sha256(json.dumps({"group": group, "features": names, "labels": classes0,
            "train_runs": sorted({r["experiment_run_id"] for r in eligible["train"]}),
            "task": "network_binary_v1"}, sort_keys=True).encode()).hexdigest()[:16]
        model_id = f"logreg-{group}-{trained_id}"
        artifact = {"artifact_version": "adaptivenet.offline_model.v1", "model_id": model_id,
                    "group": group, "feature_names": names, "classes": classes0,
                    "min_confidence": min_confidence, "model": model,
                    "trained_on_episode_ids": split["train"], "forecast_horizon_seconds": 0,
                    "task": "binary_network_degradation_detection", "scikit_learn_version": sklearn.__version__}
        out.mkdir(parents=True, exist_ok=True)
        location = out / f"{model_id}.pkl"
        with location.open("wb") as fh:
            pickle.dump(artifact, fh, protocol=pickle.HIGHEST_PROTOCOL)
        report["groups"][group].update({"status": "trained", "model_id": model_id,
            "model_artifact": str(location), "validation": score(sets["validation"], val_preds, onset),
            "test": score(sets["test"], test_preds, onset),
            "heldout_network_failure_runs": len(test_positive),
            "test_brier_probability": (statistics.mean((p[classes0.index("network_degradation")] -
                (1 if row["label"] == "network_degradation" else 0))**2
                for row, p in zip(sets["test"], test_prob) if p is not None)
                if any(p is not None for p in test_prob) else None)})
    report["status"] = "trained" if any(x.get("status") == "trained" for x in report["groups"].values()) else "not_trained"
    if report["status"] != "trained":
        report["blockers"].append("no feature group had sufficient fresh/ready training data")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/ml-archive/verified-dataset.json"))
    parser.add_argument("--labels", type=Path, default=Path("data/verified_episodes.json"))
    parser.add_argument("--reviews", type=Path, help="separately approved network-specific reviews; optional for gap audit")
    parser.add_argument("--out", type=Path, default=Path("data/ml-results/native/network-binary"))
    parser.add_argument("--embargo-seconds", type=float, default=900)
    parser.add_argument("--require-trained", action="store_true",
                        help="CI: return code 2 if verified evidence insufficient")
    args = parser.parse_args(argv)
    dataset, manifest = _load(args.dataset), _load(args.labels)
    reviews = _load(args.reviews) if args.reviews and args.reviews.exists() else {"contract_version": REVIEW_VERSION, "reviews": []}
    if args.reviews and not args.reviews.exists():
        raise FileNotFoundError(args.reviews)
    report = run(dataset, manifest, reviews, args.out, embargo_seconds=args.embargo_seconds)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "network-detection-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(f"NETWORK DETECTION: {report['status']} | network-reviewed rows: {report['network_labeled_rows']} | classes: {report['network_verified_class_counts']}")
    for warning in report.get("blockers", []):
        print("GAP:", warning)
    for group, detail in report["groups"].items():
        print(group, detail.get("status", "baseline_only"),
              ("F1="+str(round(detail["test"]["f1"],3))) if detail.get("test") else "")
    print("Report:", path)
    return 0 if report["status"] == "trained" or not args.require_trained else 2


if __name__ == "__main__":
    raise SystemExit(main())
