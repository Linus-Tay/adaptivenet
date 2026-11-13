"""Reproducible, opt-in training ONLY on independently verified episodes.

No sample data generated, no model fit without sufficient evidence. The test
split is used exactly once for evaluation, not hyperparameter selection.
"""
from __future__ import annotations
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import pickle

from .evaluation import fixed_rule, metrics, split_by
from .feature_views import GROUPS

MIN_TRAIN_ROWS = 10
MIN_TRAIN_EPISODES_PER_CLASS = 2


def vector(row: dict, group: str, feature_names: list[str]) -> list[float]:
    view = row["views"][group]
    if set(view["values"]) != set(feature_names):
        raise ValueError("feature schema changed; retraining required")
    return [float(view["values"][f]) if view["values"][f] is not None else float("nan")
            for f in feature_names] + [1.0 if view["missing"][f] else 0.0 for f in feature_names]


def train_and_evaluate(dataset: dict, out: Path, *, group: str = "aws_ripe",
                       embargo_seconds: float = 900, min_confidence: float = .55,
                       split_strategy: str = "chronological") -> dict:
    if group not in GROUPS:
        raise ValueError("unknown feature group")
    if dataset.get("contract_version") != "adaptivenet.offline_dataset.v1":
        raise ValueError("offline dataset contract mismatch")
    rows = dataset.get("rows", [])
    result = {"report_version": "adaptivenet.ml_evaluation.v1", "group": group,
              "status": "not_trained", "model_artifact": None,
              "limitations": dataset.get("limitations", []) + ["one_http_witness_vantage"],
              "forecasting_horizon_seconds": 0, "task": "current_condition_detection", "split_strategy": split_strategy}
    if not rows:
        result["blockers"] = ["no_independently_verified_labeled_snapshots"]
        return result
    # Fail closed: legacy v1 episode manifests without explicit run identity
    # can be inspected, but cannot justify a leakage-safe trained result.
    if any(not e.get("experiment_run_id_explicit", False)
           for e in dataset.get("episodes", [])):
        result["blockers"] = ["verified episodes require explicit experiment_run_id for training"]
        return result
    try:
        split = split_by(split_strategy, dataset["episodes"], embargo_seconds=embargo_seconds)
    except ValueError as exc:
        result["blockers"] = [str(exc)]
        return result
    by_set = {name: sorted((r for r in rows if r["episode_id"] in set(split[name])),
                           key=lambda r: (r["at_unix"], r["episode_id"], r["snapshot_id"]))
              for name in ("train", "validation", "test")}
    onsets = {e["episode_id"]: e["start_unix"] for e in dataset["episodes"]}
    result["split"] = {k: v for k,v in split.items()}
    result["row_counts"] = {k: len(v) for k, v in by_set.items()}
    result["fixed_rule_test"] = metrics(by_set["test"], [fixed_rule(r, group) for r in by_set["test"]], onsets)
    train_rows = [r for r in by_set["train"] if r["views"][group]["ready_for_shadow_assessment"]]
    validation_rows = [r for r in by_set["validation"] if r["views"][group]["ready_for_shadow_assessment"]]
    test_rows = by_set["test"]
    class_runs = {}
    for label in set(r["label"] for r in train_rows):
        class_runs[label] = len({r["experiment_run_id"] for r in train_rows
                                 if r["label"] == label})
    result["verified_train_class_run_counts"] = class_runs
    blockers = []
    if len(train_rows) < MIN_TRAIN_ROWS:
        blockers.append(f"need >= {MIN_TRAIN_ROWS} fresh verified training rows")
    if len(class_runs) < 2:
        blockers.append("need >=2 independently verified classes for classification")
    if any(n < MIN_TRAIN_EPISODES_PER_CLASS for n in class_runs.values()):
        blockers.append(f"need >= {MIN_TRAIN_EPISODES_PER_CLASS} independent training runs per trained class")
    if not validation_rows or not test_rows:
        blockers.append("validation and test must both have evaluable rows")
    if blockers:
        result["blockers"] = blockers
        return result
    try:
        import sklearn
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        result["blockers"] = ["install optional package: python -m pip install -e '.[ml]'"]
        return result
    names = sorted(train_rows[0]["views"][group]["values"])
    model = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                      ("scale", StandardScaler()),
                      ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced",
                                                        random_state=42))])
    model.fit([vector(r, group, names) for r in train_rows], [r["label"] for r in train_rows])
    classes = list(model.classes_)
    def predict(rows0):
        predictions, confidences, probabilities = [], [], []
        for row in rows0:
            if not row["views"][group]["ready_for_shadow_assessment"]:
                predictions.append(None);confidences.append(None);probabilities.append(None);continue
            proba = model.predict_proba([vector(row, group, names)])[0].tolist()
            idx = max(range(len(proba)), key=lambda j: proba[j])
            confidences.append(proba[idx]);probabilities.append(proba)
            predictions.append(classes[idx] if proba[idx] >= min_confidence else None)
        return predictions, confidences, probabilities
    val_pred, _, _ = predict(validation_rows)
    test_pred, test_confidence, test_prob = predict(test_rows)
    # Preserve exactly the held-out row identities and prediction results for
    # per-incident comparison. No source features, credentials, or raw telemetry.
    test_predictions = {
        "contract_version": "adaptivenet.native_test_predictions.v1",
        "group": group, "source": "reviewed_native_episodes", "test_only": True,
        "forecasting_horizon_seconds": 0,
        "rows": [{"episode_id": r["episode_id"], "experiment_run_id": r["experiment_run_id"],
                  "snapshot_id": r["snapshot_id"], "at_unix": r["at_unix"],
                  "region": r["region"], "label": r["label"],
                  "predicted_label": pred, "max_probability": confidence}
                 for r, pred, confidence in zip(test_rows, test_pred, test_confidence)]
    }
    result["validation"] = metrics(validation_rows, val_pred, onsets)
    result["test"] = metrics(test_rows, test_pred, onsets)
    # Brier score is a proper scoring rule for multiclass probability quality.
    brier_rows = [(r, p) for r, p in zip(test_rows, test_prob) if p is not None]
    result["test_multiclass_brier_score"] = (sum(sum((p[i] - (1 if r["label"] == label else 0))**2
                                                 for i, label in enumerate(classes)) for r, p in brier_rows)
                                             / len(brier_rows) if brier_rows else None)
    result["test_mean_max_probability_when_inputs_ready"] = (sum(v for v in test_confidence if v is not None)
                                                   / sum(v is not None for v in test_confidence)
                                                   if any(v is not None for v in test_confidence) else None)
    model_id = sha256(json.dumps({"group": group, "columns": names, "classes": classes,
                                  "train_episodes": split["train"], "version": "v1",
                                  **({} if split_strategy == "chronological" else {"split": split_strategy})},
                                 sort_keys=True).encode()).hexdigest()[:16]
    artifact = {"artifact_version": "adaptivenet.offline_model.v1", "model_id": f"logreg-{group}-{model_id}",
                "group": group, "feature_names": names, "classes": classes,
                "min_confidence": min_confidence, "model": model,
                "trained_on_episode_ids": split["train"], "forecast_horizon_seconds": 0,
                "task": "current_condition_detection", "scikit_learn_version": sklearn.__version__}
    out.mkdir(parents=True, exist_ok=True)
    # Pickle should only ever be loaded from locally generated, trusted artifacts.
    model_path = out / f"{artifact['model_id']}.pkl"
    with model_path.open("wb") as f:
        pickle.dump(artifact, f, protocol=pickle.HIGHEST_PROTOCOL)
    predictions_path = out / f"test-predictions-{group}.json"
    predictions_path.write_text(json.dumps(test_predictions, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    result.update({"test_predictions_artifact": str(predictions_path),
                   "status": "trained", "model_artifact": str(model_path),
                   "model_id": artifact["model_id"], "test_tuned": False,
                   "abstention_confidence_threshold": min_confidence})
    return result
