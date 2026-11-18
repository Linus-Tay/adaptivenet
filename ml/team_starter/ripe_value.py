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
# Loss quorum: >=2 distinct reachable probes losing packets and pooled loss >=10%
# within the trailing window (two 240 s rounds). One probe dropping one ping is noise.
LOSS_WINDOW_SECONDS = 480
LOSS_QUORUM_PROBES = 2
LOSS_QUORUM_PCT = 10.0

PATH_COLUMNS = ("ripe_max_rtt_inflation_ms", "ripe_paths_inflated", "ripe_reachable_probes",
                "ripe_loss_pct_reachable", "ripe_probes_unreachable", "ripe_result_age_s",
                "ripe_window_loss_pct", "ripe_window_probes_with_loss")


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
    w_sent = w_lost = 0
    lossy = set()
    for probe in index.probes(region):
        hist = index.upto((region, probe), at)
        recent = hist[-UNREACHABLE_ROUNDS:]
        if not hist or (len(recent) == UNREACHABLE_ROUNDS and all(s["rcvd"] == 0 for s in recent)):
            continue
        for smp in hist[::-1]:
            if smp["ts"] <= at - LOSS_WINDOW_SECONDS:
                break
            w_sent += smp["sent"]
            w_lost += smp["sent"] - smp["rcvd"]
            if smp["rcvd"] < smp["sent"]:
                lossy.add(probe)
    return {"ripe_window_loss_pct": 100.0 * w_lost / w_sent if w_sent else None,
            "ripe_window_probes_with_loss": len(lossy) if w_sent else None,
            "ripe_max_rtt_inflation_ms": max(inflations) if inflations else None,
            "ripe_paths_inflated": inflated if inflations else None,
            "ripe_reachable_probes": reachable,
            "ripe_loss_pct_reachable": 100.0 * lost / sent if sent else None,
            "ripe_probes_unreachable": unreachable,
            "ripe_result_age_s": at - latest_ts}


RIPE_VARIANTS = ("primary", "loss_only", "loss_or_rtt2")


def ripe_path_alert(feat: dict, variant: str = "primary") -> bool | None:
    """Fixed, pre-declared external rule. None = no usable external evidence.

    primary      : loss quorum OR >=1 inflated path (frozen before the campaign)
    loss_only    : loss quorum only                  (secondary, declared before trial data)
    loss_or_rtt2 : loss quorum OR >=2 inflated paths (secondary, declared before trial data)
    """
    if feat.get("ripe_reachable_probes") in (None, 0):
        return None
    loss = feat.get("ripe_window_loss_pct")
    quorum = (loss is not None and loss >= LOSS_QUORUM_PCT
              and (feat.get("ripe_window_probes_with_loss") or 0) >= LOSS_QUORUM_PROBES)
    inflated = feat.get("ripe_paths_inflated") or 0
    if variant == "loss_only":
        return bool(quorum)
    if variant == "loss_or_rtt2":
        return bool(quorum or inflated >= 2)
    if variant != "primary":
        raise ValueError(variant)
    return bool(inflated >= 1 or quorum)


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
def aws_view_alert(values: dict, ready: bool, region: str, history: list[float | None]) -> bool | None:
    """Most sensitive reasonable AWS-only detector, to be FAIR to AWS-only.

    Alerts on app down, 5xx ratio >= 5%, robust-z >= 3, or p95 latency >= 1.5x its own
    trailing median. None when internal data is not usable (abstain).
    Host memory/CPU drift is deliberately NOT used: it is not a network signal and in
    the Nov 10 study it moved with the experiment tooling, not with impairment.
    """
    if not ready or values.get(f"{region}.app_up") is None:
        return None
    if values[f"{region}.app_up"] == 0:
        return True
    err, z, p95 = (values.get(f"{region}.http_error_ratio"), values.get(f"{region}.http_p95_robust_z"),
                   values.get(f"{region}.http_p95_ms"))
    if err is not None and err >= 0.05:
        return True
    if z is not None and z >= 3:
        return True
    past = [h for h in history if h is not None]
    if p95 is not None and len(past) >= 10 and p95 >= 1.5 * statistics.median(past):
        return True
    return False


def aws_only_alert(slow: dict, region: str, history: list[float | None]) -> bool | None:
    view = project(slow, "aws_only")
    return aws_view_alert(view["values"], view["ready_for_shadow_assessment"], region, history)


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


# ------------------------------------------------------- reviewed-episode study
def episode_study(dataset: dict, index: ProbeIndex, *, exclusion_pad_s: float = 1800) -> dict:
    """Fixed-rule, no-training paired comparison on every reviewed episode.

    AWS-only uses the episode's own archived aws_only rows; AWS+RIPE alerts when
    AWS-only alerts OR the pre-declared RIPE rule fires on RIPE results inside the
    episode (as-of each result). No threshold is fitted, so no split is needed.
    Also scans all RIPE rounds OUTSIDE reviewed episodes (+/- pad) for alert onsets:
    that time is unlabeled, so the rate is an upper bound on false alarms, not a
    verified specificity.
    """
    rows = defaultdict(list)
    for r in dataset.get("rows", []):
        rows[r["episode_id"]].append(r)
    episodes, per = dataset.get("episodes", []), []
    for ep in sorted(episodes, key=lambda e: e["start_unix"]):
        region = ep.get("region")
        if region not in REGIONS or ep["episode_id"] not in rows:
            continue
        mine = sorted(rows[ep["episode_id"]], key=lambda r: r["at_unix"])
        first_aws, votes, hist = None, [], []
        for r in mine:
            view = r["views"]["aws_only"]
            a = aws_view_alert(view["values"], view["ready_for_shadow_assessment"], region, hist)
            hist.append(view["values"].get(f"{region}.http_p95_ms"))
            votes.append(a)
            if a and first_aws is None:
                first_aws = r["at_unix"]
        stamps = sorted({s["ts"] for p in index.probes(region)
                         for s in index.series[(region, p)] if ep["start_unix"] <= s["ts"] <= ep["end_unix"]})
        first_ripe, ripe_votes = None, []
        for t in stamps:
            f = path_features(index, region, t)
            alert = ripe_path_alert(f)
            ripe_votes.append(alert)
            if alert and first_ripe is None:
                first_ripe = t
        first_both = min([t for t in (first_aws, first_ripe) if t is not None], default=None)
        positive = ep["label"] in ("network_degradation", "partial_outage")
        per.append({"episode_id": ep["episode_id"], "label": ep["label"], "region": region,
                    "start_utc": iso(ep["start_unix"]), "duration_s": ep["end_unix"] - ep["start_unix"],
                    "positive": positive, "evaluable": any(v is not None for v in votes) or bool(stamps),
                    "aws_rows": len(mine), "aws_abstained_rows": sum(v is None for v in votes),
                    "ripe_rounds": len(stamps), "ripe_rounds_alerting": sum(bool(v) for v in ripe_votes),
                    "aws_only_detected": first_aws is not None, "aws_ripe_detected": first_both is not None,
                    "aws_only_delay_s": first_aws - ep["start_unix"] if first_aws is not None else None,
                    "aws_ripe_delay_s": first_both - ep["start_unix"] if first_both is not None else None})
    pos = [e for e in per if e["positive"]]
    neg = [e for e in per if not e["positive"]]
    # Unlabeled-time scan for alert onsets (RIPE rule only; AWS rows are not archived per round here).
    scan = {}
    for region in REGIONS:
        busy = [(e["start_unix"] - exclusion_pad_s, e["end_unix"] + exclusion_pad_s) for e in episodes]
        stamps = sorted({s["ts"] for p in index.probes(region) for s in index.series[(region, p)]})
        stamps = [t for t in stamps if not any(a <= t <= b for a, b in busy)]
        onsets, prev, covered = 0, False, 0.0
        for i, t in enumerate(stamps):
            alert = bool(ripe_path_alert(path_features(index, region, t)))
            onsets += alert and not prev
            prev = alert
            if i and t - stamps[i - 1] <= MAX_AGE_SECONDS:
                covered += t - stamps[i - 1]
        scan[region] = {"unlabeled_hours": covered / 3600, "ripe_alert_onsets": onsets,
                        "onsets_per_day_upper_bound": onsets * 86400 / covered if covered else None}
    return {"episodes": per,
            "positives": paired_summary(pos),
            "healthy_false_alarm_episodes": {"aws_only": sum(e["aws_only_detected"] for e in neg),
                                             "aws_ripe": sum(e["aws_ripe_detected"] for e in neg),
                                             "healthy_episodes": len(neg)},
            "unlabeled_time_scan": scan,
            "rule": {"loss_window_s": LOSS_WINDOW_SECONDS, "loss_quorum_probes": LOSS_QUORUM_PROBES,
                     "loss_quorum_pct": LOSS_QUORUM_PCT, "inflation_ms": INFLATION_MS,
                     "inflation_ratio": INFLATION_RATIO}}


def trial_request_delays(trials: list[dict], index: ProbeIndex) -> list[dict]:
    """RIPE-independent timing: first RIPE alert after the logged SSM *request*.

    Reviewed onsets were defined from RIPE itself, so delays from those onsets are
    circular. The request time and configured duration come from the experiment
    log, not from RIPE. A request is not proof of effect, so trials without review
    are reported, never counted as misses or hits.
    """
    out = []
    for t in trials:
        region, start = t.get("region"), t.get("requested_utc")
        if region not in REGIONS or not start or not isinstance(t.get("duration_seconds"), (int, float)):
            continue
        a = datetime.fromisoformat(start).timestamp()
        b = a + t["duration_seconds"]
        stamps = sorted({s["ts"] for p in index.probes(region) for s in index.series[(region, p)] if a <= s["ts"] <= b})
        first = next((x for x in stamps if ripe_path_alert(path_features(index, region, x))), None)
        out.append({"run_id": t.get("run_id"), "region": region, "requested_utc": start,
                    "configured_packet_loss_percent": t.get("configured_packet_loss_percent"),
                    "ripe_rounds": len(stamps), "alert_at_request": bool(ripe_path_alert(path_features(index, region, a))),
                    "first_ripe_alert_after_request_s": first - a if first is not None else None})
    return out


# ------------------------------------------------------- pre-registered campaign
def campaign_eval(slots: list[dict], amp_rows: dict[str, list[dict]], index: ProbeIndex,
                  variant: str = "primary") -> dict:
    """Evaluate the pre-registered campaign (docs/ml/RIPE_TRIALS_PROTOCOL.md).

    `slots`: one dict per executed slot with region, control_region, request_unix,
    duration_seconds, control_start_unix, control_end_unix and `effect_confirmed`
    (SSM output shows rollback armed AND rules added; this is NOT derived from RIPE).
    Positives are impaired windows; negatives are same-time windows in the other
    region. AWS-only uses AMP history only; AWS+RIPE = AWS-only OR frozen RIPE rule.
    """
    def aws_alerts(region: str, a: float, b: float, impaired: list[tuple[float, float]]):
        rows = amp_rows.get(region, [])
        hist = [r.get("http_p95_ms") for r in rows if a - BASELINE_SECONDS <= r["ts"] < a
                and not any(x <= r["ts"] <= y for x, y in impaired)]
        first, votes = None, []
        for r in rows:
            if not a <= r["ts"] <= b:
                continue
            values = {f"{region}.{k}": v for k, v in r.items() if k not in ("ts", "region")}
            v = aws_view_alert(values, True, region, hist)
            votes.append(v)
            if v and first is None:
                first = r["ts"]
        return first, votes

    def ripe_first(region: str, a: float, b: float):
        stamps = sorted({s["ts"] for p in index.probes(region) for s in index.series[(region, p)] if a <= s["ts"] <= b})
        return next((t for t in stamps if ripe_path_alert(path_features(index, region, t), variant)), None), len(stamps)

    impaired = defaultdict(list)
    for s in slots:
        if s.get("effect_confirmed"):
            impaired[s["region"]].append((s["request_unix"], s["request_unix"] + s["duration_seconds"]))
    pos, neg = [], []
    for s in slots:
        if not s.get("effect_confirmed"):
            continue
        for kind, region, a, b in (("impaired", s["region"], s["request_unix"], s["request_unix"] + s["duration_seconds"]),
                                   ("control", s["control_region"], s["control_start_unix"], s["control_end_unix"])):
            fa, votes = aws_alerts(region, a, b, impaired[region])
            fr, n = ripe_first(region, a, b)
            both = min([t for t in (fa, fr) if t is not None], default=None)
            e = {"slot": s["slot"], "kind": kind, "region": region, "loss_percent": s.get("loss_percent"),
                 "start_utc": iso(a), "aws_rows": len(votes), "ripe_rounds": n,
                 "evaluable": bool(votes) or n > 0,
                 "aws_only_detected": fa is not None, "aws_ripe_detected": both is not None,
                 "aws_only_delay_s": fa - a if fa is not None else None,
                 "aws_ripe_delay_s": both - a if both is not None else None}
            (pos if kind == "impaired" else neg).append(e)
    ev_neg = [e for e in neg if e["evaluable"]]
    by_loss = defaultdict(lambda: [0, 0, 0])
    for e in pos:
        if e["evaluable"]:
            by_loss[e["loss_percent"]][0] += 1
            by_loss[e["loss_percent"]][1] += e["aws_only_detected"]
            by_loss[e["loss_percent"]][2] += e["aws_ripe_detected"]
    return {"report_version": "adaptivenet.ripe_campaign_eval.v1", "ripe_variant": variant,
            "positives": pos, "controls": neg,
            "paired_positives": paired_summary(pos),
            "controls_false_alarms": {"evaluable": len(ev_neg),
                                      "aws_only": sum(e["aws_only_detected"] for e in ev_neg),
                                      "aws_ripe": sum(e["aws_ripe_detected"] for e in ev_neg)},
            "detection_by_loss_percent": {str(k): {"trials": v[0], "aws_only": v[1], "aws_ripe": v[2]}
                                          for k, v in sorted(by_loss.items())},
            "excluded_unconfirmed_slots": [s["slot"] for s in slots if not s.get("effect_confirmed")]}


def campaign_slots(plan: dict, log_rows: list[dict], trial_dir: Path) -> list[dict]:
    """Join plan + campaign log + guarded trial records (SSM check outcome)."""
    planned = {s["slot"]: s for s in plan["slots"]}
    out = []
    for row in log_rows:
        if row.get("status") != "submitted" or not row.get("trial_record"):
            continue
        rec_path = Path(row["trial_record"])
        # Logs store absolute paths from the recording Mac; elsewhere use trial_dir.
        if not rec_path.is_absolute() or not rec_path.is_file():
            rec_path = trial_dir / rec_path.name
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        p = planned[row["slot"]]
        req = datetime.fromisoformat(rec["requested_utc"]).timestamp()
        out.append({"slot": row["slot"], "region": rec["region"], "control_region": p["control_region"],
                    "loss_percent": rec["configured_packet_loss_percent"], "request_unix": req,
                    "duration_seconds": rec["duration_seconds"],
                    "control_start_unix": req + 240, "control_end_unix": req + rec["duration_seconds"],
                    "effect_confirmed": bool(rec.get("remote_rollback_armed_and_rules_added"))})
    return out


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
    s.add_argument("--trials-dir", type=Path, help="authorized-network-trials/*.json (request log)")
    s.add_argument("--campaign-dir", type=Path, help="ripe-campaign dir: campaign-log.jsonl, trials/, amp-*.jsonl")
    s.add_argument("--campaign-plan", type=Path)
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
        report["blockers"].append("candidate events not joined to archived AWS snapshots (--snapshots): "
                                  "AWS-only behaviour during those unlabeled events unknown")
    if args.dataset:
        report["episode_study"] = episode_study(json.loads(args.dataset.read_text(encoding="utf-8-sig")), index)
        report["status"] = "episode_study_evaluated"
        print("episode study", json.dumps(report["episode_study"]["positives"]),
              json.dumps(report["episode_study"]["healthy_false_alarm_episodes"]))
    if args.trials_dir:
        trials = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(args.trials_dir.glob("*.json"))]
        report["trial_request_delays"] = trial_request_delays(trials, index)
    if args.campaign_dir and args.campaign_plan:
        plan = json.loads(args.campaign_plan.read_text(encoding="utf-8"))
        log_rows = _read_jsonl(args.campaign_dir / "campaign-log.jsonl")
        slots = campaign_slots(plan, log_rows, args.campaign_dir / "trials")
        amp = {r: _read_jsonl(args.campaign_dir / f"amp-{r}.jsonl") for r in REGIONS
               if (args.campaign_dir / f"amp-{r}.jsonl").exists()}
        report["campaign"] = campaign_eval(slots, amp, index)
        report["campaign_secondary"] = {v: campaign_eval(slots, amp, index, v) for v in RIPE_VARIANTS[1:]}
        report["status"] = "campaign_evaluated"
        print("campaign", json.dumps(report["campaign"]["paired_positives"]),
              json.dumps(report["campaign"]["controls_false_alarms"]))
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
