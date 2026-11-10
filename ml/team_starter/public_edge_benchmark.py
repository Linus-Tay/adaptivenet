"""AdaptiveNet external Edge Fault benchmark — offline, independent of native model.

Uses published fault-injection labels, never manufactures incident labels.
Four application/device runs are disjoint across splits, but the CSV has no
independent episode identifiers or timestamps. This is a limited run-disjoint
benchmark, NOT proof of native AdaptiveNet gray-failure performance.

Run from repo root:
    python -m ml.team_starter.public_edge_benchmark \
        --csv data/public-benchmarks/edge_fault_dataset.csv \
        --out data/ml-results/edge-fault

No AWS calls, no deployment or runtime integration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

FEATURES = (
    "processing_time", "response_time", "cpu_usage", "memory_usage",
    "network_receive", "network_transmit", "curl_exit_code",
    "http_response_status", "http_successful_requests",
    "http_failed_requests", "file_size",
)

# An application/device combination is a complete publisher-described run.
# Do not treat adjacent fault-label blocks as independently verified episodes.
SPLIT = {
    ("object-detection", "asrock"): "train",
    ("speech-recognition", "rpi"): "train",
    ("object-detection", "rpi"): "validation",
    ("speech-recognition", "asrock"): "test",
}
CLASSES = ("healthy", "network_fault", "http_fault", "resource_fault", "pod_failure")


def coarse_label(raw: str) -> str:
    if raw == "no-fault":
        return "healthy"
    if raw.startswith("network-"):
        return "network_fault"
    if raw.startswith("http-"):
        return "http_fault"
    if raw in ("stress-cpu", "stress-ram"):
        return "resource_fault"
    if raw == "pod-failure":
        return "pod_failure"
    raise ValueError(f"Unexpected injected-fault label: {raw!r}")


def numerical(raw: str | None) -> float:
    if raw is None or not raw.strip() or raw.strip().lower() in {"nan", "none", "null", "na"}:
        return float("nan")
    try:
        n = float(raw)
    except ValueError as exc:
        raise ValueError(f"Unexpected non-numeric feature value: {raw!r}") from exc
    return n if math.isfinite(n) else float("nan")


def load_data(path: Path):
    import numpy as np

    arrays = {split: {"features": [], "labels": [], "run": []} for split in ("train", "validation", "test")}
    fault_counts: Counter[str] = Counter()
    run_counts: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError("CSV has no columns")
        required = set(FEATURES) | {"fault_type", "application", "device"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSV missing expected columns: {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            run = (row["application"], row["device"])
            if run not in SPLIT:
                raise ValueError(f"Unrecognized application/device run on row {line}: {run}")
            split = SPLIT[run]
            label = coarse_label(row["fault_type"])
            try:
                x = [numerical(row[name]) for name in FEATURES]
            except ValueError as exc:
                raise ValueError(f"CSV row {line}: {exc}") from exc
            arrays[split]["features"].append(x)
            arrays[split]["labels"].append(label)
            arrays[split]["run"].append("/".join(run))
            fault_counts[row["fault_type"]] += 1
            run_counts["/".join(run)] += 1
    if len(run_counts) != len(SPLIT):
        raise ValueError(f"Expected all four publisher runs; found {dict(run_counts)}")
    for split, partition in arrays.items():
        if not partition["labels"]:
            raise ValueError(f"No rows in {split}")
        partition["features"] = np.asarray(partition["features"], dtype=float)
    return arrays, dict(sorted(fault_counts.items())), dict(sorted(run_counts.items()))


def evaluate(model, partition):
    import numpy as np
    from sklearn.metrics import (accuracy_score, classification_report, confusion_matrix,
                                 precision_recall_fscore_support)

    truth = partition["labels"]
    pred = list(model.predict(partition["features"]))
    # Network versus other conditions, evaluated on the same complete runs.
    net_true = [y == "network_fault" for y in truth]
    net_pred = [y == "network_fault" for y in pred]
    p, r, f1, _ = precision_recall_fscore_support(net_true, net_pred,
                                                 average="binary", zero_division=0)
    false_positives = sum(not t and q for t, q in zip(net_true, net_pred))
    false_negatives = sum(t and not q for t, q in zip(net_true, net_pred))
    output = {
        "n_rows": len(truth), "accuracy": float(accuracy_score(truth, pred)),
        "classification_report": classification_report(truth, pred, labels=list(CLASSES),
                                                       output_dict=True, zero_division=0),
        "confusion_matrix_label_order": list(CLASSES),
        "confusion_matrix": confusion_matrix(truth, pred, labels=list(CLASSES)).tolist(),
        "network_vs_rest": {"precision": float(p), "recall": float(r), "f1": float(f1),
                            "false_positives": int(false_positives), "false_negatives": int(false_negatives)},
    }
    if hasattr(model, "predict_proba"):
        probs = model.predict_proba(partition["features"])
        output["maximum_probability_mean"] = float(np.mean(np.max(probs, axis=1)))
        output["fraction_below_predeclared_0_55_threshold"] = float(np.mean(np.max(probs, axis=1) < 0.55))
        output["probabilities_not_calibrated"] = True
    return output


def run(path: Path, out: Path) -> dict:
    from sklearn.dummy import DummyClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    import joblib

    data, fault_counts, run_counts = load_data(path)
    for name, part in data.items():
        if set(part["labels"]) != set(CLASSES):
            raise ValueError(f"Missing class(es) in {name} split: {sorted(set(CLASSES) - set(part['labels']))}")
    train = data["train"]
    baseline = DummyClassifier(strategy="most_frequent")
    baseline.fit(train["features"], train["labels"])
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scaler", StandardScaler()),
        ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced", random_state=42)),
    ])
    model.fit(train["features"], train["labels"])
    report = {
        "report_version": "adaptivenet.external_benchmark.edge_fault.v1",
        "task": "classification_of_injected_fault_conditions",
        "not_native_adaptivenet_model": True,
        "source": "https://github.com/ssepin/edge-fault-dataset/blob/main/edge_fault_dataset.csv",
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "feature_names": list(FEATURES),
        "classes": list(CLASSES),
        "original_fault_type_counts": fault_counts,
        "run_counts": run_counts,
        "split_run_ids": {name: sorted(set(part["run"])) for name, part in data.items()},
        "split_label_counts": {name: dict(sorted(Counter(part["labels"]).items()))
                               for name, part in data.items()},
        "missing_feature_cells": {
            name: int(__import__("numpy").isnan(part["features"]).sum())
            for name, part in data.items()
        },
        "evaluation": {
            "dummy_most_frequent_test": evaluate(baseline, data["test"]),
            "logistic_validation": evaluate(model, data["validation"]),
            "logistic_test": evaluate(model, data["test"]),
        },
        "forecasting_horizon_seconds": 0,
        "false_alarms_per_day": None,
        "episode_detection_delay_seconds": None,
        "limitations": [
            "CSV has no explicit timestamps or individual fault episode IDs",
            "Split by four publisher-described application/device runs only",
            "Same experiment schedule across runs may cause residual experimental leakage",
            "Fault-type labels identify injections, not independently verified user-impacting degradation",
            "No RIPE/CloudFront regional telemetry; not a native AWS-only versus AWS+RIPE comparison",
            "No defensible false alarms per day or detection delay without time and episode boundaries",
            "Probabilities are uncalibrated and should not be interpreted as reliable confidence",
            "Training and testing limited to one public edge-computing benchmark",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "evaluation-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    # This benchmark artifact MUST NOT be supplied to the native ml/predictor.py interface.
    model_path = out / "external-edge-benchmark-only.joblib"
    joblib.dump(model, model_path)
    print(f"Report: {report_path}")
    print(f"Benchmark-only model (NOT for production/native shadow): {model_path}")
    for name in ("logistic_validation", "logistic_test", "dummy_most_frequent_test"):
        v = report["evaluation"][name]
        n = v["network_vs_rest"]
        print(f"{name}: rows={v['n_rows']}, network precision={n['precision']:.3f}, "
              f"recall={n['recall']:.3f}, F1={n['f1']:.3f}, "
              f"network false positives={n['false_positives']}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/public-benchmarks/edge_fault_dataset.csv"))
    parser.add_argument("--out", type=Path, default=Path("data/ml-results/edge-fault"))
    args = parser.parse_args(argv)
    if not args.csv.is_file():
        parser.error(f"CSV not found: {args.csv}; run from AdaptiveNet repository root")
    run(args.csv, args.out)


if __name__ == "__main__":
    main()
