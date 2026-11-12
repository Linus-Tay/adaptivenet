"""Does RIPE Atlas add detection value beyond AWS-only monitoring? (offline, shadow-only)

Why the existing three arms tie
-------------------------------
* Every arm abstains on the SAME rows: `ready_for_shadow_assessment` requires the
  AWS internal source to be fresh in every arm, and the external arms can only add
  further conditions. External data can never rescue an AWS-side abstention.
* The snapshot RIPE columns are regional aggregates (`packet_loss_pct`,
  `network_rtt_p50/p95_ms`). A probe that can never reach an origin (real case:
  probe 1010367 -> Singapore, 100% loss for the whole campaign) pins aggregate loss
  at ~25% permanently, and one degraded path among four barely moves a p50.
* RIPE rounds are 240-900 s apart, so a few-minute episode contains zero or one
  new RIPE result; the column is mostly the pre-onset value.

What this module adds (no AWS calls, no writes, no labels invented)
-------------------------------------------------------------------
1. `fetch` public RIPE Atlas results (read-only, no API key) into ignored `data/`.
2. Per-probe, past-only *path* features: RTT inflation versus each probe's own
   trailing baseline, count of inflated paths, loss among reachable probes only,
   persistently unreachable probes reported separately (never as path loss),
   and data age. Missing stays `None`; never zero-filled.
3. A candidate external-path event finder. Candidates are NOT labels: they need an
   independent reviewer before they can enter `verified_episodes.json`.
4. A paired, episode-level comparison (exact McNemar test, episode bootstrap CI,
   paired detection delay) between AWS-only and AWS+RIPE-path detectors, joined to
   real archived AWS feature snapshots when they are supplied.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
import statistics
import urllib.request

from .feature_views import AWS_COLUMNS, REGIONS, project

API = "https://atlas.ripe.net/api/v2/measurements/{msm}/results/?start={start}&stop={stop}&format=json"
CHUNK_SECONDS = 6 * 86400  # the public API refuses >7 days per request
REPORT_VERSION = "adaptivenet.ripe_value_study.v1"

# Detection thresholds are fixed before looking at any AWS data or labels.
BASELINE_SECONDS = 6 * 3600
MIN_BASELINE_SAMPLES = 30
INFLATION_MS = 50.0
INFLATION_RATIO = 1.3
MIN_SUSTAINED_ROUNDS = 2
UNREACHABLE_ROUNDS = 12
MAX_AGE_SECONDS = 2 * 900

PATH_COLUMNS = ("ripe_max_rtt_inflation_ms", "ripe_paths_inflated", "ripe_reachable_probes",
                "ripe_loss_pct_reachable", "ripe_probes_unreachable", "ripe_result_age_s")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- data
def normalize(raw: list[dict], region: str) -> list[dict]:
    """Raw RIPE ping results -> minimal samples. RTT is None when nothing came back."""
    out = []
    for r in raw:
        if not isinstance(r, dict) or "prb_id" not in r or "timestamp" not in r:
            continue
        sent, rcvd = r.get("sent"), r.get("rcvd")
        if not isinstance(sent, int) or not isinstance(rcvd, int) or sent <= 0:
            continue
        avg = r.get("avg")
        rtt = float(avg) if isinstance(avg, (int, float)) and avg > 0 and rcvd > 0 else None
        out.append({"region": region, "probe": int(r["prb_id"]), "ts": float(r["timestamp"]),
                    "sent": sent, "rcvd": rcvd, "rtt": rtt, "msm": r.get("msm_id")})
    out.sort(key=lambda s: (s["ts"], s["probe"]))
    return out


def load_samples(spec: dict[str, list[Path]]) -> list[dict]:
    samples = []
    for region, paths in spec.items():
        if region not in REGIONS:
            raise ValueError(f"unknown region {region}")
        for path in paths:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError(f"not a RIPE results list: {path}")
            samples.extend(normalize(data, region))
    samples.sort(key=lambda s: (s["ts"], s["region"], s["probe"]))
    return samples


def fetch(msm: int, start: int, stop: int, out: Path) -> int:
    """Read-only download of PUBLIC measurement results; no credentials used."""
    rows: list[dict] = []
    t = start
    while t < stop:
        end = min(stop, t + CHUNK_SECONDS)
        with urllib.request.urlopen(API.format(msm=msm, start=t, stop=end), timeout=120) as resp:
            chunk = json.loads(resp.read().decode("utf-8"))
        if not isinstance(chunk, list):
            raise RuntimeError(f"RIPE API error for {msm}: {chunk}")
        rows.extend(chunk)
        t = end
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows), encoding="utf-8")
    return len(rows)


# ------------------------------------------------------------------ path features
class ProbeIndex:
    """Past-only access to each (region, probe) series."""

    def __init__(self, samples: list[dict]):
        self.series: dict[tuple[str, int], list[dict]] = defaultdict(list)
        for s in samples:
            self.series[(s["region"], s["probe"])].append(s)
        self.times = {k: [s["ts"] for s in v] for k, v in self.series.items()}

    def upto(self, key, at: float) -> list[dict]:
        return self.series[key][:bisect_right(self.times[key], at)]

    def probes(self, region: str) -> list[int]:
        return sorted(p for r, p in self.series if r == region)


def _baseline(history: list[dict], before: float) -> float | None:
    """Trailing median RTT strictly before the current result (no peeking)."""
    vals = [s["rtt"] for s in history if before - BASELINE_SECONDS <= s["ts"] < before and s["rtt"] is not None]
    return statistics.median(vals) if len(vals) >= MIN_BASELINE_SAMPLES else None


def _inflated(rtt: float | None, base: float | None) -> bool:
    return rtt is not None and base is not None and rtt - base >= INFLATION_MS and rtt / base >= INFLATION_RATIO


def path_features(index: ProbeIndex, region: str, at: float) -> dict:
    """As-of features at `at` from results with timestamp <= at only."""
    latest_ts, inflations, inflated, reachable, unreachable = None, [], 0, 0, 0
    sent = lost = 0
    for probe in index.probes(region):
        hist = index.upto((region, probe), at)
        if not hist or at - hist[-1]["ts"] > MAX_AGE_SECONDS:
            continue  # stale or absent: contributes nothing (stays missing)
        cur = hist[-1]
        recent = hist[-UNREACHABLE_ROUNDS:]
        if len(recent) == UNREACHABLE_ROUNDS and all(s["rcvd"] == 0 for s in recent):
            unreachable += 1  # a dead vantage is not evidence of path degradation
            continue
        reachable += 1
        latest_ts = cur["ts"] if latest_ts is None else max(latest_ts, cur["ts"])
        sent += cur["sent"]
        lost += cur["sent"] - cur["rcvd"]
        base = _baseline(hist[:-1], cur["ts"])
        if cur["rtt"] is not None and base is not None:
            inflations.append(cur["rtt"] - base)
            inflated += _inflated(cur["rtt"], base)
    if reachable == 0:
        return {c: None for c in PATH_COLUMNS} | {"ripe_probes_unreachable": unreachable or None}
    return {"ripe_max_rtt_inflation_ms": max(inflations) if inflations else None,
            "ripe_paths_inflated": inflated if inflations else None,
            "ripe_reachable_probes": reachable,
            "ripe_loss_pct_reachable": 100.0 * lost / sent if sent else None,
            "ripe_probes_unreachable": unreachable,
            "ripe_result_age_s": at - latest_ts}


def ripe_path_alert(feat: dict) -> bool | None:
    """Fixed, pre-declared external rule. None = no usable external evidence."""
    if feat.get("ripe_reachable_probes") in (None, 0):
        return None
    loss = feat.get("ripe_loss_pct_reachable")
    return bool((feat.get("ripe_paths_inflated") or 0) >= 1 or (loss is not None and loss >= 50))


# -------------------------------------------------------------- candidate events
def candidate_events(samples: list[dict]) -> list[dict]:
    """Sustained per-path RTT inflation or loss. Candidates only, never labels.

    `path_specific` = the same probe's path to the OTHER region stayed normal, so the
    probe's own access network is unlikely to be the cause.
    """
    index = ProbeIndex(samples)
    events = []
    for (region, probe), series in index.series.items():
        run: list[dict] = []
        for i, cur in enumerate(series):
            base = _baseline(series[:i], cur["ts"])
            bad = _inflated(cur["rtt"], base) or (cur["rcvd"] == 0 and base is not None)
            if bad:
                run.append({**cur, "base": base})
                continue
            if len(run) >= MIN_SUSTAINED_ROUNDS:
                events.append(_event(index, region, probe, run))
            run = []
        if len(run) >= MIN_SUSTAINED_ROUNDS:
            events.append(_event(index, region, probe, run))
    events.sort(key=lambda e: (e["start_unix"], e["region"], e["probe"]))
    return events


def _event(index: ProbeIndex, region: str, probe: int, run: list[dict]) -> dict:
    start, end = run[0]["ts"], run[-1]["ts"]
    rtts = [s["rtt"] for s in run if s["rtt"] is not None]
    other = [r for r in REGIONS if r != region]
    control = None
    if other and (other[0], probe) in index.series:
        series = index.series[(other[0], probe)]
        during = [s for s in series if start <= s["ts"] <= end]
        flagged = sum(_inflated(s["rtt"], _baseline([x for x in series if x["ts"] < s["ts"]], s["ts"]))
                      or s["rcvd"] == 0 for s in during)
        control = {"region": other[0], "results": len(during), "flagged": flagged}
    peers = []
    for p in index.probes(region):
        if p == probe:
            continue
        series = index.series[(region, p)]
        during = [s for s in series if start <= s["ts"] <= end]
        if during and not all(s["rcvd"] == 0 for s in during):
            peers.append(sum(_inflated(s["rtt"], _baseline([x for x in series if x["ts"] < s["ts"]], s["ts"]))
                             for s in during) / len(during))
    return {"region": region, "probe": probe, "start_unix": start, "end_unix": end,
            "start_utc": iso(start), "end_utc": iso(end), "rounds": len(run),
            "duration_s": end - start, "baseline_rtt_ms": round(run[0]["base"], 1),
            "peak_rtt_ms": round(max(rtts), 1) if rtts else None,
            "lost_rounds": sum(s["rcvd"] == 0 for s in run),
            "other_region_control": control,
            "path_specific": bool(control and control["results"] and control["flagged"] == 0),
            "other_probes_same_region_inflated_fraction": peers,
            "status": "candidate_unreviewed"}


# ------------------------------------------------------------- AWS-only detectors
def aws_only_alert(slow: dict, region: str, history: list[dict]) -> bool | None:
    """Most sensitive reasonable AWS-only detector, to be FAIR to AWS-only.

    Alerts on app down, any 5xx ratio >= 5%, existing robust-z >= 3, or p95 latency
    >= 1.5x its own trailing median. None when internal data is not usable.
    """
    view = project(slow, "aws_only")
    v = view["values"]
    if not view["ready_for_shadow_assessment"] or v.get(f"{region}.app_up") is None:
        return None
    if v[f"{region}.app_up"] == 0:
        return True
    err, z, p95 = v.get(f"{region}.http_error_ratio"), v.get(f"{region}.http_p95_robust_z"), v.get(f"{region}.http_p95_ms")
    if err is not None and err >= 0.05:
        return True
    if z is not None and z >= 3:
        return True
    past = [h for h in history if h is not None]
    if p95 is not None and len(past) >= 10 and p95 >= 1.5 * statistics.median(past):
        return True
    return False


def _snap_time(s: dict) -> float:
    return float(s["computed_at_unix"])


def join_events_with_aws(events: list[dict], snapshots: list[dict], index: ProbeIndex,
                         pad_seconds: float = 0) -> list[dict]:
    """For each event, would AWS-only and AWS+RIPE-path have alerted inside it?"""
    snaps = sorted((s for s in snapshots if s.get("contract_version") == "adaptivenet.model_input.v1"),
                   key=_snap_time)
    times = [_snap_time(s) for s in snaps]
    out = []
    for ev in events:
        region = ev["region"]
        lo, hi = ev["start_unix"] - pad_seconds, ev["end_unix"] + pad_seconds
        i0, i1 = bisect_right(times, lo - 1e-9), bisect_right(times, hi)
        window = snaps[i0:i1]
        res = {**ev, "aws_snapshots": len(window)}
        if not window:
            res["evaluable"] = False
            out.append(res)
            continue
        first_aws = first_ext = None
        aws_votes = []
        for k, s in enumerate(window):
            t = times[i0 + k]
            # AWS latency baseline: pre-event snapshots only (no in-event or future data).
            j0 = bisect_right(times, t - BASELINE_SECONDS - 1e-9)
            prior = [(x.get("features", {}).get(region) or {}).get("http_p95_ms")
                     for x in snaps[j0:i0]]
            a = aws_only_alert(s, region, prior)
            aws_votes.append(a)
            if a and first_aws is None:
                first_aws = t
            ext = ripe_path_alert(path_features(index, region, t))
            if (a or ext) and first_ext is None:
                first_ext = t
        res.update({"evaluable": any(a is not None for a in aws_votes),
                    "aws_only_detected": first_aws is not None,
                    "aws_ripe_detected": first_ext is not None,
                    "aws_only_delay_s": first_aws - ev["start_unix"] if first_aws is not None else None,
                    "aws_ripe_delay_s": first_ext - ev["start_unix"] if first_ext is not None else None,
                    "aws_abstained_snapshots": sum(a is None for a in aws_votes)})
        out.append(res)
    return out


# ---------------------------------------------------------------- paired stats
def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on discordant pairs b (A only) and c (B only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def bootstrap_ci(values: list[float], *, reps: int = 10000, seed: int = 3104, alpha: float = .05) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return [means[int(alpha / 2 * reps)], means[int((1 - alpha / 2) * reps) - 1]]


def paired_summary(episodes: list[dict], a: str = "aws_only", b: str = "aws_ripe") -> dict:
    """Episode-level paired comparison; each episode is one independent unit."""
    ev = [e for e in episodes if e.get("evaluable")]
    da = [bool(e[f"{a}_detected"]) for e in ev]
    db = [bool(e[f"{b}_detected"]) for e in ev]
    both = sum(x and y for x, y in zip(da, db))
    only_a = sum(x and not y for x, y in zip(da, db))
    only_b = sum(y and not x for x, y in zip(da, db))
    neither = sum(not x and not y for x, y in zip(da, db))
    joint = [e[f"{a}_delay_s"] - e[f"{b}_delay_s"] for e in ev
             if e[f"{a}_detected"] and e[f"{b}_detected"]]
    n = len(ev)
    return {"evaluable_episodes": n, "both_detected": both, f"{a}_only": only_a, f"{b}_only": only_b,
            "neither": neither,
            f"{a}_recall": da.count(True) / n if n else None,
            f"{b}_recall": db.count(True) / n if n else None,
            "recall_difference_bootstrap_95ci": bootstrap_ci([float(y) - float(x) for x, y in zip(da, db)]),
            "mcnemar_exact_p": mcnemar_exact(only_a, only_b),
            "median_delay_gain_s_jointly_detected": statistics.median(joint) if joint else None,
            "note": "episodes are the unit; rows inside one episode are not independent"}


# ------------------------------------------------------------------- native arm
def augment_dataset(dataset: dict, index: ProbeIndex) -> dict:
    """Add an `aws_ripe_path` view: AWS-only columns + as-of per-probe path features.

    The AWS part is copied from the row's own aws_only view, so the two arms differ
    ONLY by external path information. Readiness = AWS readiness (external missing
    stays missing and is imputed only inside the model with an explicit mask).
    """
    rows = []
    for row in dataset.get("rows", []):
        base = row["views"]["aws_only"]
        values = dict(base["values"])
        for region in REGIONS:
            for k, v in path_features(index, region, float(row["at_unix"])).items():
                values[f"{region}.{k}"] = v
        view = {"values": values, "missing": {k: v is None for k, v in values.items()},
                "ready_for_shadow_assessment": base["ready_for_shadow_assessment"]}
        rows.append({**row, "views": {**row["views"], "aws_ripe_path": view}})
    return {**dataset, "rows": rows}


def native_paired(dataset: dict, manifest: dict, reviews: dict, index: ProbeIndex, *,
                  embargo_seconds: float = 900, min_confidence: float = .55) -> dict:
    """Retrain aws_only vs aws_ripe_path on the IDENTICAL reviewed split."""
    from .network_binary import task_data, chronological_run_split, score
    from .training import vector
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    task = task_data(augment_dataset(dataset, index), manifest, reviews)
    split = chronological_run_split(task["episodes"], embargo_seconds)
    sets = {p: sorted((r for r in task["rows"] if r["episode_id"] in set(split[p])), key=lambda r: r["at_unix"])
            for p in ("train", "validation", "test")}
    onset = {e["episode_id"]: e["start_unix"] for e in task["episodes"]}
    arms, per_ep = {}, defaultdict(dict)
    for group in ("aws_only", "aws_ripe_path"):
        train = [r for r in sets["train"] if r["views"][group]["ready_for_shadow_assessment"]]
        names = sorted(train[0]["views"][group]["values"])
        model = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                          ("scale", StandardScaler()),
                          ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced", random_state=42))])
        model.fit([vector(r, group, names) for r in train], [r["label"] for r in train])
        classes = list(model.classes_)
        preds = []
        for r in sets["test"]:
            if not r["views"][group]["ready_for_shadow_assessment"]:
                preds.append(None)
                continue
            p = model.predict_proba([vector(r, group, names)])[0]
            j = int(max(range(len(p)), key=lambda i: p[i]))
            preds.append(classes[j] if p[j] >= min_confidence else None)
        arms[group] = score(sets["test"], preds, onset)
        for r, p in zip(sets["test"], preds):
            d = per_ep[r["episode_id"]]
            d.setdefault("label", r["label"])
            if p == "network_degradation" and f"{group}_first" not in d:
                d[f"{group}_first"] = r["at_unix"]
    episodes = []
    for eid, d in per_ep.items():
        if d["label"] != "network_degradation":
            continue
        e = {"episode_id": eid, "evaluable": True}
        for g, key in (("aws_only", "aws_only"), ("aws_ripe_path", "aws_ripe")):
            t = d.get(f"{g}_first")
            e[f"{key}_detected"] = t is not None
            e[f"{key}_delay_s"] = t - onset[eid] if t is not None else None
        episodes.append(e)
    return {"split": split, "arms": arms, "episodes": episodes, "paired": paired_summary(episodes)}


# ------------------------------------------------------------------------- CLI
def _region_files(values: list[str]) -> dict[str, list[Path]]:
    spec: dict[str, list[Path]] = defaultdict(list)
    for v in values:
        region, _, path = v.partition("=")
        spec[region].append(Path(path))
    return spec


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download public RIPE results (read-only)")
    f.add_argument("--msm", type=int, required=True)
    f.add_argument("--start", type=int, required=True)
    f.add_argument("--stop", type=int, required=True)
    f.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("study", help="candidate events (+ AWS join / native retrain if data given)")
    s.add_argument("--ripe", action="append", required=True, metavar="REGION=PATH")
    s.add_argument("--snapshots", type=Path, help="archived model_input.v1 JSONL (real AWS values)")
    s.add_argument("--dataset", type=Path)
    s.add_argument("--labels", type=Path)
    s.add_argument("--reviews", type=Path)
    s.add_argument("--out", type=Path, default=Path("data/ml-results/native/ripe-value"))
    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        print("results:", fetch(args.msm, args.start, args.stop, args.out))
        return 0
    samples = load_samples(_region_files(args.ripe))
    index = ProbeIndex(samples)
    events = candidate_events(samples)
    report = {"report_version": REPORT_VERSION, "forecasting_horizon_seconds": 0,
              "thresholds": {"inflation_ms": INFLATION_MS, "inflation_ratio": INFLATION_RATIO,
                             "baseline_seconds": BASELINE_SECONDS, "min_sustained_rounds": MIN_SUSTAINED_ROUNDS,
                             "unreachable_rounds": UNREACHABLE_ROUNDS},
              "ripe_samples": len(samples), "candidate_events": events,
              "status": "candidates_only", "blockers": []}
    if args.snapshots:
        joined = join_events_with_aws(events, _read_jsonl(args.snapshots), index)
        report["aws_join"] = joined
        report["aws_join_paired"] = paired_summary(joined)
        report["status"] = "aws_joined_candidates_unreviewed"
    else:
        report["blockers"].append("no archived AWS snapshots supplied: AWS-only behaviour during events unknown")
    if args.dataset and args.labels and args.reviews:
        load = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
        report["native"] = native_paired(load(args.dataset), load(args.labels), load(args.reviews), index)
        report["status"] = "native_paired_evaluated"
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "ripe-value-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"RIPE VALUE: {report['status']} | samples {len(samples)} | candidate events {len(events)}")
    for key in ("aws_join_paired",):
        if key in report:
            print(key, json.dumps(report[key]))
    if "native" in report:
        print("native paired", json.dumps(report["native"]["paired"]))
    for b in report["blockers"]:
        print("GAP:", b)
    print("Report:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
