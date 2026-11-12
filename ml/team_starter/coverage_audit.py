"""Six-class evidence coverage and false-alarm estimability audit.

Read-only. It answers "what can we honestly claim from the verified evidence we
actually have?" and lists exactly which evidence is still missing. It never
creates labels, rows or scores: every count comes from a human-reviewed
``adaptivenet.verified_episodes.v1`` manifest (and optionally a dataset built
by ``offline_cli dataset`` and the predictions of one arm).

False alarms per day use the *verified healthy interval duration* of held-out
episodes as exposure time, not the span between the first and last row (a
single-row healthy episode would otherwise contribute zero exposure). The rate
is reported with an exact (Garwood) Poisson 95% interval; with zero alarms the
upper bound is the familiar "rule of three" style bound.

Standalone:
    python -m ml.team_starter.coverage_audit --labels verified.json \
        [--dataset dataset.json] [--predictions preds.json] [--out audit.json]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path

from .evaluation import split_episodes
from .verified_labels import LABELS, validate_manifest

CONTRACT = "adaptivenet.class_coverage_audit.v1"
INCIDENT_LABELS = ("application_failure", "network_degradation", "partial_outage")
# Minimum independent experiment runs per class before a supervised class can be
# trained AND scored. Two runs in train lets validation see an unseen run; one
# held-out run is the bare minimum for a test score, not a reliable estimate.
MIN_TRAIN_RUNS = 2
MIN_TEST_RUNS = 1
# Below this much held-out healthy exposure a per-day rate is reported but
# flagged as indicative only (one hour of quiet says little about a day).
MIN_HEALTHY_TEST_HOURS = 6.0
SPLITS = ("train", "validation", "test", "excluded_for_embargo")


def _poisson_cdf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def _bisect(f, lo: float, hi: float) -> float:
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid):
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def poisson_interval(k: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided Poisson CI for an observed count ``k`` (Garwood)."""
    if type(k) is not int or k < 0:
        raise ValueError("count must be a nonnegative int")
    hi_cap = 10.0 + 10 * k
    upper = _bisect(lambda lam: _poisson_cdf(k, lam) <= alpha / 2, 0.0, hi_cap)
    lower = 0.0 if k == 0 else _bisect(lambda lam: 1 - _poisson_cdf(k - 1, lam) >= alpha / 2, 0.0, hi_cap)
    return lower, upper


def false_alarm_rate(alarms: int, exposure_seconds: float) -> dict:
    """False alarms per 24h of verified-healthy exposure, with exact 95% CI."""
    if not isinstance(exposure_seconds, (int, float)) or not math.isfinite(exposure_seconds) or exposure_seconds < 0:
        raise ValueError("exposure must be finite and nonnegative")
    if exposure_seconds == 0:
        return {"status": "not_estimable", "reason": "no_held_out_verified_healthy_exposure",
                "false_alarms": alarms, "exposure_hours": 0.0, "per_day": None,
                "per_day_95ci": None}
    days = exposure_seconds / 86400
    lo, hi = poisson_interval(alarms)
    hours = exposure_seconds / 3600
    return {"status": "estimated" if hours >= MIN_HEALTHY_TEST_HOURS else "indicative_only",
            "false_alarms": alarms, "exposure_hours": hours, "per_day": alarms / days,
            "per_day_95ci": [lo / days, hi / days],
            "note": (None if hours >= MIN_HEALTHY_TEST_HOURS else
                     f"held-out healthy exposure < {MIN_HEALTHY_TEST_HOURS} h; interval is wide")}


def _key(row: dict) -> tuple[str, str]:
    return row["episode_id"], row["snapshot_id"]


def _alarm_transitions(rows: list[dict], preds: dict[tuple[str, str], str | None]) -> dict[str, int]:
    """Transitions into a non-healthy alert inside each healthy episode."""
    by_ep = defaultdict(list)
    for row in rows:
        if row["label"] == "healthy" and _key(row) in preds:
            by_ep[row["episode_id"]].append((row["at_unix"], preds[_key(row)]))
    out = {}
    for ep, samples in by_ep.items():
        samples.sort(key=lambda s: s[0])
        count, previous = 0, False
        for _, pred in samples:
            alert = pred not in (None, "healthy")
            count += alert and not previous
            previous = alert
        out[ep] = count
    return out


def audit(manifest: dict, dataset: dict | None = None, predictions: dict | None = None,
          *, embargo_seconds: float = 900) -> dict:
    episodes = validate_manifest(manifest)
    blockers: list[str] = []
    try:
        parts = split_episodes(episodes, embargo_seconds=embargo_seconds)
    except ValueError as exc:
        parts = None
        blockers.append(f"chronological_split_impossible: {exc}")
    split_of = {}
    for name in SPLITS:
        for ident in (parts or {}).get(name, []):
            split_of[ident] = name
    rows_by_ep = defaultdict(int)
    for row in (dataset or {}).get("rows", []):
        rows_by_ep[row["episode_id"]] += 1

    table = {label: {s: {"episodes": 0, "runs": set(), "rows": 0, "verified_seconds": 0.0}
                     for s in SPLITS + ("unsplit",)} for label in LABELS}
    for e in episodes:
        cell = table[e["label"]][split_of.get(e["episode_id"], "unsplit")]
        cell["episodes"] += 1
        cell["runs"].add(e["experiment_run_id"])
        cell["rows"] += rows_by_ep.get(e["episode_id"], 0)
        cell["verified_seconds"] += e["end_unix"] - e["start_unix"]

    classes = {}
    for label in LABELS:
        cells = table[label]
        total_runs = set().union(*(c["runs"] for c in cells.values()))
        needs = []
        if not total_runs:
            needs.append("no_verified_episode_of_this_class")
        if len(cells["train"]["runs"]) < MIN_TRAIN_RUNS:
            needs.append(f"need >= {MIN_TRAIN_RUNS} independent experiment runs in train "
                         f"(have {len(cells['train']['runs'])})")
        if len(cells["test"]["runs"]) < MIN_TEST_RUNS:
            needs.append(f"need >= {MIN_TEST_RUNS} held-out test run (have {len(cells['test']['runs'])})")
        if needs and label == "partial_outage":
            needs.append("each episode needs >= 2 independent vantage points; the single HTTP witness "
                         "is not enough, so use multi-probe RIPE Atlas evidence")
        if needs and label in ("transient", "recovery"):
            needs.append("label the phase inside the same experiment_run_id as the incident it belongs to")
        classes[label] = {
            "by_split": {s: {"episodes": c["episodes"], "experiment_runs": len(c["runs"]),
                             "rows": c["rows"], "verified_hours": c["verified_seconds"] / 3600}
                         for s, c in cells.items() if c["episodes"]},
            "trainable_and_scorable": not needs,
            "missing": needs,
        }

    healthy_test = [e for e in episodes if e["label"] == "healthy" and split_of.get(e["episode_id"]) == "test"]
    exposure = sum(e["end_unix"] - e["start_unix"] for e in healthy_test)
    fa = {"status": "not_estimable", "reason": "no_held_out_verified_healthy_exposure",
          "exposure_hours": exposure / 3600, "per_day": None}
    if exposure > 0:
        if predictions is None or dataset is None:
            fa = {"status": "estimable_needs_predictions", "exposure_hours": exposure / 3600,
                  "healthy_test_episodes": len(healthy_test), "per_day": None}
        else:
            if predictions.get("contract_version") != "adaptivenet.native_test_predictions.v1":
                raise ValueError("predictions must be adaptivenet.native_test_predictions.v1")
            preds = {_key(p): p.get("predicted_label") for p in predictions.get("rows", [])}
            test_ids = {e["episode_id"] for e in healthy_test}
            rows = [r for r in dataset.get("rows", []) if r["episode_id"] in test_ids]
            missing_preds = sum(_key(r) not in preds for r in rows)
            alarms = sum(_alarm_transitions(rows, preds).values())
            fa = false_alarm_rate(alarms, exposure)
            fa.update(healthy_test_episodes=len(healthy_test), healthy_test_rows=len(rows),
                      rows_without_prediction=missing_preds)
            if missing_preds:
                fa["status"] = "incomplete_predictions"
    if fa["per_day"] is None and fa["status"] == "not_estimable":
        blockers.append("false_alarms_per_day_not_estimable: no verified healthy episode in a held-out test block")

    six_class_ready = all(c["trainable_and_scorable"] for c in classes.values())
    if not six_class_ready:
        blockers.append("six_class_model_not_supported: " + ", ".join(
            l for l, c in classes.items() if not c["trainable_and_scorable"]))
    return {"contract_version": CONTRACT, "episode_count": len(episodes),
            "experiment_runs": len({e["experiment_run_id"] for e in episodes}),
            "legacy_episodes_without_run_id": sum(not e["experiment_run_id_explicit"] for e in episodes),
            "split": parts, "classes": classes,
            "trainable_classes": [l for l, c in classes.items() if c["trainable_and_scorable"]],
            "six_class_ready": six_class_ready, "false_alarms_per_day": fa,
            "blockers": blockers,
            "fabrication_guard": "counts derive only from reviewed manifest episodes; nothing is generated"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--embargo-seconds", type=float, default=900)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    load = lambda p: json.loads(p.read_text(encoding="utf-8")) if p else None
    result = audit(load(args.labels), load(args.dataset), load(args.predictions),
                   embargo_seconds=args.embargo_seconds)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    print(text if not args.out else f"{args.out}: six_class_ready={result['six_class_ready']} "
          f"false_alarms={result['false_alarms_per_day']['status']}")
    for b in result["blockers"]:
        print("BLOCKER", b)
    return 0 if not result["blockers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
