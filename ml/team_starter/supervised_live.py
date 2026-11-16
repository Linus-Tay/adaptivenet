"""Amendment 9: two supervised early-warning classifiers (SHADOW ONLY), tested on the Tuesday trials.

S1 = supervised RIPE; S2 = supervised HTTP (15 s witness) + RIPE loss. Both are trained on the 20
labelled 12 Nov campaign faults plus live healthy rows from BEFORE ``LIVE_END`` only, so no held-out
data is read when the picks are made. ``pick`` computes both picks by the fixed rules and writes the
frozen definitions; the Wednesday stage scores them, the lab HTTP rule (context) and the frozen
loss_only rule (comparator) on the Tuesday trials and the held-out healthy time.
Nothing here can trigger an action: an alert only asks for a confirming check.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import json
import statistics
from pathlib import Path

from . import campaign_ml as cm
from . import live_shadow as ls

LIVE_END = ls.ts("2026-11-16T11:29:00+00:00")
S2_NEG_END = ls.ts("2026-11-16T07:47:00+00:00")
S2_VAL = (ls.ts("2026-11-16T08:02:00+00:00"), LIVE_END)
RIPE_LOSS = ("ripe_window_loss_pct", "ripe_window_probes_with_loss", "ripe_loss_pct_reachable",
             "ripe_probes_unreachable")
RIPE_RTT = ("ripe_max_rtt_inflation_ms", "ripe_paths_inflated")
HTTP_ALL = ("w_ex150", "w_ex800", "w_exmax", "w_exmean", "o_ex150", "o_ex800", "d_ex150", "d_ex800")
COLS = {"S1": {"ripe_loss": RIPE_LOSS, "ripe_loss+rtt": RIPE_LOSS + RIPE_RTT},
        "S2": {"http800+ripe_loss": ("w_ex800", "d_ex800", "o_ex800") + RIPE_LOSS,
               "http": HTTP_ALL, "http+ripe_loss": HTTP_ALL + RIPE_LOSS}}
OTHER = {"region-a": "region-b", "region-b": "region-a"}
WINDOW, BASELINE, CAP = 120, 1800, 5000.0
PASS = {"max_held_out_fa_per_day": 5.0, "max_tuesday_controls_alarmed": 1,
        "comparator": "frozen_aws_ripe_rule_loss_only"}


# ------------------------------------------------------------------------- witness
class Witness:
    """Per-path (scope) sorted witness records."""

    def __init__(self, paths: list[Path], until: float | None = None):
        seen, by = set(), defaultdict(list)
        for p in paths:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    r = json.loads(line)
                    t = ls.ts(r["timestamp"])
                except (ValueError, KeyError):
                    continue
                if until is not None and t >= until:
                    continue
                k = (r.get("scope"), t)
                if k in seen:
                    continue
                seen.add(k)
                by[r.get("scope")].append((t, r.get("outcome") == "ok", float(r.get("latency_ms") or 0)))
        self.rec = {s: sorted(v) for s, v in by.items()}
        self.ts = {s: [x[0] for x in v] for s, v in self.rec.items()}

    def window(self, scope: str, lo: float, hi: float) -> list[tuple]:
        t = self.ts.get(scope, [])
        return self.rec[scope][bisect_right(t, lo):bisect_right(t, hi)] if t else []


def _path(w: Witness, scope: str, t: float) -> dict | None:
    win = w.window(scope, t - WINDOW, t)
    base = [lat for _, ok, lat in w.window(scope, t - WINDOW - BASELINE, t - WINDOW) if ok]
    if len(win) < 4 or len(base) < 20:
        return None
    b = statistics.median(base)
    ex = [min(CAP, (lat if ok else CAP) - b) for _, ok, lat in win]   # a failed request counts as 5000 ms
    return {"ex150": sum(e > 150 for e in ex) / len(ex), "ex800": sum(e > 800 for e in ex) / len(ex),
            "exmax": max(ex), "exmean": sum(ex) / len(ex)}


def http_features(w: Witness, region: str, t: float) -> dict | None:
    own, oth = _path(w, region, t), _path(w, OTHER[region], t)
    if own is None or oth is None:
        return None
    f = {f"w_{k}": v for k, v in own.items()} | {f"o_{k}": v for k, v in oth.items()}
    f["d_ex150"], f["d_ex800"] = own["ex150"] - oth["ex150"], own["ex800"] - oth["ex800"]
    return f


def lab_http_rule(w: Witness, region: str, t: float) -> bool:
    """Unchanged lab HTTP rule on the own path: 60 s window, failures >= 20% or p95 >= 800 ms."""
    win = w.window(region, t - 60, t)
    if not win:
        return False
    fails = sum(not ok for _, ok, _ in win) / len(win)
    lat = sorted(lat for _, ok, lat in win if ok)
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None
    return fails >= 0.20 or (p95 is not None and p95 >= 800)


def with_http(rows: list[dict], w: Witness) -> list[dict]:
    """Copies of rows that have HTTP features (others cannot alert under S2)."""
    out = []
    for r in rows:
        f = http_features(w, r["region"], r["ts"])
        if f is not None:
            out.append(r | {"values": r["values"] | f})
    return out


# -------------------------------------------------------------------------- models
def _X(rows, cols):
    import numpy as np
    return np.array([[float(r["values"].get(c) or 0.0) for c in cols] for r in rows], dtype=float)  # missing -> 0


def fit(train: list[dict], cols: tuple, model: str):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    y = [r["label"] == "impaired" for r in train]
    if model == "logreg":
        m = Pipeline([("s", StandardScaler()), ("c", LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000))])
    elif model == "gboost":
        m = GradientBoostingClassifier(n_estimators=100, max_depth=2, learning_rate=0.1, random_state=42)
    else:
        raise ValueError(model)
    return m.fit(_X(train, cols), y)


def alerts_for(m, rows: list[dict], cols: tuple, thr: float, k: int) -> dict:
    if not rows:
        return {}
    j = list(m.classes_).index(True)
    p = m.predict_proba(_X(rows, cols))[:, j]
    return ls.persist({(r["region"], r["ts"]): bool(x >= thr) for r, x in zip(rows, p)}, k)


def campaign_blocked(camp, slots, extra_neg, cols, model, thr, k) -> dict:
    start, end = min(r["ts"] for r in camp), max(r["ts"] for r in camp)
    alerts = {}
    for lo, hi in cm.folds_from_slots(slots, start, end):
        test = [r for r in camp if lo <= r["ts"] < hi]
        m = fit(cm._train_rows(camp, lo, hi, cm.PROTOCOL["embargo_seconds"]) + extra_neg, cols, model)
        alerts |= alerts_for(m, test, cols, thr, k)
    return cm.score(camp, alerts, slots)


def grid(name: str) -> list[dict]:
    g = []
    for model in ("logreg", "gboost"):
        for cset in COLS[name]:
            for training in (("campaign", "campaign+live") if name == "S1" else ("campaign+live",)):
                for thr in (0.5, 0.8, 0.9):
                    for k in ((1, 2) if name == "S1" else (1, 2, 3)):
                        g.append({"family": name, "model": model, "columns": cset, "training": training,
                                  "threshold": thr, "persistence": k})
    return g


def cname(c: dict) -> str:
    return f"{c['family']}|{c['model']}|{c['columns']}|{c['training']}|p{c['threshold']}|k{c['persistence']}"


def _order(c):
    return (-c["trials"], c["validation"]["false_alarm_onsets"], c["campaign_day_fa"] or 0,
            len(COLS[c["family"]][c["columns"]]), c["model"] != "logreg", c["persistence"], -c["threshold"])


def evaluate(name, camp, slots, neg, val) -> list[dict]:
    out, cache = [], {}
    for c in grid(name):
        cols = COLS[name][c["columns"]]
        extra = neg if c["training"] == "campaign+live" else []
        key = (c["model"], c["columns"], c["training"])
        if key not in cache:
            cache[key] = fit(camp + extra, cols, c["model"])
        v = ls.fa_summary(val, alerts_for(cache[key], val, cols, c["threshold"], c["persistence"]))
        d = campaign_blocked(camp, slots, extra, cols, c["model"], c["threshold"], c["persistence"])
        out.append(c | {"name": cname(c), "trials": d["trials_detected"], "controls": d["controls_alarmed"],
                        "campaign_day_fa": d["false_alarms_per_day"], "median_delay_s": d["median_delay_s"],
                        "missed": [t["slot"] for t in d["trials"] if not t["detected"]], "validation": v})
    return out


def pick(cands: list[dict], name: str) -> dict | None:
    if name == "S1":
        ok = [c for c in cands if c["controls"] <= 1 and (c["validation"]["false_alarms_per_day"] or 0) <= 5.0]
    else:
        ok = [c for c in cands if c["controls"] <= 1 and c["validation"]["false_alarm_onsets"] <= 1]
    return sorted(ok, key=_order)[0] if ok else None


# ------------------------------------------------------------------------- loading
def load(C: Path, L: Path, until: float | None):
    camp, slots = ls.campaign_rows(C, C / "campaign-plan.json",
                                   {"region-a": [C / "ripe/236862553.json"], "region-b": [C / "ripe/236862555.json"]})
    amp = {r: ls.read_jsonl(L / f"amp-{r}.jsonl") for r in ("region-a", "region-b")}
    if until is not None:
        amp = {r: [x for x in v if x["ts"] < until] for r, v in amp.items()}
    live = ls.live_rows(amp, ls.load_index(ls._ripe_spec(L)), [], until=until,
                        witness=ls.witness_times(L / "http_witness.jsonl"))
    live = [r for r in live if r["aws"] and (until is None or r["ts"] < until)]
    wc = Witness([C / "witness.stdout", C / "http_witness.jsonl"])
    wl = Witness([L / "http_witness.jsonl", L / "witness.stdout"], until=until)
    return camp, slots, amp, live, wc, wl


def run_pick(C: Path, L: Path) -> dict:
    camp, slots, amp, live, wc, wl = load(C, L, LIVE_END)
    fit_rows = [r for r in live if r["ts"] < ls.ts(ls.CANDIDATE_SEARCH["fit_window_utc"][1])]
    val = [r for r in live if ls.ts(ls.CANDIDATE_SEARCH["validation_window_utc"][0]) <= r["ts"]
           < ls.ts(ls.CANDIDATE_SEARCH["validation_window_utc"][1])]
    s1 = evaluate("S1", camp, slots, fit_rows, val)
    camp_h = with_http(camp, wc)
    live_h = with_http(live, wl)
    neg2 = [r for r in live_h if r["ts"] < S2_NEG_END]
    val2 = [r for r in live_h if S2_VAL[0] <= r["ts"] < S2_VAL[1]]
    s2 = evaluate("S2", camp_h, slots, neg2, val2)
    p1, p2 = pick(s1, "S1"), pick(s2, "S2")
    refit = None
    if p2:
        cols = COLS["S2"][p2["columns"]]
        d = campaign_blocked(camp_h, slots, live_h, cols, p2["model"], p2["threshold"], p2["persistence"])
        refit = {"trials": d["trials_detected"], "controls": d["controls_alarmed"],
                 "campaign_day_fa": d["false_alarms_per_day"], "median_delay_s": d["median_delay_s"]}
    lab = {(r["region"], r["ts"]): lab_http_rule(wc, r["region"], r["ts"]) for r in camp_h}
    labd = cm.score(camp_h, lab, slots)
    labv = ls.fa_summary(val2, {(r["region"], r["ts"]): lab_http_rule(wl, r["region"], r["ts"]) for r in val2})
    return {"rows": {"campaign": len(camp), "campaign_impaired": sum(r["label"] == "impaired" for r in camp),
                     "live_before_live_end": len(live), "fit": len(fit_rows), "validation": len(val),
                     "campaign_http": len(camp_h), "live_http": len(live_h), "s2_negatives": len(neg2),
                     "s2_validation": len(val2)},
            "S1": s1, "S2": s2, "S1_pick": p1, "S2_pick": p2, "S2_pick_refit_all_live": refit,
            "lab_http_campaign": {"trials": labd["trials_detected"], "controls": labd["controls_alarmed"],
                                  "campaign_day_fa": labd["false_alarms_per_day"], "median_delay_s": labd["median_delay_s"]},
            "lab_http_validation": labv}


# ----------------------------------------------------------------------- Wednesday
def frozen_models(C: Path, L: Path, spec: dict):
    """Refit the frozen S1/S2 definitions on data before LIVE_END only."""
    camp, slots, amp, live, wc, wl = load(C, L, LIVE_END)
    out = {}
    s1 = spec["S1"]
    fit_rows = [r for r in live if r["ts"] < ls.ts(ls.CANDIDATE_SEARCH["fit_window_utc"][1])]
    out["S1"] = (fit(camp + (fit_rows if s1["training"] == "campaign+live" else []), COLS["S1"][s1["columns"]],
                     s1["model"]), s1)
    s2 = spec["S2"]
    out["S2"] = (fit(with_http(camp, wc) + with_http(live, wl), COLS["S2"][s2["columns"]], s2["model"]), s2)
    return out


def wednesday(C: Path, L: Path, rep: dict, held: list[dict], trial_rows: list[dict] | None,
              trial_slots: list[dict] | None, spec: dict) -> dict:
    models = frozen_models(C, L, spec)
    w = Witness([L / "http_witness.jsonl", L / "witness.stdout"])
    held_h = with_http(held, w)
    trial_h = with_http(trial_rows, w) if trial_rows else []
    res = {"held_out_rows": len(held), "held_out_rows_with_http": len(held_h),
           "held_out_rows_without_http": len(held) - len(held_h)}
    tt = (rep.get("tuesday_trials") or {}).get("results") or {}
    comp = tt.get("context/" + PASS["comparator"])
    comp_fa = rep["results"].get("context/" + PASS["comparator"], {}).get("held_out_live", {})

    def trial_score(alerts, rows):
        sc = cm.score(rows, alerts, trial_slots)
        return {k: sc[k] for k in ("trials_evaluated", "trials_detected", "controls_evaluated", "controls_alarmed",
                                   "median_delay_s")} | {"per_trial": {t["slot"]: t for t in sc["trials"]}}

    out = {}
    lab_held = {(r["region"], r["ts"]): lab_http_rule(w, r["region"], r["ts"]) for r in held_h}
    lab_fa = ls.fa_summary(held_h, lab_held)
    for name, (m, s) in models.items():
        cols = COLS[name][s["columns"]]
        hrows = held if name == "S1" else held_h
        fa = ls.fa_summary(hrows, alerts_for(m, hrows, cols, s["threshold"], s["persistence"]))
        e = {"spec": cname(s), "held_out_fa": fa}
        if trial_rows and trial_slots:
            trows = trial_rows if name == "S1" else trial_h
            ts_ = trial_score(alerts_for(m, trows, cols, s["threshold"], s["persistence"]), trows)
            e["tuesday"] = {k: v for k, v in ts_.items() if k != "per_trial"}
            if comp:
                P, R = ts_["per_trial"], {t["slot"]: t for t in comp["per_trial"]}
                shared = [x for x in P if P[x]["detected"] and R.get(x, {}).get("detected")]
                diffs = {x: P[x]["delay_s"] - R[x]["delay_s"] for x in shared}
                only_m = sorted(x for x in P if P[x]["detected"] and not R.get(x, {}).get("detected"))
                e |= {"only_model": only_m,
                      "only_rule": sorted(x for x in R if R[x]["detected"] and not P.get(x, {}).get("detected")),
                      "model_minus_rule_s": diffs, "earlier_on": sum(d < 0 for d in diffs.values()),
                      "shared": len(shared)}
                passed = (ts_["trials_detected"] >= comp["trials_detected"]
                          and (len(only_m) >= 1 or e["earlier_on"] > len(shared) / 2)
                          and fa["false_alarms_per_day"] is not None
                          and fa["false_alarms_per_day"] <= PASS["max_held_out_fa_per_day"]
                          and ts_["controls_alarmed"] <= PASS["max_tuesday_controls_alarmed"])
                if name == "S2":
                    passed = passed and fa["false_alarm_onsets"] < lab_fa["false_alarm_onsets"]
                e["verdict"] = "passed" if passed else "did not pass"
        out[name] = e
    lab = {"held_out_fa_on_http_rows": lab_fa}
    if trial_rows and trial_slots:
        lt = trial_score({(r["region"], r["ts"]): lab_http_rule(w, r["region"], r["ts"]) for r in trial_h}, trial_h)
        lab["tuesday"] = {k: v for k, v in lt.items() if k != "per_trial"}
    out["lab_http_rule"] = lab
    out["comparator"] = {"name": PASS["comparator"], "held_out_fa": comp_fa,
                         "tuesday": {k: comp[k] for k in ("trials_detected", "trials_evaluated", "controls_alarmed",
                                                          "controls_evaluated", "median_delay_s")} if comp else None}
    return res | out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--campaign-dir", type=Path, required=True)
    ap.add_argument("--live-dir", type=Path, required=True)
    a = ap.parse_args(argv)
    rep = run_pick(a.campaign_dir, a.live_dir)
    (a.live_dir / "supervised-pick-report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print("rows", rep["rows"])
    for fam in ("S1", "S2"):
        ok = [c for c in rep[fam] if c["controls"] <= 1]
        for c in sorted(ok, key=_order)[:6]:
            v = c["validation"]
            print(f"  {c['name']:52s} trials {c['trials']}/20 controls {c['controls']}/20 campday {c['campaign_day_fa']} "
                  f"delay {c['median_delay_s']} | val {v['false_alarm_onsets']} in {v['healthy_hours']} h = "
                  f"{v['false_alarms_per_day']} {v['false_alarms_per_day_ci95']} missed {c['missed']}")
        print(f"  {fam} PICK:", rep[f"{fam}_pick"]["name"] if rep[f"{fam}_pick"] else None)
    print("S2 refit:", rep["S2_pick_refit_all_live"])
    print("lab http campaign:", rep["lab_http_campaign"], "validation:", rep["lab_http_validation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
