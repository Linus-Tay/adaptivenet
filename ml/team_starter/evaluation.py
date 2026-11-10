"""Episode-disjoint chronological splits and honest detection metrics."""
from __future__ import annotations
from collections import Counter, defaultdict
import math
import statistics

from .feature_views import GROUPS


def split_episodes(episodes: list[dict], *, embargo_seconds: float = 900) -> dict:
    """Chronological split by entire experiment-run windows, with embargo.

    All phases from a run remain together, including gaps between phases.
    Overlapping run windows merge conservatively, even across regions.
    Discard boundary blocks until embargo is satisfied.
    """
    if embargo_seconds < 0:
        raise ValueError("embargo must be nonnegative")
    # Group *whole experiment runs* first. Multiple healthy, failure and
    # recovery episodes may be separated by gaps; they are NOT independent.
    # Interleaved runs are combined into a single chronological time block to
    # prevent train/validation/test from straddling overlapping run windows.
    runs = {}
    for e in episodes:
        ident = e["episode_id"]
        run_id = e.get("experiment_run_id", ident)  # legacy split inspection only
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"episode {ident} has invalid experiment_run_id")
        a, b = e["start_unix"], e["end_unix"]
        if b <= a:
            raise ValueError(f"episode {ident} has invalid interval")
        if run_id not in runs:
            runs[run_id] = {"start": a, "end": b, "ids": []}
        else:
            runs[run_id]["start"] = min(runs[run_id]["start"], a)
            runs[run_id]["end"] = max(runs[run_id]["end"], b)
        runs[run_id]["ids"].append(ident)
    blocks = []
    for run in sorted(runs.values(), key=lambda r: (r["start"], r["end"])):
        if not blocks or run["start"] >= blocks[-1]["end"]:
            blocks.append({"start": run["start"], "end": run["end"],
                           "ids": list(run["ids"])})
        else:
            blocks[-1]["end"] = max(blocks[-1]["end"], run["end"])
            blocks[-1]["ids"].extend(run["ids"])
    if len(blocks) < 3:
        raise ValueError("need >=3 chronologically distinct experiment blocks for train/validation/test")
    n = len(blocks)
    cut1, cut2 = max(1, int(n * .6)), max(2, int(n * .8))
    if cut2 >= n:
        cut2 = n - 1
    assigned = [blocks[:cut1], blocks[cut1:cut2], blocks[cut2:]]
    # Exclude blocks closest to split borders. No future label/feature leakage.
    removed = []
    for i in (1, 2):
        if not assigned[i - 1]:
            raise ValueError("embargo eliminated a partition")
        end = assigned[i - 1][-1]["end"]
        while assigned[i] and assigned[i][0]["start"] - end < embargo_seconds:
            removed += assigned[i].pop(0)["ids"]
    if any(not a for a in assigned):
        raise ValueError("not enough time-separated episodes after embargo")
    parts = {name: [ident for block in group for ident in block["ids"]]
             for name, group in zip(("train", "validation", "test"), assigned)}
    parts["excluded_for_embargo"] = removed
    if any(set(parts[a]) & set(parts[b]) for a,b in (("train","validation"),("train","test"),("validation","test"))):
        raise AssertionError("group leakage")
    return parts


def fixed_rule(row: dict, group: str) -> str | None:
    """A reproducible, untrained baseline; not an outage root-cause oracle."""
    if group not in GROUPS:
        raise ValueError(group)
    view = row["views"][group]
    if not view["ready_for_shadow_assessment"]:
        return None
    v = view["values"]
    for region in ("region-a", "region-b"):
        if v[f"{region}.app_up"] == 0:
            return "application_failure"
        if v[f"{region}.http_error_ratio"] is not None and v[f"{region}.http_error_ratio"] >= .25:
            return "application_failure"
    if group != "aws_only":
        for region in ("region-a", "region-b"):
            loss = v[f"{region}.packet_loss_pct"]
            if loss is not None and loss >= 20:
                return "network_degradation"
    if group == "aws_ripe_http":
        for region in ("region-a", "region-b"):
            count = v[f"http.{region}.300s.count"]
            ratio = v[f"http.{region}.300s.failure_ratio"]
            if count is not None and count >= 6 and ratio is not None and ratio >= .3:
                return "network_degradation"  # single witness != widespread outage
    for region in ("region-a", "region-b"):
        if v[f"{region}.cpu_busy_pct"] is not None and v[f"{region}.cpu_busy_pct"] >= 85:
            return "application_failure"  # possible internal overload, not verified root cause
    return "healthy"


def metrics(rows: list[dict], predictions: list[str | None],
            verified_onsets: dict[str, float] | None = None) -> dict:
    """Calculate multiclass + episode metrics. No observed rows => no score."""
    if len(rows) != len(predictions):
        raise ValueError("prediction length mismatch")
    if not rows:
        return {"status": "no_verified_test_rows", "samples": 0}
    classes = sorted(set(r["label"] for r in rows))
    counts = {}
    for label in classes:
        tp = sum(r["label"] == label and p == label for r, p in zip(rows, predictions))
        fp = sum(r["label"] != label and p == label for r, p in zip(rows, predictions))
        fn = sum(r["label"] == label and p != label for r, p in zip(rows, predictions))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        counts[label] = {"support": sum(r["label"] == label for r in rows), "precision": precision,
                         "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}
    covered = sum(p is not None for p in predictions)
    overall = sum(p == r["label"] for r, p in zip(rows, predictions)) / len(rows)
    by_ep = defaultdict(list)
    for row, pred in zip(rows, predictions):
        by_ep[row["episode_id"]].append((row["at_unix"], row["label"], pred))
    detected_delays = []
    misses = 0
    false_alarms = 0
    recovery_delays = []
    observed_healthy_seconds = 0
    for ep, samples in by_ep.items():
        samples.sort()
        label = samples[0][1]
        hits = [t for t, actual, pred in samples if pred == actual and pred is not None]
        if label not in ("healthy", "recovery", "transient"):
            if hits:
                detected_delays.append(max(0.0, hits[0] - (verified_onsets or {}).get(ep, samples[0][0])))
            else:
                misses += 1
        if label == "recovery" and hits:
            recovery_delays.append(max(0.0, hits[0] - (verified_onsets or {}).get(ep, samples[0][0])))
        if label == "healthy":
            # Count transitions into a nonhealthy alert, not every consecutive row.
            previous_alert = False
            for _, _, pred in samples:
                alert = pred not in (None, "healthy")
                if alert and not previous_alert:
                    false_alarms += 1
                previous_alert = alert
            if len(samples) >= 2:
                observed_healthy_seconds += samples[-1][0] - samples[0][0]
    sorted_delays = sorted(detected_delays)
    p95 = sorted_delays[math.ceil(.95 * len(sorted_delays)) - 1] if sorted_delays else None
    return {"status": "evaluated", "samples": len(rows), "episode_count": len(by_ep),
            "coverage": covered / len(rows), "abstentions": len(rows) - covered,
            "accuracy_including_abstentions_as_errors": overall,
            "macro_f1": statistics.mean(x["f1"] for x in counts.values()),
            "per_class": counts, "missed_incident_episodes": misses,
            "detected_incident_episodes": len(detected_delays),
            "detection_delay_median_seconds": statistics.median(detected_delays) if detected_delays else None,
            "detection_delay_p95_seconds": p95,
            "time_to_recovery_median_seconds": (statistics.median(recovery_delays) if recovery_delays else None),
            "healthy_observed_hours": observed_healthy_seconds / 3600,
            "false_alert_transitions": false_alarms,
            "false_alarms_per_24h_observed_healthy": (false_alarms * 86400 / observed_healthy_seconds
                                                      if observed_healthy_seconds > 0 else None),
            "limits": ["detection_delay_from_verified_onset_when_manifest_supplied",
                       "false_alarm_day_denominator_only_observed_healthy_windows",
                       "sample_weighted_scores_can_overweight_long_episodes"]}
