"""Live shadow monitoring (14-18 Nov 2026) and the pre-registered Wednesday retrain. SHADOW ONLY.

Nothing in this module can trigger a mitigation: it reads local files (public RIPE
results, a read-only AMP export) and writes JSON/Markdown reports. The frozen RIPE rule
(`ripe_value.ripe_path_alert`) is scored unchanged, for context only.

Two jobs
--------
* ``snapshot``: the *current* model (the 13 Nov same-day anomaly model, retrained the
  same way on every campaign healthy row) scored on the live data so far, one row per
  Singapore-time day. Live time is unreviewed while the run is on, so these are *alert
  onsets*; they become false alarms only after the exclusion review (SSM history and
  operator notes) on Wednesday.
* ``retrain``: the comparison in LIVE_PROTOCOL, which is committed BEFORE the first live
  RIPE result exists. Current vs retrained model on the same held-out live hours (false
  alarms) and on the 20 campaign trials (detection, blocked split as in campaign_ml).
  Nothing in the protocol may change after live data arrives.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess

from . import campaign_ml as cm
from .ripe_value import (PATH_COLUMNS, ProbeIndex, aws_view_alert, campaign_slots, iso,
                         load_samples, path_features, ripe_path_alert)

SGT = timezone(timedelta(hours=8))
STEP = cm.PROTOCOL["row_step_seconds"]

# ----------------------------------------------------------------------------------
# PRE-REGISTERED (committed before any live data). Do not edit after 2026-11-14.
# ----------------------------------------------------------------------------------
LIVE_PROTOCOL = {
    "version": "adaptivenet.live_shadow.v1",
    "live_stop_utc": "2026-11-18T01:00:00+00:00",        # Wed 18 Nov 09:00 SGT
    "held_out_start_utc": "2026-11-16T13:00:00+00:00",   # last 36 h of the run
    "embargo_seconds": 900,                               # gap between live train and held-out
    "ripe_warmup_seconds": cm.PROTOCOL["ripe_warmup_seconds"],
    "exclusion_pre_pad_seconds": 60,
    "exclusion_post_pad_seconds": 600,
    "excluded_probes": cm.PROTOCOL["excluded_probes"],    # 1010367, Singapore only
    "hyperparameters": "campaign_ml.PROTOCOL unchanged (IsolationForest 300 trees, seed 42, q=0.995)",
    "primary": "anomaly/aws_ripe_no_host",
    "secondary": ["anomaly/aws_ripe (with host columns)",
                  "persistence_2: primary, alert only on 2 consecutive 30 s rows in alert",
                  "persistence_2 on the with-host model"],
    "current_model": "trained on every campaign healthy row (12-13 Nov), per region",
    "retrained_model": "campaign healthy rows + live rows from live start to held_out_start - embargo",
    "false_alarms": "alert onsets on held-out live rows outside every exclusion window; Poisson 95% CI",
    "detection": "20 campaign trials, campaign_ml blocked 5-fold split; the retrained model adds "
                 "the live training rows to every fold's training set",
    "exclusions": "every SSM command in either region during the run (read-only list-commands), "
                  "plus incidents the operator reports, fixed before the retrain is scored",
    "decision": "report both models side by side; no threshold or feature is tuned on live data",
    # Amendment 1 (2026-11-14, still before any live data): the Mac is off from about
    # 15 Nov 03:00 SGT to 16 Nov 14:00 SGT, so the witness traffic stops. The model was
    # trained with witness traffic, so rows without it are not comparable. Rule fixed from
    # the witness log only, never from alerts.
    "witness_gap_seconds": 60,
    "witness_gap_rule": "a row is dropped (train and held-out) when the HTTP witness log has no "
                        "record in the 60 s up to the row; reported as a row count",
    # Amendment 2 (2026-11-14 04:40 UTC, before any live scoring or snapshot): MY corrected the
    # Mac's off time to Mon 16 Nov 03:00-14:00 SGT (not Sunday). Rules, split and held-out are
    # unchanged; the witness-gap rule above is driven by the witness log, not by these times.
    "amendment_2_expected_gap_sgt": "2026-11-16 03:00 to 2026-11-16 14:00",
    # Amendment 5 (2026-11-14 10:10 UTC, before any scoring): MY leaves with the Mac Wed 18 Nov
    # between 07:00 and 08:00 SGT (team review 15:00 SGT), so the run ends at Wed 07:00 SGT instead
    # of 09:00. Held-out start is unchanged; held-out shrinks from 36 h to 34 h.
    "amendment_5_live_stop_utc": "2026-11-17T23:00:00+00:00",
    # Amendment 6 (2026-11-16 03:00 UTC, before any held-out data and before the candidate search):
    # a restart at boot truncated witness.stdout and the journal had rotated, so the witness log
    # for Sat 14 Nov 08:44 to Mon 16 Nov 01:11 SGT is lost. The origin request rate in AMP is an
    # independent record of the witness: region-a http_rps was >= 0.126 whenever the log shows the
    # witness on (Sat-Sun, and Mon 01:11-05:18), region-b >= 0.063, and both exactly 0 while the Mac
    # was off. A row now counts
    # as witnessed if the log shows a record OR that region's http_rps >= 0.03 (about half the lower
    # witness-only rate). Chosen from on/off periods only, never from alerts.
    "amendment_6_min_origin_rps": 0.03,
}
LIVE_PROTOCOL["live_stop_utc"] = LIVE_PROTOCOL["amendment_5_live_stop_utc"]
# Amendment 3 (2026-11-14 05:30 UTC, before any snapshot or scoring of live data): candidate
# search requested by MY. Candidates are compared ONLY on (a) a validation block inside the
# live training period and (b) the 20 campaign trials (blocked split). One candidate is picked
# by SELECTION_RULE and frozen in a commit before HELD-OUT starts; every candidate is then also
# scored once on held-out and all are reported. Held-out is never used to choose.
CANDIDATE_SEARCH = {
    "fit_window_utc": ["live start", "2026-11-15T00:00:00+00:00"],      # Sat 08:55 to Sun 08:00 SGT
    "validation_window_utc": ["2026-11-15T00:15:00+00:00", "2026-11-15T19:00:00+00:00"],  # to Mon 03:00 SGT
    "grid": {
        "model": ["isolation_forest", "elliptic_envelope", "robust_z_max"],
        "features": ["aws_ripe_no_host", "aws_ripe_no_host_no_rtt", "ripe_loss_only"],
        "threshold_quantile": [0.995, 0.999],
        "persistence_rows": [1, 2, 3],
        "training_data": ["campaign+live", "live_only"],
    },
    "notes": {"robust_z_max": "max over features of |x - median| / (1.4826 MAD) on training healthy rows",
              "aws_ripe_no_host_no_rtt": "drops ripe_max_rtt_inflation_ms and ripe_paths_inflated",
              "ripe_loss_only": "ripe_window_loss_pct, ripe_window_probes_with_loss, ripe_loss_pct_reachable, "
                                "ripe_reachable_probes"},
    "selection_rule": "among candidates with validation FA/day <= 2.0, pick the most campaign trials "
                      "detected; ties -> lower validation FA/day, then simpler (persistence, then fewer features). "
                      "If none qualify, pick the lowest validation FA/day.",
    "freeze_by_utc": "2026-11-16T12:45:00+00:00",                        # Mon 20:45 SGT
}
# Amendment 7 (2026-11-16 ~13:40 UTC, chosen by MY; before the Tuesday trials and before any
# held-out scoring). The Monday pick (57cc91d) is kept and reported as is, but it is defective:
# trained on live data only, 10 of its 11 columns have MAD 0, so it only watches ripe_result_age_s
# and alarms in 20/20 campaign controls (370 FA/day on campaign healthy rows). The selection rule
# never looked at controls. A second pick uses the same rule plus a controls gate, and every
# candidate's campaign controls and campaign healthy FA are reported. The frozen rule's declared
# secondary variants are scored as context only.
AMENDMENT_7 = {"max_controls_alarmed": 1, "rule_variants": ("loss_only", "loss_or_rtt2")}
# Amendment 8 (2026-11-16 ~16:30 UTC, chosen by MY; before the Tuesday trials, no held-out data read):
# early-warning test. Origin, stated honestly: the reviewer's exploratory look at the 12 Nov campaign
# day (16 Nov ~16:20 UTC) suggested an ML early warning beside the frozen loss_only rule (13 + 2 more
# trials; earlier on 8 of 13 shared, median 210 s). The Tuesday trials are the unseen test.
# Role: early warning only; an alert asks for a confirming check and never moves traffic.
AMENDMENT_8 = {
    "pick_rule": "from the 108 candidates: campaign controls alarmed <= 1/20 and validation FA <= 5/day, "
                 "then most campaign trials; ties -> lower validation FA, then simpler",
    "pick": "isolation_forest|aws_ripe_no_host_no_rtt|q0.995|p2|live_only",   # computed from search-report, 15/20, 1/20, 3.84/day
    "comparator": "frozen_aws_ripe_rule_loss_only",   # frozen rule, loss_only variant, unchanged
    "pass_mark": "pick detects >= rule on the Tuesday trials AND (catches >= 1 trial the rule misses OR is earlier "
                 "on > half of the shared trials) AND pick held-out FA <= 5/day AND (addendum) pick alarms in "
                 "<= 1 of the 10 Tuesday same-time controls in the other region",
    "max_held_out_fa_per_day": 5.0,
    "max_tuesday_controls_alarmed": 1,   # addendum, 2026-11-16 ~16:30 UTC, before the trials
}
MODELS = {  # name -> (feature names, persistence rows)
    "anomaly_aws_ripe_no_host": (tuple(n for n in cm.ARMS["aws_ripe"] if n not in cm.HOST_FEATURES), 1),
    "anomaly_aws_ripe": (cm.ARMS["aws_ripe"], 1),
    "anomaly_aws_ripe_no_host_persist2": (tuple(n for n in cm.ARMS["aws_ripe"] if n not in cm.HOST_FEATURES), 2),
    "anomaly_aws_ripe_persist2": (cm.ARMS["aws_ripe"], 2),
}
PRIMARY = "anomaly_aws_ripe_no_host"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sgt_day(t: float) -> str:
    return datetime.fromtimestamp(t, SGT).strftime("%Y-%m-%d")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.is_file() else []


# ------------------------------------------------------------------------------ rows
def load_index(spec: dict[str, list[Path]]) -> ProbeIndex:
    ex = LIVE_PROTOCOL["excluded_probes"]
    return ProbeIndex([s for s in load_samples(spec) if s["probe"] not in ex.get(s["region"], [])])


def in_exclusion(region: str, t: float, exclusions: list[dict]) -> bool:
    pre, post = LIVE_PROTOCOL["exclusion_pre_pad_seconds"], LIVE_PROTOCOL["exclusion_post_pad_seconds"]
    return any(e.get("region", "both") in ("both", region)
               and ts(e["start_utc"]) - pre <= t <= ts(e["end_utc"]) + post for e in exclusions)


def witness_times(path: Path) -> list[float]:
    """Witness record times. The journal (`path`) rotates at a size cap and drops old records,
    so the full history is also read from the witness's own stdout log, which keeps every
    record (bug fix 2026-11-15, found by the health check before any live scoring)."""
    out = set()
    for p in (path, path.with_name("witness.stdout")):
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("{"):
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("timestamp"):
                        out.add(ts(r["timestamp"]))
    return sorted(out)


def witness_ok(times: list[float] | None, t: float) -> bool:
    if times is None:
        return True
    i = bisect_left(times, t - LIVE_PROTOCOL["witness_gap_seconds"])
    return i < len(times) and times[i] <= t


def live_rows(amp: dict[str, list[dict]], index: ProbeIndex, exclusions: list[dict],
              until: float | None = None, witness: list[float] | None = None) -> list[dict]:
    """A 30 s grid per region from RIPE warm-up to the last RIPE result (or `until`).

    A grid point carries the AMP values at that exact timestamp when the export has
    them (``aws`` True); otherwise only RIPE features (``aws`` False, ML not scored).
    Rows inside a padded exclusion window are dropped, never labelled.
    """
    rows = []
    for region in sorted({r for r, _ in index.series}):
        ripe_ts = [s["ts"] for k in index.probes(region) for s in index.series[(region, k)]]
        if not ripe_ts:
            continue
        lo = math.ceil((min(ripe_ts) + LIVE_PROTOCOL["ripe_warmup_seconds"]) / STEP) * STEP
        hi = min(max(ripe_ts), until or float("inf"), ts(LIVE_PROTOCOL["live_stop_utc"]))
        by_ts = {float(r["ts"]): r for r in amp.get(region, [])}
        t = lo
        while t <= hi:
            a = by_ts.get(t)
            seen = witness_ok(witness, t) or (a is not None and (a.get("http_rps") or 0) >= LIVE_PROTOCOL["amendment_6_min_origin_rps"])
            if not in_exclusion(region, t, exclusions) and seen:
                values = (cm._amp_values(a) if a else {}) | path_features(index, region, t)
                rows.append({"region": region, "ts": t, "label": "healthy", "slot": None, "aws": a is not None,
                             "values": {k: values.get(k) for k in cm.ARMS["aws_ripe"]}})
            t += STEP
    return rows


def campaign_rows(campaign_dir: Path, plan: Path, ripe: dict[str, list[Path]]) -> tuple[list[dict], list[dict]]:
    index = load_index(ripe)
    slots = campaign_slots(json.loads(plan.read_text(encoding="utf-8")),
                           read_jsonl(campaign_dir / "campaign-log.jsonl"), campaign_dir / "trials")
    amp = {r: read_jsonl(campaign_dir / f"amp-{r}.jsonl") for r in ripe}
    return cm.build_rows(amp, index, slots), slots


# --------------------------------------------------------------------------- scoring
def persist(alerts: dict, k: int) -> dict:
    """Alert only when the last k contiguous rows (same region, 30 s apart) are all in alert."""
    if k <= 1:
        return dict(alerts)
    out, run, prev = {}, 0, None
    for key in sorted(alerts):
        region, t = key
        contiguous = prev is not None and prev[0] == region and t - prev[1] <= STEP * 1.5
        run = (run + 1 if contiguous else 1) if alerts[key] else 0
        out[key] = run >= k
        prev = key
    return out


def predict(train: list[dict], test: list[dict], model: str) -> dict:
    feats, k = MODELS[model]
    alerts = {}
    for region in sorted({r["region"] for r in test}):
        tr = [r for r in train if r["region"] == region]
        te = [r for r in test if r["region"] == region]
        for r, a in zip(te, cm.fit_predict(tr, te, feats, "anomaly")):
            alerts[(r["region"], r["ts"])] = a
    return persist(alerts, k)


def frozen_rules(rows: list[dict], amp: dict[str, list[dict]]) -> dict[str, dict]:
    """Unchanged frozen rules; the 6 h p95 history is found by bisection (same window as cm)."""
    out = {"frozen_aws_ripe_rule": {}, "frozen_ripe_rule": {}}
    for v in AMENDMENT_7["rule_variants"]:   # amendment 7 context only; the frozen rule stays primary
        out[f"frozen_aws_ripe_rule_{v}"], out[f"frozen_ripe_rule_{v}"] = {}, {}
    series = {r: sorted(amp.get(r, []), key=lambda x: x["ts"]) for r in amp}
    stamps = {r: [x["ts"] for x in v] for r, v in series.items()}
    for row in rows:
        region, t, key = row["region"], row["ts"], (row["region"], row["ts"])
        ripe = ripe_path_alert(row["values"], "primary")
        out["frozen_ripe_rule"][key] = bool(ripe)
        if row.get("aws", True):
            st = stamps.get(region, [])
            hist = [x.get("http_p95_ms") for x in series[region][bisect_left(st, t - 6 * 3600):bisect_left(st, t)]]
            vals = {f"{region}.{k}": v for k, v in row["values"].items() if k in cm.AWS_FEATURES}
            aws = bool(aws_view_alert(vals, True, region, hist))
            out["frozen_aws_ripe_rule"][key] = aws or bool(ripe)
        for v in AMENDMENT_7["rule_variants"]:
            rv = bool(ripe_path_alert(row["values"], v))
            out[f"frozen_ripe_rule_{v}"][key] = rv
            if row.get("aws", True):
                out[f"frozen_aws_ripe_rule_{v}"][key] = aws or rv
    return out


def fa_summary(rows: list[dict], alerts: dict) -> dict:
    s = cm.score(rows, alerts, [])
    return {k: s[k] for k in ("healthy_hours", "healthy_rows", "false_alarm_onsets", "false_alarms_per_day",
                              "false_alarms_per_day_ci95", "healthy_rows_in_alert_pct")}


def by_day(rows: list[dict], alerts: dict) -> dict:
    days = defaultdict(list)
    for r in rows:
        if (r["region"], r["ts"]) in alerts:
            days[sgt_day(r["ts"])].append(r)
    return {d: fa_summary(v, alerts) for d, v in sorted(days.items())}


# -------------------------------------------------------------------------- snapshot
def snapshot(camp: list[dict], rows: list[dict], amp: dict, meta: dict) -> dict:
    aws_rows = [r for r in rows if r["aws"]]
    report = {"report_version": LIVE_PROTOCOL["version"] + ".snapshot", "mode": "offline_shadow_only",
              "generated_utc": iso(datetime.now(timezone.utc).timestamp()), **meta,
              "note": ("Live time is unreviewed until the Wednesday exclusion review, so counts are alert "
                       "onsets on presumed-healthy time. There are no fault trials in the live run, so "
                       "detection is only measured on the 20 campaign trials (retrain report)."),
              "rows": {"total": len(rows), "with_aws": len(aws_rows)},
              "detectors": {}}
    for name in ("anomaly_aws_ripe_no_host", "anomaly_aws_ripe_no_host_persist2", "anomaly_aws_ripe"):
        if aws_rows:
            report["detectors"][f"current_{name}"] = by_day(aws_rows, predict(camp, aws_rows, name))
    rules = frozen_rules(rows, amp)
    report["detectors"]["frozen_aws_ripe_rule"] = by_day(aws_rows, rules["frozen_aws_ripe_rule"])
    report["detectors"]["frozen_ripe_rule"] = by_day(rows, rules["frozen_ripe_rule"])
    return report


def snapshot_markdown(rep: dict) -> str:
    lines = [f"# Live shadow snapshot ({rep['generated_utc']})", "",
             f"RIPE data through {rep.get('ripe_through_utc')}; AWS data through {rep.get('aws_through_utc')}.",
             f"AWS export: {rep.get('aws_status')}.", "", rep["note"], "",
             "Alert onsets per Singapore-time day (hours of data in brackets):", "",
             "| detector | " + " | ".join(sorted({d for v in rep["detectors"].values() for d in v})) + " |"]
    days = sorted({d for v in rep["detectors"].values() for d in v})
    lines.append("|---|" + "---:|" * len(days))
    for name, v in rep["detectors"].items():
        cells = [f"{v[d]['false_alarm_onsets']} ({v[d]['healthy_hours']} h)" if d in v else "n/a" for d in days]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- retrain
def retrain(camp: list[dict], slots: list[dict], rows: list[dict], amp: dict, exclusions: list[dict]) -> dict:
    held_lo = ts(LIVE_PROTOCOL["held_out_start_utc"])
    emb = LIVE_PROTOCOL["embargo_seconds"]
    aws_rows = [r for r in rows if r["aws"]]
    live_train = [r for r in aws_rows if r["ts"] < held_lo - emb]
    held = [r for r in aws_rows if r["ts"] >= held_lo]
    if not live_train or not held:
        raise SystemExit("live training or held-out rows missing; export AMP for the whole run first")
    camp_healthy = [r for r in camp if r["label"] == "healthy"]
    report = {"report_version": LIVE_PROTOCOL["version"] + ".retrain", "mode": "offline_shadow_only",
              "protocol": LIVE_PROTOCOL, "campaign_protocol": cm.PROTOCOL, "exclusions": exclusions,
              "live_train_span_utc": [iso(min(r["ts"] for r in live_train)), iso(max(r["ts"] for r in live_train))],
              "held_out_span_utc": [iso(min(r["ts"] for r in held)), iso(max(r["ts"] for r in held))],
              "rows": {"campaign_healthy": len(camp_healthy), "live_train": len(live_train), "held_out": len(held),
                       "live_rows_without_aws": len(rows) - len(aws_rows)},
              "results": {}}
    start, end = min(r["ts"] for r in camp), max(r["ts"] for r in camp)
    folds = cm.folds_from_slots(slots, start, end)
    cemb = cm.PROTOCOL["embargo_seconds"]
    for name in MODELS:
        for which, extra in (("current", []), ("retrained", live_train)):
            fa = fa_summary(held, predict(camp_healthy + extra, held, name))
            alerts = {}
            for lo, hi in folds:
                test = [r for r in camp if lo <= r["ts"] < hi]
                alerts |= predict(cm._train_rows(camp, lo, hi, cemb) + extra, test, name)
            det = cm.score(camp, alerts, slots)
            report["results"][f"{which}/{name}"] = {
                "held_out_live": fa,
                "campaign_trials": {k: det[k] for k in ("trials_evaluated", "trials_detected", "recall_ci95",
                                                        "detection_by_loss_percent", "median_delay_s",
                                                        "false_alarms_per_day", "false_alarms_per_day_ci95")},
                "campaign_trials_missed": [t["slot"] for t in det["trials"] if not t["detected"]]}
    rules = frozen_rules(held, amp)
    for name, al in rules.items():
        report["results"][f"context/{name}"] = {"held_out_live": fa_summary(held, al)}
    return report


def retrain_markdown(rep: dict) -> str:
    lines = ["# Wednesday retrain: current vs retrained (shadow only)", "",
             f"Held-out live: {rep['held_out_span_utc'][0]} to {rep['held_out_span_utc'][1]} "
             f"({rep['rows']['held_out']} rows). Live training rows: {rep['rows']['live_train']}.", "",
             "| model | held-out FA/day (95% CI) | FA onsets | held-out h | campaign trials detected | missed slots |",
             "|---|---:|---:|---:|---:|---|"]
    for k, v in rep["results"].items():
        h = v["held_out_live"]
        c = v.get("campaign_trials")
        ci = h["false_alarms_per_day_ci95"]
        lines.append(f"| {k} | {h['false_alarms_per_day']} ({ci[0]}-{ci[1]}) | {h['false_alarm_onsets']} | "
                     f"{h['healthy_hours']} | " + (f"{c['trials_detected']}/{c['trials_evaluated']}" if c else "n/a")
                     + f" | {v.get('campaign_trials_missed', '')} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ candidate search
# Held-out scoring of candidates (decided with the search code, 2026-11-14, before any live
# scoring): each candidate is refit on all live rows before held_out_start - embargo (plus the
# campaign healthy rows when its training_data is campaign+live), like the planned retrain.
FEATURE_SETS = {
    "aws_ripe_no_host": MODELS[PRIMARY][0],
    "aws_ripe_no_host_no_rtt": tuple(n for n in MODELS[PRIMARY][0]
                                     if n not in ("ripe_max_rtt_inflation_ms", "ripe_paths_inflated")),
    "ripe_loss_only": ("ripe_window_loss_pct", "ripe_window_probes_with_loss", "ripe_loss_pct_reachable",
                       "ripe_reachable_probes"),
}


def _raw_scores(train: list[dict], test: list[dict], feats: tuple, model: str, q: float) -> list[bool]:
    """Unsupervised score fit on TRAIN healthy rows; alert above the q-quantile of train scores."""
    import numpy as np
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    healthy = [r for r in train if r["label"] == "healthy"]
    if not test or not healthy:
        return [False] * len(test)
    imp = SimpleImputer(strategy="median", keep_empty_features=True)
    Xtr = imp.fit_transform(np.array(cm._matrix(healthy, feats), dtype=float))
    Xte = imp.transform(np.array(cm._matrix(test, feats), dtype=float))
    if model == "robust_z_max":
        med = np.median(Xtr, axis=0)
        mad = 1.4826 * np.median(np.abs(Xtr - med), axis=0)
        mad[mad == 0] = np.inf  # a constant column cannot raise an alarm
        str_, ste = (np.max(np.abs(X - med) / mad, axis=1) for X in (Xtr, Xte))
    else:
        sc = StandardScaler().fit(Xtr)
        Xtr_s, Xte_s = sc.transform(Xtr), sc.transform(Xte)
        if model == "isolation_forest":
            from sklearn.ensemble import IsolationForest
            m = IsolationForest(**cm.PROTOCOL["isolation_forest"]).fit(Xtr_s)
        elif model == "elliptic_envelope":
            from sklearn.covariance import EllipticEnvelope
            keep = Xtr_s.std(axis=0) > 0
            Xtr_s, Xte_s = Xtr_s[:, keep], Xte_s[:, keep]
            # support_fraction=1.0: the RIPE columns are discrete (probe counts), so the robust
            # MCD subset is often degenerate; 1.0 is the plain covariance (set before live data).
            m = EllipticEnvelope(random_state=42, support_fraction=1.0).fit(Xtr_s)
        else:
            raise ValueError(model)
        str_, ste = -m.score_samples(Xtr_s), -m.score_samples(Xte_s)
    thr = float(np.quantile(str_, q))
    return [bool(x > thr) for x in ste]


def candidate_alerts(train: list[dict], test: list[dict], cand: dict) -> dict:
    alerts = {}
    for region in sorted({r["region"] for r in test}):
        tr = [r for r in train if r["region"] == region]
        te = [r for r in test if r["region"] == region]
        for r, a in zip(te, _raw_scores(tr, te, FEATURE_SETS[cand["features"]], cand["model"],
                                        cand["threshold_quantile"])):
            alerts[(r["region"], r["ts"])] = a
    return alerts


def candidates() -> list[dict]:
    g = CANDIDATE_SEARCH["grid"]
    return [{"model": m, "features": f, "threshold_quantile": q, "persistence_rows": k, "training_data": d}
            for m in g["model"] for f in g["features"] for q in g["threshold_quantile"]
            for d in g["training_data"] for k in g["persistence_rows"]]


def cand_name(c: dict) -> str:
    return f"{c['model']}|{c['features']}|q{c['threshold_quantile']}|p{c['persistence_rows']}|{c['training_data']}"


def _camp_detection(camp, slots, extra, cand, cache):
    """Blocked campaign split. live_only training ignores campaign rows entirely."""
    key = (cand["model"], cand["features"], cand["threshold_quantile"], cand["training_data"])
    if key not in cache:
        alerts = {}
        if cand["training_data"] == "live_only":
            alerts = candidate_alerts(extra, camp, cand)
        else:
            start, end = min(r["ts"] for r in camp), max(r["ts"] for r in camp)
            for lo, hi in cm.folds_from_slots(slots, start, end):
                test = [r for r in camp if lo <= r["ts"] < hi]
                alerts |= candidate_alerts(cm._train_rows(camp, lo, hi, cm.PROTOCOL["embargo_seconds"]) + extra,
                                           test, cand)
        cache[key] = alerts
    return cm.score(camp, persist(cache[key], cand["persistence_rows"]), slots)


def _rolling_slices(rows: list[dict], n: int = 4) -> list[tuple[float, float]]:
    t = sorted(r["ts"] for r in rows)
    if not t:
        return []
    edges = [t[0] + i * (t[-1] - t[0]) / n for i in range(n + 1)]
    return list(zip(edges, edges[1:]))


def search(camp: list[dict], slots: list[dict], rows: list[dict]) -> dict:
    cs = CANDIDATE_SEARCH
    aws = [r for r in rows if r["aws"]]
    fit = [r for r in aws if r["ts"] < ts(cs["fit_window_utc"][1])]
    val = [r for r in aws if ts(cs["validation_window_utc"][0]) <= r["ts"] < ts(cs["validation_window_utc"][1])]
    if not fit or not val:
        raise SystemExit("fit or validation rows missing; export AMP through Mon 03:00 SGT first")
    camp_h = [r for r in camp if r["label"] == "healthy"]
    pre = [r for r in aws if r["ts"] < ts(LIVE_PROTOCOL["held_out_start_utc"]) - LIVE_PROTOCOL["embargo_seconds"]]
    slices = _rolling_slices(pre)
    out, val_cache, roll_cache, det_cache = [], {}, {}, {}
    for c in candidates():
        base = (c["model"], c["features"], c["threshold_quantile"], c["training_data"])
        extra = camp_h if c["training_data"] == "campaign+live" else []
        if base not in val_cache:
            val_cache[base] = candidate_alerts(extra + fit, val, c)
            roll = []
            for lo, hi in slices:   # rolling: fit on pre-held-out live outside the slice (+ embargo)
                tr = [r for r in pre if r["ts"] < lo - 900 or r["ts"] >= hi + 900]
                te = [r for r in pre if lo <= r["ts"] < hi]
                roll.append((te, candidate_alerts(extra + tr, te, c)))
            roll_cache[base] = roll
        v = fa_summary(val, persist(val_cache[base], c["persistence_rows"]))
        rolling = [fa_summary(te, persist(al, c["persistence_rows"]))["false_alarms_per_day"]
                   for te, al in roll_cache[base] if te]
        d = _camp_detection(camp, slots, fit, c, det_cache)
        out.append({"name": cand_name(c), **c, "validation": v, "rolling_fa_per_day": rolling,
                    "campaign_trials_detected": d["trials_detected"], "campaign_trials": d["trials_evaluated"],
                    "campaign_missed": [t["slot"] for t in d["trials"] if not t["detected"]],
                    "campaign_controls_alarmed": d["controls_alarmed"], "campaign_controls": d["controls_evaluated"],
                    "campaign_healthy_fa_per_day": d["false_alarms_per_day"]})
    ok = [c for c in out if (c["validation"]["false_alarms_per_day"] or 0) <= 2.0]
    simple = lambda c: (c["persistence_rows"], len(FEATURE_SETS[c["features"]]))
    if ok:
        pick = sorted(ok, key=lambda c: (-c["campaign_trials_detected"], c["validation"]["false_alarms_per_day"],
                                         *simple(c)))[0]
    else:
        pick = sorted(out, key=lambda c: (c["validation"]["false_alarms_per_day"], -c["campaign_trials_detected"],
                                          *simple(c)))[0]
    # Amendment 7: second pick = same rule, restricted to candidates whose campaign controls
    # alarmed in at most AMENDMENT_7["max_controls_alarmed"] of 20 slots.
    gated = [c for c in ok if c["campaign_controls_alarmed"] <= AMENDMENT_7["max_controls_alarmed"]]
    pick2 = (sorted(gated, key=lambda c: (-c["campaign_trials_detected"], c["validation"]["false_alarms_per_day"],
                                          *simple(c)))[0] if gated else None)
    return {"report_version": LIVE_PROTOCOL["version"] + ".search", "mode": "offline_shadow_only",
            "pick_2": pick2["name"] if pick2 else None, "qualifying_pick_2": len(gated),
            "search": cs, "rows": {"fit": len(fit), "validation": len(val), "pre_held_out": len(pre)},
            "validation_hours": round(len(val) * STEP / 3600, 2), "candidates": out,
            "qualifying": len(ok), "pick": pick["name"],
            "pick_spec": {k: pick[k] for k in ("model", "features", "threshold_quantile",
                                               "persistence_rows", "training_data")}}


def search_markdown(rep: dict) -> str:
    rows = sorted(rep["candidates"], key=lambda c: (-c["campaign_trials_detected"],
                                                   c["validation"]["false_alarms_per_day"]))
    lines = [f"# Candidate search ({len(rows)} candidates, {rep['validation_hours']} validation h, "
             f"{rep['qualifying']} with <= 2 FA/day)", "", f"**Pick (frozen before held-out): {rep['pick']}**", "",
             f"**Pick 2 (amendment 7, at most {AMENDMENT_7['max_controls_alarmed']} of 20 controls alarmed): {rep.get('pick_2')}**", "",
             "| candidate | validation FA/day (95% CI) | rolling FA/day | campaign trials | campaign controls alarmed | campaign healthy FA/day |",
             "|---|---:|---|---:|---:|---:|"]
    for c in rows:
        v = c["validation"]
        ci = v["false_alarms_per_day_ci95"]
        lines.append(f"| {c['name']} | {v['false_alarms_per_day']} ({ci[0]}-{ci[1]}) | {c['rolling_fa_per_day']} | "
                     f"{c['campaign_trials_detected']}/{c['campaign_trials']} | "
                     f"{c.get('campaign_controls_alarmed')}/{c.get('campaign_controls')} | {c.get('campaign_healthy_fa_per_day')} |")
    return "\n".join(lines) + "\n"


def heldout_candidates(camp: list[dict], rows: list[dict]) -> dict:
    held_lo = ts(LIVE_PROTOCOL["held_out_start_utc"])
    aws = [r for r in rows if r["aws"]]
    train = [r for r in aws if r["ts"] < held_lo - LIVE_PROTOCOL["embargo_seconds"]]
    held = [r for r in aws if r["ts"] >= held_lo]
    camp_h = [r for r in camp if r["label"] == "healthy"]
    out, cache = {}, {}
    for c in candidates():
        base = (c["model"], c["features"], c["threshold_quantile"], c["training_data"])
        if base not in cache:
            cache[base] = candidate_alerts((camp_h if c["training_data"] == "campaign+live" else []) + train, held, c)
        out[cand_name(c)] = fa_summary(held, persist(cache[base], c["persistence_rows"]))
    return out


# ---------------------------------------------------------- Tuesday trials (amendment 4)
# Amendment 4 (2026-11-14 10:05 UTC, approved by MY in the thread): 10 packet-loss trials on
# Tue 17 Nov from 11:00 UTC (seed 1711, 5 cycles, same runner and rollback as the campaign).
# They are inside the held-out window: their SSM commands make them exclusions for the false-
# alarm count automatically. Detection on them is scored separately for every model, each
# trained only on data before held_out_start - embargo (the pick frozen beforehand), so the
# trials are fully out-of-sample. Same rules as campaign_ml: impaired = SSM request to
# request + duration of SSM-confirmed slots; controls = the other region at the same time.
TRIALS = {"seed": 1711, "start_utc": "2026-11-17T11:00:00+00:00", "cycles": 5}


def trial_rows_slots(rows_all: list[dict], L: Path):
    """(rows, slots) for the Tuesday trials with campaign_ml labels, or (None, None)."""
    tdir = L / "trials"
    if not (tdir / "campaign-log.jsonl").is_file():
        return None, None
    slots = campaign_slots(json.loads((tdir / "trials-plan.json").read_text(encoding="utf-8")),
                           read_jsonl(tdir / "campaign-log.jsonl"), tdir / "trials")
    if not slots:
        return None, []
    lo = min(s["request_unix"] for s in slots) - 3600
    hi = max(s["request_unix"] + s["duration_seconds"] for s in slots) + 3600
    rows = []
    for r in rows_all:
        if not (lo <= r["ts"] <= hi) or not r["aws"]:
            continue
        r = dict(r)
        for s in slots:
            if s["region"] == r["region"] and s["request_unix"] <= r["ts"] <= s["request_unix"] + s["duration_seconds"]:
                r["label"], r["slot"] = ("impaired", s["slot"]) if s["effect_confirmed"] else ("dropped", None)
        if r["label"] != "dropped":
            rows.append(r)
    return rows, slots


def trial_detection(camp: list[dict], rows_all: list[dict], amp: dict, L: Path) -> dict | None:
    rows, slots = trial_rows_slots(rows_all, L)
    if rows is None and slots is None:
        return None
    if not slots:
        return {"error": "no submitted trials"}
    held_lo = ts(LIVE_PROTOCOL["held_out_start_utc"])
    train = [r for r in rows_all if r["aws"] and r["ts"] < held_lo - LIVE_PROTOCOL["embargo_seconds"]]
    camp_h = [r for r in camp if r["label"] == "healthy"]
    keep = ("trials_evaluated", "trials_detected", "recall_ci95", "detection_by_loss_percent", "median_delay_s",
            "controls_evaluated", "controls_alarmed")
    res = {}
    def put(name, alerts):
        sc = cm.score(rows, alerts, slots)
        res[name] = {k: sc[k] for k in keep} | {"missed": [t["slot"] for t in sc["trials"] if not t["detected"]],
                                                "per_trial": [{k: t[k] for k in ("slot", "detected", "delay_s")} for t in sc["trials"]]}
    for name in MODELS:
        put(f"current/{name}", predict(camp_h, rows, name))
        put(f"retrained/{name}", predict(camp_h + train, rows, name))
    for c in candidates():
        al = candidate_alerts((camp_h if c["training_data"] == "campaign+live" else []) + train, rows, c)
        put(f"candidate/{cand_name(c)}", persist(al, c["persistence_rows"]))
    for name, al in frozen_rules(rows, amp).items():
        put(f"context/{name}", al)
    return {"slots": [{k: s[k] for k in ("slot", "region", "loss_percent", "effect_confirmed")} | {"start_utc": iso(s["request_unix"])}
                      for s in slots], "results": res}


def _commit(md: str, msg: str) -> None:
    """Copy the summary into docs/ml/live/ and commit + push it (aggregate numbers only)."""
    repo = Path(__file__).resolve().parents[2]
    out = repo / "docs" / "ml" / "live" / "WEDNESDAY_RESULTS.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    for cmd in (["git", "add", str(out)], ["git", "commit", "-q", "-m", msg], ["git", "push", "-q"]):
        try:   # a slow or failed push must never stop the scoring; the commit stays local
            subprocess.run(cmd, cwd=repo, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            pass


def early_warning(rep: dict) -> dict | None:
    """Amendment 8 comparison on the Tuesday trials and held-out healthy time."""
    tt = (rep.get("tuesday_trials") or {}).get("results")
    if not tt:
        return None
    a8 = AMENDMENT_8
    pk, rl = tt.get("candidate/" + a8["pick"]), tt.get("context/" + a8["comparator"])
    if not pk or not rl:
        return {"error": "pick or comparator missing from trial results"}
    P = {t["slot"]: t for t in pk["per_trial"]}
    R = {t["slot"]: t for t in rl["per_trial"]}
    shared = [s for s in P if P[s]["detected"] and R.get(s, {}).get("detected")]
    diffs = {s: P[s]["delay_s"] - R[s]["delay_s"] for s in shared}
    earlier = sum(d < 0 for d in diffs.values())
    pfa = (rep.get("candidates_held_out") or {}).get(a8["pick"], {})
    rfa = rep["results"].get("context/" + a8["comparator"], {}).get("held_out_live", {})
    only_pick = sorted(s for s in P if P[s]["detected"] and not R.get(s, {}).get("detected"))
    passed = (pk["trials_detected"] >= rl["trials_detected"]
              and (len(only_pick) >= 1 or earlier > len(shared) / 2)
              and pfa.get("false_alarms_per_day") is not None
              and pfa["false_alarms_per_day"] <= a8["max_held_out_fa_per_day"]
              and pk["controls_alarmed"] <= a8["max_tuesday_controls_alarmed"])
    return {"pick": a8["pick"], "comparator": a8["comparator"],
            "pick_detected": f"{pk['trials_detected']}/{pk['trials_evaluated']}",
            "rule_detected": f"{rl['trials_detected']}/{rl['trials_evaluated']}",
            "pick_controls_alarmed": f"{pk['controls_alarmed']}/{pk['controls_evaluated']}",
            "rule_controls_alarmed": f"{rl['controls_alarmed']}/{rl['controls_evaluated']}",
            "only_pick": only_pick,
            "only_rule": sorted(s for s in R if R[s]["detected"] and not P.get(s, {}).get("detected")),
            "shared": sorted(shared), "pick_minus_rule_s": diffs, "pick_earlier_on": earlier,
            "pick_held_out_fa": {k: pfa.get(k) for k in ("false_alarms_per_day", "false_alarms_per_day_ci95")},
            "rule_held_out_fa": {k: rfa.get(k) for k in ("false_alarms_per_day", "false_alarms_per_day_ci95")},
            "verdict": "passed" if passed else "did not pass"}


def _supervised_stage(C: Path, L: Path, rep: dict, rows: list[dict], amp: dict, wit) -> dict | None:
    """Amendment 9: frozen S1/S2 (docs/ml/live/supervised-picks.json) on held-out and Tuesday trials."""
    from . import supervised_live as sl
    f = Path(__file__).resolve().parents[2] / "docs" / "ml" / "live" / "supervised-picks.json"
    if not f.is_file():
        return None
    spec = json.loads(f.read_text(encoding="utf-8"))["frozen"]
    held_lo = ts(LIVE_PROTOCOL["held_out_start_utc"])
    held = [r for r in rows if r["aws"] and r["ts"] >= held_lo]
    trows, tslots = trial_rows_slots(live_rows(amp, load_index(_ripe_spec(L)), [], witness=wit), L)
    return sl.wednesday(C, L, rep, held, trows, tslots, spec)


def team_summary(rep: dict) -> str:
    """Plain-language summary built only from the numbers in the report."""
    r = rep["results"]
    cur, ret = r[f"current/{PRIMARY}"], r[f"retrained/{PRIMARY}"]
    f = lambda v: f"{v['held_out_live']['false_alarms_per_day']}/day (95% CI {v['held_out_live']['false_alarms_per_day_ci95'][0]}-{v['held_out_live']['false_alarms_per_day_ci95'][1]})"
    lines = ["", "## Team summary (generated from the numbers above; shadow only)", "",
             f"* Held-out: {rep['held_out_span_utc'][0]} to {rep['held_out_span_utc'][1]}, "
             f"{cur['held_out_live']['healthy_hours']} healthy hours after exclusions, never used for training or choosing.",
             f"* Current model (13 Nov anomaly, AWS+RIPE, no host): {f(cur)} false alarms; "
             f"{cur['campaign_trials']['trials_detected']}/20 campaign trials.",
             f"* Planned retrain (+ live healthy data): {f(ret)} false alarms; "
             f"{ret['campaign_trials']['trials_detected']}/20 campaign trials.",
             f"* Frozen RIPE rule (unchanged, context): {r['context/frozen_aws_ripe_rule']['held_out_live']['false_alarms_per_day']}/day."]
    pick = rep.get("frozen_pick")
    ch = rep.get("candidates_held_out") or {}
    if pick and pick in ch:
        v = ch[pick]
        lines.append(f"* Candidate picked on Monday before held-out ({pick}): {v['false_alarms_per_day']}/day "
                     f"(95% CI {v['false_alarms_per_day_ci95'][0]}-{v['false_alarms_per_day_ci95'][1]}); "
                     f"all {len(ch)} candidates are in retrain-report.json.")
    elif not pick:
        lines.append("* No candidate pick was frozen before held-out, so no candidate is claimed.")
    if pick:
        lines.append("  (Monday pick failed review before any held-out scoring: trained on live data only it watches "
                     "one column, RIPE result age, and alarmed in 20/20 campaign controls. Reported, not claimed.)")
    pick2 = rep.get("frozen_pick_2")
    if pick2 and pick2 in ch:
        v = ch[pick2]
        lines.append(f"* Pick 2 (amendment 7, frozen before the Tuesday trials and held-out, {pick2}): "
                     f"{v['false_alarms_per_day']}/day (95% CI {v['false_alarms_per_day_ci95'][0]}-{v['false_alarms_per_day_ci95'][1]}).")
    tt = rep.get("tuesday_trials")
    if tt and "results" in tt:
        def t(k):
            v = tt["results"].get(k)
            return f"{v['trials_detected']}/{v['trials_evaluated']} (controls alarmed {v['controls_alarmed']}/{v['controls_evaluated']})" if v else "n/a"
        lines.append(f"* Tuesday trials (unseen faults): current {t('current/' + PRIMARY)}, retrained {t('retrained/' + PRIMARY)}, "
                     + (f"Monday pick {t('candidate/' + pick)}, " if pick else "")
                     + (f"pick 2 {t('candidate/' + pick2)}, " if pick2 else "") + f"frozen rule {t('context/frozen_aws_ripe_rule')}.")
    ew = rep.get("early_warning")
    if ew and "verdict" in ew:
        med = sorted(ew["pick_minus_rule_s"].values())
        lines.append(f"* Early-warning test (amendment 8, {ew['pick']} vs frozen loss_only rule): pick {ew['pick_detected']}, "
                     f"rule {ew['rule_detected']}; only pick {ew['only_pick']}, only rule {ew['only_rule']}; pick earlier on "
                     f"{ew['pick_earlier_on']}/{len(ew['shared'])} shared trials"
                     + (f" (median pick minus rule {med[len(med) // 2]:.0f} s)" if med else "")
                     + f"; held-out FA pick {ew['pick_held_out_fa']['false_alarms_per_day']}/day, rule "
                     f"{ew['rule_held_out_fa']['false_alarms_per_day']}/day; Tuesday controls alarmed pick "
                     f"{ew['pick_controls_alarmed']}, rule {ew['rule_controls_alarmed']}. Verdict: {ew['verdict']}. "
                     "Early warning only: an alert asks for a check and never moves traffic.")
    sv = rep.get("supervised")
    if sv:
        for name in ("S1", "S2"):
            e = sv.get(name) or {}
            fa, tu = e.get("held_out_fa") or {}, e.get("tuesday") or {}
            lines.append(f"* Supervised {name} (amendment 9, {e.get('spec')}): Tuesday "
                         f"{tu.get('trials_detected')}/{tu.get('trials_evaluated')} (controls {tu.get('controls_alarmed')}/"
                         f"{tu.get('controls_evaluated')}); only model {e.get('only_model')}, only rule {e.get('only_rule')}; "
                         f"earlier on {e.get('earlier_on')}/{e.get('shared')} shared; held-out {fa.get('false_alarms_per_day')}/day "
                         f"(95% CI {(fa.get('false_alarms_per_day_ci95') or ['?', '?'])[0]}-{(fa.get('false_alarms_per_day_ci95') or ['?', '?'])[1]}, "
                         f"{fa.get('healthy_hours')} h). Verdict: {e.get('verdict')}.")
        lab = (sv.get("lab_http_rule") or {})
        lf, lt = lab.get("held_out_fa_on_http_rows") or {}, lab.get("tuesday") or {}
        lines.append(f"* Lab HTTP rule (context): Tuesday {lt.get('trials_detected')}/{lt.get('trials_evaluated')} "
                     f"(controls {lt.get('controls_alarmed')}/{lt.get('controls_evaluated')}); held-out "
                     f"{lf.get('false_alarms_per_day')}/day on {sv.get('held_out_rows_with_http')} rows with HTTP features "
                     f"({sv.get('held_out_rows_without_http')} without).")
    lines.append("* The ML stays shadow-only; it never triggers an action.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- I/O
def ssm_exclusions(start: float, end: float, profile: str) -> list[dict]:
    """READ-ONLY: every SSM command sent to either origin during the run becomes an exclusion."""
    out = []
    for region, aws_region in (("region-a", "ap-southeast-1"), ("region-b", "us-east-1")):
        res = subprocess.run(["aws", "ssm", "list-commands", "--region", aws_region, "--profile", profile,
                              "--output", "json", "--no-cli-pager"], capture_output=True, text=True, timeout=120)
        if res.returncode:
            raise RuntimeError(f"ssm list-commands failed in {aws_region}")
        for c in json.loads(res.stdout).get("Commands", []):
            t0 = datetime.fromisoformat(str(c["RequestedDateTime"])).timestamp()
            if start - 3600 <= t0 <= end:
                timeout = float(c.get("TimeoutSeconds") or 3600)
                out.append({"region": region, "start_utc": iso(t0), "end_utc": iso(t0 + timeout),
                            "reason": f"SSM {c.get('DocumentName')} {c.get('CommandId')} ({c.get('Status')})"})
    return out


def _ripe_spec(live_dir: Path) -> dict[str, list[Path]]:
    return {r: [live_dir / f"ripe-{r}.json"] for r in ("region-a", "region-b") if (live_dir / f"ripe-{r}.json").is_file()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["snapshot", "retrain", "ssm-exclusions", "search"])
    ap.add_argument("--campaign-dir", type=Path, required=True)
    ap.add_argument("--live-dir", type=Path, required=True)
    ap.add_argument("--profile", default="adaptivenet-ml")
    a = ap.parse_args(argv)
    C, L = a.campaign_dir, a.live_dir
    meta = json.loads((L / "live.json").read_text(encoding="utf-8"))
    if a.command == "ssm-exclusions":
        ex = ssm_exclusions(ts(meta["start_utc"]), ts(LIVE_PROTOCOL["live_stop_utc"]), a.profile)
        (L / "exclusions-ssm.json").write_text(json.dumps(ex, indent=2), encoding="utf-8")
        print(f"{len(ex)} SSM commands during the run -> {L / 'exclusions-ssm.json'}")
        return 0
    camp, slots = campaign_rows(C, C / "campaign-plan.json",
                                {"region-a": [C / "ripe/236862553.json"], "region-b": [C / "ripe/236862555.json"]})
    exclusions = []
    for f in ("exclusions-ssm.json", "exclusions-operator.json"):
        if (L / f).is_file():
            exclusions += json.loads((L / f).read_text(encoding="utf-8"))
    amp = {r: read_jsonl(L / f"amp-{r}.jsonl") for r in ("region-a", "region-b")}
    wit = witness_times(L / "http_witness.jsonl")
    rows = live_rows(amp, load_index(_ripe_spec(L)), exclusions, witness=wit)
    if a.command == "snapshot":
        status = (L / "aws_status.txt").read_text().strip() if (L / "aws_status.txt").is_file() else "unknown"
        aws_last = max((r["ts"] for v in amp.values() for r in v), default=None)
        ripe_last = max((r["ts"] for r in rows), default=None)
        rep = snapshot(camp, rows, amp, {"live_start_utc": meta["start_utc"], "aws_status": status,
                                         "aws_through_utc": iso(aws_last) if aws_last else None,
                                         "ripe_through_utc": iso(ripe_last) if ripe_last else None,
                                         "exclusions": len(exclusions)})
        out = L / "snapshots"
        out.mkdir(exist_ok=True)
        stamp = datetime.now(SGT).strftime("%Y-%m-%d")
        (out / f"snapshot-{stamp}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        md = snapshot_markdown(rep)
        (L / "SNAPSHOT.md").write_text(md, encoding="utf-8")
        print(md)
        return 0
    if a.command == "search":
        rep = search(camp, slots, rows)
        (L / "search-report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        (L / "SEARCH.md").write_text(search_markdown(rep), encoding="utf-8")
        print(f"pick: {rep['pick']} ({rep['qualifying']} qualifying of {len(rep['candidates'])})")
        return 0
    if not (L / "exclusions-ssm.json").is_file():
        raise SystemExit("run ssm-exclusions first (read-only) so the review is fixed before scoring")
    # Stages are saved, summarised and committed one by one, so a Mac that closes early still
    # leaves the finished stages pushed (Wednesday 07:00-08:00 SGT window).
    docs = Path(__file__).resolve().parents[2] / "docs" / "ml" / "live"
    readpick = lambda f: json.loads((docs / f).read_text(encoding="utf-8"))["pick"] if (docs / f).is_file() else None
    rep = retrain(camp, slots, rows, amp, exclusions)
    rep["frozen_pick"] = readpick("candidate-pick.json")        # Monday pick, reported as is (failed, amendment 7)
    rep["frozen_pick_2"] = readpick("candidate-pick-2.json")    # amendment 7 pick
    stages = [("main comparison", None),
              ("every candidate on held-out", lambda: rep.__setitem__("candidates_held_out", heldout_candidates(camp, rows))),
              ("Tuesday trials", lambda: rep.__setitem__("tuesday_trials", trial_detection(
                  camp, live_rows(amp, load_index(_ripe_spec(L)), [], witness=wit), amp, L))),
              ("early-warning test", lambda: rep.__setitem__("early_warning", early_warning(rep))),
              ("supervised S1/S2 test", lambda: rep.__setitem__("supervised", _supervised_stage(C, L, rep, rows, amp, wit)))]
    for label, fn in stages:
        if fn:
            fn()
        (L / "retrain-report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        md = retrain_markdown(rep) + team_summary(rep)
        (L / "RETRAIN.md").write_text(md, encoding="utf-8")
        _commit(md, f"ml: Wednesday live evaluation - {label} (automatic)")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
