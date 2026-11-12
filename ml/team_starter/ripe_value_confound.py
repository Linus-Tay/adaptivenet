"""Retrain the Step 3 logistic arms with and without host-resource columns.

Shows which features the trained AWS-only model actually used on the reviewed
network split. Offline, shadow-only; no labels or values are created.
Usage:
  python -m ml.team_starter.ripe_value_confound --dataset .../verified_network_dataset.json \
     --split .../approved_chronological_split.json --ripe region-b=... [--ripe ...]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import ripe_value as rv

HOST = ("memory", "cpu", "load")


def retrain(dataset: dict, split: dict, group: str, drop: tuple[str, ...] = (), min_confidence: float = .55) -> dict:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    label = lambda r: "network_degradation" if r["label"] == "network_degradation" else "normal"
    train = [r for r in dataset["rows"] if r["episode_id"] in split["train"]
             and r["views"][group]["ready_for_shadow_assessment"]]
    names = sorted(k for k in train[0]["views"][group]["values"] if not any(x in k for x in drop))

    def vec(r):
        v = r["views"][group]
        return ([float(v["values"][f]) if v["values"][f] is not None else float("nan") for f in names] +
                [1.0 if v["missing"][f] else 0.0 for f in names])
    model = Pipeline([("i", SimpleImputer(strategy="median", keep_empty_features=True)), ("s", StandardScaler()),
                      ("c", LogisticRegression(max_iter=1500, class_weight="balanced", random_state=42))])
    model.fit([vec(r) for r in train], [label(r) for r in train])
    pos = list(model.classes_).index("network_degradation")
    held = {}
    for eid in split["validation"] + split["test"] + split.get("excluded_for_embargo", []):
        rows = [r for r in dataset["rows"] if r["episode_id"] == eid]
        ready = [r for r in rows if r["views"][group]["ready_for_shadow_assessment"]]
        alerts = sum(model.predict_proba([vec(r)])[0][pos] >= min_confidence for r in ready)
        held[eid] = {"rows": len(rows), "ready_rows": len(ready), "alert_rows": int(alerts)}
    coef = model.named_steps["c"].coef_[0]
    feats = names + ["missing:" + n for n in names]
    sign = 1 if pos == 1 else -1  # coef_ refers to classes_[1]
    top = sorted(((float(sign * c), f) for c, f in zip(coef, feats)), key=lambda x: -abs(x[0]))[:5]
    return {"group": group, "dropped": list(drop), "heldout": held, "top_coefficients_toward_degradation": top}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--split", type=Path, required=True)
    ap.add_argument("--ripe", action="append", required=True, metavar="REGION=PATH")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    index = rv.ProbeIndex(rv.load_samples(rv._region_files(a.ripe)))
    data = rv.augment_dataset(json.loads(a.dataset.read_text(encoding="utf-8-sig")), index)
    split = json.loads(a.split.read_text(encoding="utf-8"))
    res = [retrain(data, split, "aws_only"), retrain(data, split, "aws_only", HOST),
           retrain(data, split, "aws_ripe_path", HOST)]
    text = json.dumps(res, indent=2)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
