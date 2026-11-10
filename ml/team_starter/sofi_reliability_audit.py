"""Step 8.5 — SOFI reliability audit for AdaptiveNet (offline, no AWS writes).

Run from AdaptiveNet repository root after the SOFI Step 8.4 script is installed:
  python -m ml.team_starter.sofi_reliability_audit --core-i "data/public-benchmarks/sofi/tc6ysmh5j8-2/normalized/SOFI CoreSwitch-I.csv" --core-ii "data/public-benchmarks/sofi/tc6ysmh5j8-2/normalized/SOFI CoreSwitch-II.csv" --out data/ml-results/sofi-audit

This audits observed monitoring missingness / invalid counters, and compares
pre-specified transparent logistic variants with a threshold chosen ONLY on
validation. The final chronological test is never used to select the model or
threshold. The script writes no deployment artifact and never creates labels.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def run(core_i: Path, core_ii: Path, out: Path) -> dict:
    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, confusion_matrix, precision_recall_curve
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from ml.team_starter.public_sofi_benchmark import read_pair, SENSORS, load_published_fault_intervals

    times, raw_values, labels, unmatched = read_pair(core_i, core_ii)
    n = len(times)
    cut1, cut2 = int(n * .60), int(n * .80)
    windows = {"train": slice(0, cut1), "validation": slice(cut1, cut2), "test": slice(cut2, n)}
    y = (labels == "F").astype(int)

    # Observation counters, ICMP times/loss, and interface flags must be >= 0.
    # -1 is already missing in the v1 parser; -2 also occurs in the raw SNMP
    # counter fields and cannot represent a physical negative packet count.
    negative_raw_cells = int(np.sum(np.isfinite(raw_values) & (raw_values < 0)))
    values = raw_values.copy()
    values[values < 0] = np.nan
    feature_names = [f"{switch}:{name}" for switch in ("I", "II") for name in SENSORS]
    icmp_loss_indices = [feature_names.index("I:ICMP_loss"), feature_names.index("II:ICMP_loss")]

    def rates_per_period():
        summary = {}
        for w, sl in windows.items():
            record = {"rows": int(len(y[sl])), "fault_rows": int(y[sl].sum()), "missing_cells": int(np.isnan(values[sl]).sum()), "negative_cells_excluded": int(np.sum(np.isfinite(raw_values[sl]) & (raw_values[sl] < 0)))}
            for cls, is_fault in (("healthy", 0), ("fault", 1)):
                mask = (y[sl] == is_fault)
                if int(mask.sum()) == 0:
                    raise ValueError(f"No {cls} records in {w}; cannot evaluate binary task")
                record[f"{cls}_rows"] = int(mask.sum())
                for i in icmp_loss_indices:
                    record[f"{cls}_{feature_names[i]}_missing_fraction"] = float(np.isnan(values[sl][mask, i]).mean())
            summary[w] = record
        return summary

    def score(y_true, probabilities, threshold):
        predictions = (probabilities >= threshold).astype(int)
        tn, fp, fn, tp = [int(v) for v in confusion_matrix(y_true, predictions, labels=[0, 1]).ravel()]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {"threshold": float(threshold), "true_positive": tp, "false_positive": fp,
                "false_negative": fn, "true_negative": tn, "precision": precision,
                "recall": recall, "f1": f1, "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
                "positive_prediction_fraction": float(predictions.mean())}

    def validation_threshold(y_val, p_val):
        precisions, recalls, thresholds = precision_recall_curve(y_val, p_val)
        # The final PR curve point has no threshold; exclude it.
        f1 = np.divide(2 * precisions[:-1] * recalls[:-1],
                       precisions[:-1] + recalls[:-1],
                       out=np.zeros_like(thresholds, dtype=float),
                       where=(precisions[:-1] + recalls[:-1]) > 0)
        best = int(np.argmax(f1))
        return float(thresholds[best])

    # Verify published fault intervals do not straddle any of our time splits.
    fault_dir = (core_i.parent.parent / "raw data" / "failures"
                 if core_i.parent.name.lower() == "normalized"
                 else core_i.parent / "raw data" / "failures")
    episodes = load_published_fault_intervals(fault_dir)
    crossing = [e for e in episodes if any(e["start"] < int(times[c]) <= e["end"] for c in (cut1, cut2))]
    if crossing:
        raise ValueError(f"Published fault interval crosses chronological partition boundary: {crossing[:3]}")

    options = {
        "balanced_logistic_with_missing_indicators": dict(weight="balanced", indicators=True),
        "unweighted_logistic_without_missing_indicators": dict(weight=None, indicators=False),
        "balanced_logistic_without_missing_indicators": dict(weight="balanced", indicators=False),
    }
    candidates = {}
    # Train-only median fitting. Always preserve true numeric zeros.
    for name, config in options.items():
        model = Pipeline([
            ("imputer", SimpleImputer(strategy="median", add_indicator=config["indicators"], keep_empty_features=True)),
            ("scaler", StandardScaler()),
            ("classifier", LogisticRegression(max_iter=2000, class_weight=config["weight"], random_state=42)),
        ])
        model.fit(values[windows["train"]], y[windows["train"]])
        pv = model.predict_proba(values[windows["validation"]])[:, 1]
        threshold = validation_threshold(y[windows["validation"]], pv)
        candidates[name] = {
            "threshold_selected_on_validation": threshold,
            "validation_average_precision": float(average_precision_score(y[windows["validation"]], pv)),
            "validation_at_default_0_5": score(y[windows["validation"]], pv, .5),
            "validation_at_selected_threshold": score(y[windows["validation"]], pv, threshold),
        }
        # Freeze the choices and probabilities. No test metrics used for tuning.
        candidates[name]["_model"] = model

    selected = max(candidates, key=lambda key: (candidates[key]["validation_at_selected_threshold"]["f1"],
                                                  candidates[key]["validation_at_selected_threshold"]["precision"]))
    tested_models = {}
    for name, stats in candidates.items():
        if name != selected and name != "balanced_logistic_with_missing_indicators":
            # Avoid extra exploratory test comparisons to protect the final holdout.
            continue
        pt = stats["_model"].predict_proba(values[windows["test"]])[:, 1]
        tested_models[name] = {
            "threshold": stats["threshold_selected_on_validation"] if name == selected else .5,
            "test": score(y[windows["test"]], pt,
                          stats["threshold_selected_on_validation"] if name == selected else .5),
            "test_average_precision": float(average_precision_score(y[windows["test"]], pt)),
        }

    for stats in candidates.values():
        stats.pop("_model")
    # A predeclared, direct signal baseline: observed nonzero ICMP loss.
    test_rows = values[windows["test"]]
    simple_rule_pred = np.any(np.nan_to_num(test_rows[:, icmp_loss_indices], nan=0) > 0, axis=1).astype(float)
    rule_stats = score(y[windows["test"]], simple_rule_pred, .5)

    report = {
        "report_version": "adaptivenet.external_benchmark.sofi_reliability_audit.v1",
        "source": "SOFI CoreSwitch-I/II paired historical network telemetry, NOT AdaptiveNet live data",
        "observations": int(n),
        "unmatched": unmatched,
        "negative_packet_counter_cells_marked_missing": negative_raw_cells,
        "published_interval_entries": len(episodes),
        "published_interval_crossing_chronological_splits": len(crossing),
        "split": "chronological 60/20/20, paired timestamps together; fault intervals may not cross split",
        "feature_quality": rates_per_period(),
        "candidate_models_selected_using_validation_only": candidates,
        "selected_model": selected,
        "heldout_test_evaluation": tested_models,
        "nonzero_icmp_loss_rule_test": rule_stats,
        "warnings": [
            "Validation-selected threshold can still fail on future periods if measurement completeness changes",
            "A 0.0 recall for a strict threshold is not a successful detector simply because false alarms are low",
            "Published fault injection labels are not verified AdaptiveNet cloud gray-failure labels",
            "If test results are weak, abstain from deployment; no test-set-based retuning",
            "No model deployed; no native predictor, AWS, Docker or infrastructure files modified",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / "reliability-audit.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Audit report: {path}")
    print(f"Aligned timestamps={n}; negative counter cells treated as missing={negative_raw_cells}; intervals crossing splits={len(crossing)}")
    for split, stat in report["feature_quality"].items():
        print(f"{split} faults={stat['fault_rows']}/{stat['rows']}; "
              f"ICMP loss missing among healthy={stat['healthy_I:ICMP_loss_missing_fraction']:.1%}, "
              f"fault={stat['fault_I:ICMP_loss_missing_fraction']:.1%}")
    print("Selected using VALIDATION ONLY:", selected)
    for name, scores in tested_models.items():
        s = scores["test"]
        print(f"test {name}: threshold={s['threshold']:.5f} TP={s['true_positive']} FP={s['false_positive']} FN={s['false_negative']} precision={s['precision']:.3f} recall={s['recall']:.3f} F1={s['f1']:.3f}")
    s = rule_stats
    print(f"test observed-ICMP-loss rule: TP={s['true_positive']} FP={s['false_positive']} FN={s['false_negative']}")
    print("Decision: benchmark-only audit. DO NOT DEPLOY from this report.")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-i", type=Path, required=True)
    parser.add_argument("--core-ii", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("data/ml-results/sofi-audit"))
    args = parser.parse_args()
    for p in (args.core_i, args.core_ii):
        if not p.is_file():
            parser.error(f"SOFI CSV missing: {p}")
    run(args.core_i, args.core_ii, args.out)


if __name__ == "__main__":
    main()
