#!/usr/bin/env python3
"""Train and evaluate the AdaptiveNet cause classifier on labelled lab ticks.

Input: a JSON-lines file, one row per region per tick, with these fields:
    run_id, split ("train" or "heldout"), tick_index, region,
    int_p95_ms, int_error_ratio, int_samples,
    ext_p95_ms, ext_error_ratio, ext_samples,
    label (healthy | network | compute | critical)

What it does:
    1. Builds features (badness per view, the gap between views, 3-tick means)
    2. Scores the Table I rules as the baseline
    3. Trains a small decision tree, cross-validated by run (GroupKFold)
    4. Tests rules and tree on the held-out fault magnitudes
    5. Saves the model to models/cause-tree-v1.joblib

Usage:
    python train_classifier.py --data data/ticks.jsonl
"""
import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import GroupKFold
from sklearn.tree import DecisionTreeClassifier, export_text

LATENCY_MS, ERROR_RATIO = 800.0, 0.2  # thresholds from the article, Section II-D
LABELS = ["healthy", "network", "compute", "critical"]
FEATURES = [
    "int_badness", "ext_badness", "int_error_ratio", "ext_error_ratio",
    "p95_gap_ms", "error_gap", "int_badness_mean3", "ext_badness_mean3",
]


def build_features(df):
    df = df[(df.int_samples >= 3) & (df.ext_samples >= 3)].copy()  # thin evidence = unknown, dropped
    for v in ("int", "ext"):
        df[f"{v}_badness"] = np.maximum(df[f"{v}_p95_ms"] / LATENCY_MS, df[f"{v}_error_ratio"] / ERROR_RATIO)
    df["p95_gap_ms"] = df.ext_p95_ms - df.int_p95_ms
    df["error_gap"] = df.ext_error_ratio - df.int_error_ratio
    df = df.sort_values(["run_id", "region", "tick_index"])
    for v in ("int", "ext"):  # mean of the last 3 ticks, matching the 3-tick persistence rule
        df[f"{v}_badness_mean3"] = (df.groupby(["run_id", "region"])[f"{v}_badness"]
                                      .transform(lambda s: s.rolling(3, min_periods=1).mean()))
    return df


def rules(df):
    """Table I of the article, written as code: the baseline the model must beat."""
    ext_bad, int_bad = df.ext_badness >= 1, df.int_badness >= 1
    critical = (df.ext_error_ratio >= 0.9) & (df.int_error_ratio >= 0.9)
    return np.select([critical, ext_bad & ~int_bad, ext_bad & int_bad, int_bad],
                     ["critical", "network", "compute", "compute"], default="healthy")


def false_action_rate(df, pred):
    """Share of healthy runs where a healthy region was called faulty 3 ticks in a row."""
    d = df.assign(pred=pred, wrong=lambda x: (x.label == "healthy") & (x.pred != "healthy"))
    hits = d.groupby(["run_id", "region"]).wrong.apply(
        lambda s: (s.rolling(3).sum() >= 3).any())
    return hits.mean()


def report(name, df, pred):
    print(f"\n=== {name} ===")
    print(f"macro-F1: {f1_score(df.label, pred, labels=LABELS, average='macro', zero_division=0):.3f}")
    print(f"false action rate on healthy regions: {false_action_rate(df, pred):.1%}")
    print("confusion matrix (rows = true, columns = predicted):", LABELS)
    print(confusion_matrix(df.label, pred, labels=LABELS))
    print(classification_report(df.label, pred, labels=LABELS, zero_division=0))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/ticks.jsonl")
    p.add_argument("--max-depth", type=int, default=4)
    p.add_argument("--out", default="models/cause-tree-v1.joblib")
    a = p.parse_args()

    df = build_features(pd.read_json(a.data, lines=True))
    train, heldout = df[df.split == "train"], df[df.split == "heldout"]
    print(f"{len(train)} training rows from {train.run_id.nunique()} runs, "
          f"{len(heldout)} held-out rows from {heldout.run_id.nunique()} runs")

    # Cross-validation: every tick of a run stays on the same side of the split.
    scores = []
    for fit_idx, test_idx in GroupKFold(n_splits=5).split(train, groups=train.run_id):
        tree = DecisionTreeClassifier(max_depth=a.max_depth, class_weight="balanced", random_state=0)
        tree.fit(train.iloc[fit_idx][FEATURES], train.iloc[fit_idx].label)
        part = train.iloc[test_idx]
        scores.append(f1_score(part.label, tree.predict(part[FEATURES]), labels=LABELS,
                               average="macro", zero_division=0))
    print(f"\nTree, 5-fold cross-validation by run: macro-F1 {np.mean(scores):.3f} +/- {np.std(scores):.3f}")

    # Final model on all training runs, then the honest test: unseen fault magnitudes.
    tree = DecisionTreeClassifier(max_depth=a.max_depth, class_weight="balanced", random_state=0)
    tree.fit(train[FEATURES], train.label)
    if len(heldout):
        report("Rules (Table I) on held-out magnitudes", heldout, rules(heldout))
        report("Decision tree on held-out magnitudes", heldout, tree.predict(heldout[FEATURES]))

    print("\nThe learned tree, readable for the report:")
    print(export_text(tree, feature_names=FEATURES))

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": tree, "features": FEATURES, "labels": LABELS, "version": "cause-tree-v1"}, out)
    print(f"Saved model to {out}")


if __name__ == "__main__":
    main()
