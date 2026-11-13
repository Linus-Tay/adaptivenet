"""Same-day retrain of the ML detectors on the 12-13 Nov RIPE campaign (offline, SHADOW ONLY).

Question: earlier native models (trained on Nov 9 healthy data) raised 58-150 false
alarms/day on a reviewed Nov 10 healthy window, because host memory drifted between days.
Does training on healthy data from the SAME evening as the trials fix that, and what do
the retrained models detect on the 20 SSM-confirmed packet-loss trials?

Everything below is fixed in code BEFORE the first scoring run (see PROTOCOL). Nothing
here changes the frozen RIPE rule (`ripe_value.ripe_path_alert`), which is only scored
for context on the same held-out rows. No AWS calls; inputs are the local campaign files.

Leakage controls
----------------
* Healthy rows = rows outside every submitted slot window (confirmed or not) plus a
  recovery pad. Rows in a pad are dropped, never labelled.
* Impaired rows = [SSM request, request + duration] of SSM-confirmed slots only.
* Blocked split: contiguous time folds, cut only between slots. A fold's model never
  sees that fold's rows (trial or healthy), and training also drops an embargo on
  both sides of the fold, so false alarms are always counted on unseen healthy time.
* Forward split: train only on the first half of the campaign, test on the second half.
* Features are as-of: AMP row at t and RIPE results with timestamp <= t only.
* Dead Singapore probe 1010367 is removed (Singapore only) before any feature is computed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

from .ripe_value import (PATH_COLUMNS, ProbeIndex, aws_view_alert, campaign_slots, iso,
                         load_samples, path_features, ripe_path_alert)

REPORT_VERSION = "adaptivenet.campaign_same_day_ml.v1"

PROTOCOL = {
    "row_step_seconds": 30,                 # AMP export step
    "ripe_warmup_seconds": 600,             # rows start 10 min after the first RIPE result
    "recovery_pad_seconds": 600,            # > 480 s RIPE loss window after rule removal
    "pre_pad_seconds": 60,
    "embargo_seconds": 900,
    "slots_per_fold": 4,
    # 1010367 never reaches Singapore (0 of 408 pings) but reaches Virginia normally
    # (379 of 392), so it is removed for region-a only. Correction made before the
    # results were written up: the first run removed it from both regions.
    "excluded_probes": {"region-a": [1010367]},
    "supervised_min_probability": 0.55,     # same as the earlier native arms
    "anomaly_quantile": 0.995,              # threshold from TRAINING healthy scores only
    "isolation_forest": {"n_estimators": 300, "random_state": 42},
    "logistic_regression": {"max_iter": 1500, "class_weight": "balanced", "random_state": 42},
}
AWS_FEATURES = ("app_up", "http_rps", "http_5xx_rps", "http_error_ratio", "http_p95_ms",
                "memory_available_pct", "cpu_busy_pct", "node_load1")
HOST_FEATURES = ("memory_available_pct", "cpu_busy_pct", "node_load1")
ARMS = {"aws_only": AWS_FEATURES, "aws_ripe": AWS_FEATURES + tuple(PATH_COLUMNS)}
MODELS = ("supervised", "anomaly")


# --------------------------------------------------------------------------- rows
def _amp_values(r: dict) -> dict:
    v = {k: r.get(k) for k in AWS_FEATURES if k != "memory_available_pct"}
    tot, avail = r.get("memory_total_bytes"), r.get("memory_available_bytes")
    v["memory_available_pct"] = 100.0 * avail / tot if tot and avail is not None else None
    return v


def build_rows(amp: dict[str, list[dict]], index: ProbeIndex, slots: list[dict]) -> list[dict]:
    """One row per (region, AMP timestamp) with label impaired / healthy / dropped."""
    p = PROTOCOL
    rows = []
    for region, amp_rows in amp.items():
        ripe_ts = [s["ts"] for k in index.probes(region) for s in index.series[(region, k)]]
        if not ripe_ts:
            continue
        lo, hi = min(ripe_ts) + p["ripe_warmup_seconds"], max(ripe_ts)
        mine = [s for s in slots if s["region"] == region]
        for r in amp_rows:
            t = float(r["ts"])
            if not lo <= t <= hi:
                continue
            label, slot = "healthy", None
            for s in mine:
                a, b = s["request_unix"], s["request_unix"] + s["duration_seconds"]
                if s["effect_confirmed"] and a <= t <= b:
                    label, slot = "impaired", s["slot"]
                    break
                if a - p["pre_pad_seconds"] <= t <= b + p["recovery_pad_seconds"]:
                    label = "dropped"
            if label == "dropped":
                continue
            values = _amp_values(r) | path_features(index, region, t)
            rows.append({"region": region, "ts": t, "label": label, "slot": slot,
                         "values": {k: values.get(k) for k in ARMS["aws_ripe"]}})
    rows.sort(key=lambda x: (x["region"], x["ts"]))
    return rows


def folds_from_slots(slots: list[dict], start: float, end: float) -> list[tuple[float, float]]:
    """Contiguous time folds, cut halfway between consecutive slots, so no trial is split."""
    ordered = sorted(slots, key=lambda s: s["request_unix"])
    k = PROTOCOL["slots_per_fold"]
    groups = [ordered[i:i + k] for i in range(0, len(ordered), k)]
    if len(groups) > 1 and len(groups[-1]) < k // 2 + 1:
        groups[-2].extend(groups.pop())
    cuts = [start]
    for g, nxt in zip(groups, groups[1:]):
        last_end = max(s["request_unix"] + s["duration_seconds"] for s in g)
        cuts.append((last_end + nxt[0]["request_unix"]) / 2)
    cuts.append(end + 1)
    return list(zip(cuts, cuts[1:]))


def forward_split(slots: list[dict]) -> float:
    """Cut before the first confirmed trial of the second half."""
    conf = sorted((s for s in slots if s["effect_confirmed"]), key=lambda s: s["request_unix"])
    nxt = conf[len(conf) // 2]
    prev = conf[len(conf) // 2 - 1]
    return (prev["request_unix"] + prev["duration_seconds"] + nxt["request_unix"]) / 2


# ------------------------------------------------------------------------- models
def _matrix(rows, names):
    nan = float("nan")
    return [[nan if r["values"].get(n) is None else float(r["values"][n]) for n in names] for r in rows]


def fit_predict(train: list[dict], test: list[dict], names: tuple[str, ...], model: str) -> list[bool]:
    from sklearn.ensemble import IsolationForest
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    if not test:
        return []
    pre = [("imputer", SimpleImputer(strategy="median", keep_empty_features=True)), ("scale", StandardScaler())]
    if model == "supervised":
        y = [r["label"] == "impaired" for r in train]
        if not any(y) or all(y):
            raise ValueError("supervised training needs both classes")
        pipe = Pipeline(pre + [("clf", LogisticRegression(**PROTOCOL["logistic_regression"]))])
        pipe.fit(_matrix(train, names), y)
        j = list(pipe.classes_).index(True)
        prob = pipe.predict_proba(_matrix(test, names))[:, j]
        return [bool(x >= PROTOCOL["supervised_min_probability"]) for x in prob]
    if model == "anomaly":
        healthy = [r for r in train if r["label"] == "healthy"]
        pipe = Pipeline(pre + [("iso", IsolationForest(**PROTOCOL["isolation_forest"]))])
        pipe.fit(_matrix(healthy, names))
        thr = float(np.quantile(-pipe.score_samples(_matrix(healthy, names)), PROTOCOL["anomaly_quantile"]))
        return [bool(s > thr) for s in -pipe.score_samples(_matrix(test, names))]
    raise ValueError(model)


def frozen_rule_alerts(rows: list[dict], amp: dict[str, list[dict]], index: ProbeIndex) -> dict:
    """Context only: the unchanged pre-registered rules on the same rows."""
    out = {}
    for r in rows:
        region, t = r["region"], r["ts"]
        hist = [a.get("http_p95_ms") for a in amp[region] if t - 6 * 3600 <= a["ts"] < t]
        vals = {f"{region}.{k}": v for k, v in r["values"].items() if k in AWS_FEATURES}
        aws = bool(aws_view_alert(vals, True, region, hist))
        ripe = bool(ripe_path_alert(r["values"], "primary"))
        out[(region, t)] = {"aws_rule": aws, "aws_ripe_rule": aws or ripe}
    return out


# ------------------------------------------------------------------------ scoring
def poisson_ci(k: int, alpha: float = .05) -> tuple[float, float]:
    from scipy.stats import chi2
    lo = 0.0 if k == 0 else chi2.ppf(alpha / 2, 2 * k) / 2
    return lo, chi2.ppf(1 - alpha / 2, 2 * k + 2) / 2


def binom_ci(k: int, n: int, alpha: float = .05) -> tuple[float, float] | None:
    from scipy.stats import beta
    if n == 0:
        return None
    lo = 0.0 if k == 0 else beta.ppf(alpha / 2, k, n - k + 1)
    hi = 1.0 if k == n else beta.ppf(1 - alpha / 2, k + 1, n - k)
    return float(lo), float(hi)


def score(rows: list[dict], alerts: dict, slots: list[dict]) -> dict:
    """`alerts`: (region, ts) -> bool for held-out rows only."""
    step = PROTOCOL["row_step_seconds"]
    healthy = sorted((r for r in rows if r["label"] == "healthy" and (r["region"], r["ts"]) in alerts),
                     key=lambda r: (r["region"], r["ts"]))
    onsets, in_alert, prev = 0, 0, None
    for r in healthy:
        a = alerts[(r["region"], r["ts"])]
        contiguous = prev is not None and prev[0] == r["region"] and r["ts"] - prev[1] <= step * 1.5
        if a and not (contiguous and prev[2]):
            onsets += 1
        in_alert += a
        prev = (r["region"], r["ts"], a)
    hours = len(healthy) * step / 3600
    lo, hi = poisson_ci(onsets)
    by_region = defaultdict(list)
    for r in rows:
        by_region[r["region"]].append(r)
    trials, controls = [], []
    for s in slots:
        if not s["effect_confirmed"]:
            continue
        a, b = s["request_unix"], s["request_unix"] + s["duration_seconds"]
        tr = [r for r in by_region[s["region"]] if r["label"] == "impaired" and r["slot"] == s["slot"]
              and (r["region"], r["ts"]) in alerts]
        if tr:
            first = next((r["ts"] for r in tr if alerts[(r["region"], r["ts"])]), None)
            trials.append({"slot": s["slot"], "region": s["region"], "loss_percent": s["loss_percent"],
                           "start_utc": iso(a), "rows": len(tr), "detected": first is not None,
                           "delay_s": first - a if first is not None else None})
        cr = [r for r in by_region[s["control_region"]] if s["control_start_unix"] <= r["ts"] <= s["control_end_unix"]
              and r["label"] == "healthy" and (r["region"], r["ts"]) in alerts]
        if cr:
            controls.append({"slot": s["slot"], "region": s["control_region"], "rows": len(cr),
                             "alarmed": any(alerts[(r["region"], r["ts"])] for r in cr)})
    det = sum(t["detected"] for t in trials)
    by_loss = defaultdict(lambda: [0, 0])
    for t in trials:
        by_loss[t["loss_percent"]][0] += 1
        by_loss[t["loss_percent"]][1] += t["detected"]
    delays = [t["delay_s"] for t in trials if t["delay_s"] is not None]
    return {"trials_evaluated": len(trials), "trials_detected": det, "recall_ci95": binom_ci(det, len(trials)),
            "detection_by_loss_percent": {str(k): {"trials": v[0], "detected": v[1]} for k, v in sorted(by_loss.items())},
            "median_delay_s": statistics.median(delays) if delays else None,
            "healthy_hours": round(hours, 3), "healthy_rows": len(healthy),
            "false_alarm_onsets": onsets,
            "false_alarms_per_day": round(24 * onsets / hours, 2) if hours else None,
            "false_alarms_per_day_ci95": [round(24 * lo / hours, 2), round(24 * hi / hours, 2)] if hours else None,
            "healthy_rows_in_alert_pct": round(100 * in_alert / len(healthy), 2) if healthy else None,
            "controls_evaluated": len(controls), "controls_alarmed": sum(c["alarmed"] for c in controls),
            "trials": trials, "controls": controls}


# --------------------------------------------------------------------------- study
def _train_rows(rows, lo, hi, embargo):
    return [r for r in rows if r["ts"] < lo - embargo or r["ts"] >= hi + embargo]


def run_study(rows: list[dict], slots: list[dict], amp: dict, index: ProbeIndex) -> dict:
    emb = PROTOCOL["embargo_seconds"]
    start, end = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    folds = folds_from_slots(slots, start, end)
    cut = forward_split(slots)
    splits = {"blocked": [(lo, hi) for lo, hi in folds], "forward": [(cut, end + 1)]}
    report = {"report_version": REPORT_VERSION, "protocol": PROTOCOL, "mode": "offline_shadow_only",
              "row_span_utc": [iso(start), iso(end)],
              "rows": {lab: sum(r["label"] == lab for r in rows) for lab in ("healthy", "impaired")},
              "folds_utc": [[iso(a), iso(min(b, end))] for a, b in folds], "forward_cut_utc": iso(cut),
              "excluded_unconfirmed_slots": [s["slot"] for s in slots if not s["effect_confirmed"]],
              "results": {}}
    for split, parts in splits.items():
        for model in MODELS:
            for arm, names in ARMS.items():
                for host in (True, False):
                    feats = names if host else tuple(n for n in names if n not in HOST_FEATURES)
                    alerts = {}
                    for lo, hi in parts:
                        for region in sorted({r["region"] for r in rows}):
                            reg = [r for r in rows if r["region"] == region]
                            test = [r for r in reg if lo <= r["ts"] < hi]
                            train = (_train_rows(reg, lo, hi, emb) if split == "blocked"
                                     else [r for r in reg if r["ts"] < lo - emb])
                            for r, a in zip(test, fit_predict(train, test, feats, model)):
                                alerts[(r["region"], r["ts"])] = a
                    key = f"{split}/{model}/{arm}" + ("" if host else "_no_host")
                    report["results"][key] = score(rows, alerts, slots)
        held = {(r["region"], r["ts"]) for r in rows if any(lo <= r["ts"] < hi for lo, hi in parts)}
        rule = frozen_rule_alerts([r for r in rows if (r["region"], r["ts"]) in held], amp, index)
        for name in ("aws_rule", "aws_ripe_rule"):
            report["results"][f"{split}/frozen/{name}"] = score(rows, {k: v[name] for k, v in rule.items()}, slots)
    return report


def summary_table(report: dict) -> str:
    lines = ["| key | trials | FA onsets | healthy h | FA/day (95% CI) | healthy rows in alert | controls alarmed |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for k, v in report["results"].items():
        ci = v["false_alarms_per_day_ci95"]
        lines.append(f"| {k} | {v['trials_detected']}/{v['trials_evaluated']} | {v['false_alarm_onsets']} | "
                     f"{v['healthy_hours']} | {v['false_alarms_per_day']} ({ci[0]}-{ci[1]}) | "
                     f"{v['healthy_rows_in_alert_pct']}% | {v['controls_alarmed']}/{v['controls_evaluated']} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign-dir", type=Path, required=True)
    ap.add_argument("--campaign-plan", type=Path, required=True)
    ap.add_argument("--ripe", action="append", required=True, help="region=path to RIPE results JSON")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    spec = defaultdict(list)
    for v in args.ripe:
        region, _, path = v.partition("=")
        spec[region].append(Path(path))
    samples = [s for s in load_samples(spec) 
               if s["probe"] not in PROTOCOL["excluded_probes"].get(s["region"], [])]
    index = ProbeIndex(samples)
    read = lambda p: [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    plan = json.loads(args.campaign_plan.read_text(encoding="utf-8"))
    slots = campaign_slots(plan, read(args.campaign_dir / "campaign-log.jsonl"), args.campaign_dir / "trials")
    amp = {r: read(args.campaign_dir / f"amp-{r}.jsonl") for r in spec}
    rows = build_rows(amp, index, slots)
    report = run_study(rows, slots, amp, index)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "campaign-same-day-ml.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(summary_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
