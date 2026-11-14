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
    return sorted(ts(r["timestamp"]) for r in read_jsonl(path) if r.get("timestamp"))


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
            if not in_exclusion(region, t, exclusions) and witness_ok(witness, t):
                a = by_ts.get(t)
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
            out["frozen_aws_ripe_rule"][key] = bool(aws_view_alert(vals, True, region, hist)) or bool(ripe)
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
    ap.add_argument("command", choices=["snapshot", "retrain", "ssm-exclusions"])
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
    if not (L / "exclusions-ssm.json").is_file():
        raise SystemExit("run ssm-exclusions first (read-only) so the review is fixed before scoring")
    rep = retrain(camp, slots, rows, amp, exclusions)
    (L / "retrain-report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    md = retrain_markdown(rep)
    (L / "RETRAIN.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
