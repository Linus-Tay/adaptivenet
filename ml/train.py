#!/usr/bin/env python3
"""Train a cause classifier on labeled model-input rows and save it for ml/predictor.py.

Input: JSONL rows as written by scripts/export_ml_dataset.py, each with a verified
`fault_label` from the experiment manifest: 'healthy' or '<region>:<cause>' where
cause is network_degradation | compute_overload | critical_failure. Rows without a
label are skipped: labels are never inferred here.

    python ml/train.py --data data/ml-snapshots.labeled.jsonl --out data/cause_model.pkl
    ADAPTIVENET_MODEL_PATH=data/cause_model.pkl   # the controller then uses it

Requires scikit-learn (`pip install -e '.[ml]'`). The saved bundle is a pickle:
{"model_id", "feature_columns", "estimator"}; keep it next to the run manifest.
"""
from __future__ import annotations
import argparse
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT))

from adaptivenet.ingestion.handoff import FEATURE_COLUMNS  # noqa: E402
from ml.predictor import feature_vector  # noqa: E402

LABELS = {"healthy"} | {f"{r}:{c}" for r in ("region-a", "region-b")
                        for c in ("network_degradation", "compute_overload", "critical_failure")}


def load_rows(path: Path) -> tuple[list[list[float]], list[str]]:
    X, y = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        label = row.get("fault_label")
        if label not in LABELS:
            continue
        X.append(feature_vector({"features": row["features"]}))
        y.append(label)
    return X, y


def fit(X, y, model_id: str):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import make_pipeline
    # Missing values: the mask half of the vector carries missingness explicitly, so the
    # imputed constant is never mistaken for a measurement (see docs/prediction/README.md).
    estimator = make_pipeline(
        SimpleImputer(strategy="constant", fill_value=0.0, keep_empty_features=True),
        HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, early_stopping=False,
                                       min_samples_leaf=1, random_state=0))
    estimator.fit(X, y)
    return {"model_id": model_id, "feature_columns": list(FEATURE_COLUMNS), "estimator": estimator}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model-id", default="cause_hgb_v1")
    args = parser.parse_args(argv)
    X, y = load_rows(Path(args.data))
    if len(set(y)) < 2:
        parser.error(f"need at least two labeled classes, found {sorted(set(y))}")
    bundle = fit(X, y, args.model_id)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as handle:
        pickle.dump(bundle, handle)
    print(f"trained {args.model_id} on {len(y)} labeled rows; classes={sorted(set(y))}; wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
