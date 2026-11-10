"""SOFI public network-fault benchmark — offline; NOT a native AdaptiveNet model.

Usage from the AdaptiveNet repo root:
    python -m ml.team_starter.public_sofi_benchmark --core-i "data/public-benchmarks/sofi/tc6ysmh5j8-2/normalized/SOFI CoreSwitch-I.csv" --core-ii "data/public-benchmarks/sofi/tc6ysmh5j8-2/normalized/SOFI CoreSwitch-II.csv" --out data/ml-results/sofi

This benchmark uses timestamp-aligned readings from both core switches and a
chronological 60/20/20 split. Published 'F' means documented faulty condition,
not independently verified AdaptiveNet/Internet gray failure. No AWS calls.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Explicitly exclude timestamp, range, source class and Device_uptime as inputs:
# they can encode observation period rather than network symptoms. Counters and
# interface states are observations at that timestamp (no future values).
SENSORS = (
    "ICMP_ping", "ICMP_loss", "ICMP_response_time", "SNMP_availability",
    "P_Operational_status", "EX_Operational_status", "IN_Operational_status",
    "P_Inbound_packets_discarded", "EX_Inbound_packets_discarded",
    "P_Inbound_packets_with_errors", "EX_Inbound_packets_with_errors",
    "P_Outbound_packets_discarded", "EX_Outbound_packets_discarded",
    "P_Outbound_packets_with_errors", "EX_Outbound_packets_with_errors",
    "IN_Inbound_packets_discarded", "IN_Inbound_packets_with_errors",
    "IN_Outbound_packets_discarded", "IN_Outbound_packets_with_errors",
)


def read_switch(path: Path):
    """Parse LF, CRLF, or original CR-only CSV; preserve original timestamps."""
    output = {}
    with path.open("r", newline=None, encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"Missing header: {path}")
        columns = {name.lower(): name for name in reader.fieldnames}
        required = {"timestamp", "class"} | {name.lower() for name in SENSORS}
        absent = sorted(required - set(columns))
        if absent:
            raise ValueError(f"Missing columns {absent} in {path}")
        for row in reader:
            timestamp = int(row[columns["timestamp"]])
            label = row[columns["class"]].strip()
            if label not in {"F", "NE"}:
                raise ValueError(f"Unexpected class {label!r} at {timestamp} in {path}")
            if timestamp in output:
                raise ValueError(f"Duplicate timestamp {timestamp} in {path}")
            features = []
            for name in SENSORS:
                raw = row[columns[name.lower()]].strip()
                if raw == "" or raw.lower() in {"nan", "none", "null"}:
                    val = float("nan")
                else:
                    val = float(raw)
                    # SOFI uses -1 in sensor fields for unobserved/invalid
                    # readings; never confuse it with a true 0 measurement.
                    if val == -1 or not math.isfinite(val):
                        val = float("nan")
                features.append(val)
            output[timestamp] = (label, features)
    if not output:
        raise ValueError(f"Empty data: {path}")
    return output


def read_pair(core_i: Path, core_ii: Path):
    import numpy as np

    first = read_switch(core_i)
    second = read_switch(core_ii)
    shared_times = set(first) & set(second)
    unmatched = {"core_i": len(first) - len(shared_times), "core_ii": len(second) - len(shared_times)}
    if sum(unmatched.values()) > max(2, int(len(shared_times) * 0.01)):
        raise ValueError(f"Excessive unmatched timestamps; refusing unreviewed join: {unmatched}")
    times = sorted(shared_times)
    labels = []
    values = []
    for timestamp in times:
        y1, x1 = first[timestamp]
        y2, x2 = second[timestamp]
        if y1 != y2:
            raise ValueError(f"Conflicting labels across switches at {timestamp}")
        labels.append(y1)
        values.append(x1 + x2)
    return np.asarray(times, dtype="int64"), np.asarray(values, dtype="float64"), np.asarray(labels), unmatched


def load_published_fault_intervals(directory: Path):
    """Read independent experiment timestamps if original SOFI interval files exist."""
    if not directory.is_dir():
        return []
    intervals = []
    for path in sorted(directory.glob("*.csv")):
        with path.open("r", newline=None, encoding="utf-8-sig") as stream:
            for line in csv.reader(stream):
                if not line:
                    continue
                start, end, layer = int(line[0]), int(line[1]), line[2].strip()
                if start == end == 0:
                    continue
                if start > end:
                    raise ValueError(f"Invalid fault interval {path}: {line}")
                intervals.append({"start": start, "end": end, "layer": layer, "source_file": path.name})
    return sorted(intervals, key=lambda x: (x["start"], x["end"]))


def metrics(pred, y):
    from sklearn.metrics import (accuracy_score, confusion_matrix,
                                 precision_recall_fscore_support, average_precision_score)
    import numpy as np

    y_int = (y == "F").astype(int)
    p_int = (pred == "F").astype(int)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_int, p_int, average="binary", zero_division=0
    )
    matrix = confusion_matrix(y_int, p_int, labels=[0, 1])
    return {
        "observations": int(len(y)),
        "healthy": int(sum(y == "NE")),
        "fault": int(sum(y == "F")),
        "accuracy": float(accuracy_score(y_int, p_int)),
        "precision_fault": float(precision),
        "recall_fault": float(recall),
        "f1_fault": float(f1),
        "confusion_matrix_order": ["NE", "F"],
        "confusion_matrix": matrix.tolist(),
        "false_positive_observations": int(matrix[0,1]),
        "missed_fault_observations": int(matrix[1,0]),
    }


def run(core_i: Path, core_ii: Path, out: Path, faults_dir: Path | None = None) -> dict:
    import joblib
    import numpy as np
    from sklearn.dummy import DummyClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import average_precision_score, roc_auc_score

    times, values, labels, unmatched = read_pair(core_i, core_ii)
    if faults_dir is None:
        faults_dir = core_i.parent.parent / "raw data" / "failures" if core_i.parent.name.lower() == "normalized" else core_i.parent / "raw data" / "failures"
    episodes = load_published_fault_intervals(faults_dir)
    interval_matches = None
    if episodes:
        intervals_as_masks = [((times >= e["start"]) & (times <= e["end"])) for e in episodes]
        annotated_fault_mask = np.any(np.vstack(intervals_as_masks), axis=0)
        interval_matches = {
            "published_interval_entries": len(episodes),
            "fault_label_rows_covered_by_intervals": int(np.sum((labels == "F") & annotated_fault_mask)),
            "fault_label_rows_not_covered_by_intervals": int(np.sum((labels == "F") & (~annotated_fault_mask))),
            "normal_rows_inside_intervals": int(np.sum((labels == "NE") & annotated_fault_mask)),
        }
    n = len(times)
    cut_1, cut_2 = int(n * 0.60), int(n * 0.80)
    # A label streak is not a verified incident, so do not assign incident IDs
    # or infer independent episodes. Strictly separate chronological periods.
    windows = {
        "train": slice(0, cut_1),
        "validation": slice(cut_1, cut_2),
        "test": slice(cut_2, n),
    }
    result = {}
    for name, sl in windows.items():
        window_labels = labels[sl]
        if len(set(window_labels)) != 2:
            raise ValueError(f"Both classes required in {name} period")
        result[name] = {
            "rows": len(window_labels),
            "class_counts": dict(Counter(window_labels.tolist())),
            "start_utc": datetime.fromtimestamp(int(times[sl][0]), timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(int(times[sl][-1]), timezone.utc).isoformat(),
            "missing_sensor_cells": int(np.isnan(values[sl]).sum()),
        }
    model = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("scaler", StandardScaler()),
        ("classifier", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=42)),
    ])
    dummy = DummyClassifier(strategy="most_frequent")
    model.fit(values[windows["train"]], labels[windows["train"]])
    dummy.fit(values[windows["train"]], labels[windows["train"]])
    evaluation = {}
    for split in ("validation", "test"):
        sl = windows[split]
        X, y = values[sl], labels[sl]
        evaluation[f"logistic_{split}"] = metrics(model.predict(X), y)
        proba = model.predict_proba(X)[:, list(model.classes_).index("F")]
        evaluation[f"logistic_{split}"]["average_precision_fault"] = float(
            average_precision_score(y == "F", proba)
        )
        evaluation[f"logistic_{split}"]["roc_auc_fault"] = float(
            roc_auc_score(y == "F", proba)
        )
        evaluation[f"logistic_{split}"]["probabilities_uncalibrated"] = True
        evaluation[f"dummy_{split}"] = metrics(dummy.predict(X), y)
    report = {
        "report_version": "adaptivenet.external_benchmark.sofi.v1",
        "task": "binary_sofi_fault_condition_detection",
        "native_adaptivenet_model": False,
        "source_url": "https://data.mendeley.com/datasets/tc6ysmh5j8/2",
        "sha256_input_files": {
            "core_i": hashlib.sha256(core_i.read_bytes()).hexdigest(),
            "core_ii": hashlib.sha256(core_ii.read_bytes()).hexdigest(),
        },
        "n_unique_timestamps": int(n),
        "published_injection_intervals_audit": interval_matches,
        "published_injection_files_dir": str(faults_dir) if episodes else None,
        "n_switch_rows_aligned": int(2 * n),
        "unmatched_timestamps_excluded_explicitly": unmatched,
        "feature_names": [f"{switch}:{sensor}" for switch in ("I", "II") for sensor in SENSORS],
        "normalization": "Preserve numeric 0; mark blank/nonfinite/-1 sensor sentinel as missing (NaN), fit imputer only on training period",
        "split": "chronological 60/20/20, both switches paired by timestamp",
        "windows": result,
        "evaluation": evaluation,
        "forecast_horizon_seconds": 0,
        "limitations": [
            "Only 196 fault-labeled times in each original switch corpus; severe class imbalance",
            "One nonmatching timestamp from each switch was explicitly excluded during pairwise alignment",
            "One original monitored network across historical periods; modest generalization evidence",
            "Published labels represent network fault conditions, not independently verified AdaptiveNet gray failures",
            "Time periods are disjoint; experiment intervals are separately retained for provenance when available",
            "Publisher interval logs may contain multiple entries for one physical event; not treated as independent incidents without review",
            "Model uses both switches jointly; no external AWS-only vs AWS+RIPE feature arms",
            "No user-visible application HTTP impact labels or AWS internal CloudFront metrics",
            "Logistic probabilities have not been calibrated",
            "No incident-level false alarms per day or detection delay without verified onset/incident definitions",
            "Do not deploy model to native ml/predictor.py",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "evaluation-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    model_path = out / "external-sofi-benchmark-only.joblib"
    joblib.dump(model, model_path)
    print(f"Report: {report_path}")
    print(f"Benchmark-only model (NOT for AdaptiveNet production): {model_path}")
    print(f"Paired timestamps: {n}; explicitly excluded unmatched: {unmatched}")
    if interval_matches:
        print(f"Published injection interval audit: {interval_matches}")
    for split in ("validation", "test"):
        for kind in ("logistic", "dummy"):
            vals = evaluation[f"{kind}_{split}"]
            print(f"{kind}_{split}: rows={vals['observations']} fault_rows={vals['fault']} "
                  f"precision={vals['precision_fault']:.3f} recall={vals['recall_fault']:.3f} "
                  f"F1={vals['f1_fault']:.3f} FP={vals['false_positive_observations']} "
                  f"FN={vals['missed_fault_observations']}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-i", type=Path, required=True)
    parser.add_argument("--core-ii", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/ml-results/sofi"))
    parser.add_argument("--faults-dir", type=Path, default=None, help="Original SOFI raw data/failures directory")
    args = parser.parse_args()
    for p in (args.core_i, args.core_ii):
        if not p.is_file():
            parser.error(f"Cannot find {p}; check extracted SOFI dataset path")
    run(args.core_i, args.core_ii, args.out, args.faults_dir)


if __name__ == "__main__":
    main()
