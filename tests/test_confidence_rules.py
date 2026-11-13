"""Confidence rules (services/adaptivenet/controller/confidence.py).

Snapshots here are hand-built TEST FIXTURES, not recorded telemetry.
"""
from __future__ import annotations

import itertools

import pytest

from adaptivenet.controller.confidence import ConfidenceRules, ConfidenceTracker, signal_votes

_ids = itertools.count()


def region(*, up=1, errors=0.0, p95=120, cpu=20, loss=0.0, rtt=60, probes=4, full=0,
           internal="fresh", external="fresh"):
    return {"quality": {"internal": internal, "external": external},
            "features": {"app_up": up, "http_error_ratio": errors, "http_p95_ms": p95, "cpu_busy_pct": cpu,
                         "ripe_probes_reporting": probes, "ripe_probes_full_loss": full,
                         "packet_loss_median_pct": loss, "network_rtt_p50_ms": rtt,
                         "memory_available_pct": 60}}


def snap(a=None, b=None):
    return {"snapshot_id": f"features-{next(_ids)}", "regions": {"region-a": a or region(), "region-b": b or region()}}


GOOD = {"region-a": True, "region-b": True}
A_HTTP_BAD = {"region-a": False, "region-b": True}


def feed(tracker, n, *, a=None, b=None, http=GOOD, start=0.0, step=5.0):
    t = start
    for _ in range(n):
        assert tracker.observe(snap(a, b), now=t, http_ok=http)
        t += step
    return t


# -- votes -------------------------------------------------------------------

def test_votes_per_signal():
    s = snap(region(up=0), region(loss=40))
    assert signal_votes(s, "region-a", False) == {"aws": "bad", "ripe": "good", "http": "bad"}
    assert signal_votes(s, "region-b", None) == {"aws": "good", "ripe": "bad", "http": "unknown"}


def test_stale_or_thin_data_is_unknown_not_good():
    s = snap(region(internal="stale", probes=2))
    assert signal_votes(s, "region-a") == {"aws": "unknown", "ripe": "unknown", "http": "unknown"}


def test_repeated_snapshot_counts_once():
    t = ConfidenceTracker()
    s = snap(region(up=0))
    assert t.observe(s, now=0, http_ok=A_HTTP_BAD)
    assert not t.observe(s, now=5, http_ok=A_HTTP_BAD)
    assert t.regions["region-a"].bad_windows == 1


# -- failover ----------------------------------------------------------------

def test_failover_needs_three_bad_windows_with_two_signals():
    """SSM-style stop: AWS app_up=0 and client HTTP fail, RIPE still fine."""
    t = ConfidenceTracker()
    now = feed(t, 2, a=region(up=0), http=A_HTTP_BAD)
    v = t.failover("region-a", now)
    assert not v.ok and v.reason == "bad_windows:2/3"
    now = feed(t, 1, a=region(up=0), http=A_HTTP_BAD, start=now)
    v = t.failover("region-a", now)
    assert v.ok and v.reason == "confident:AWS+HTTP_bad_for_3_windows"


def test_gray_network_failure_needs_ripe_and_http_to_agree():
    t = ConfidenceTracker()
    now = feed(t, 3, a=region(loss=35), http=A_HTTP_BAD)
    assert t.failover("region-a", now).reason == "confident:RIPE+HTTP_bad_for_3_windows"


def test_one_signal_alone_never_fails_over():
    t = ConfidenceTracker()
    now = feed(t, 10, a=region(loss=60))          # RIPE says bad, AWS + HTTP say good
    v = t.failover("region-a", now)
    assert not v.ok and v.reason == "signals_disagree:1/2_bad(RIPE)"


def test_one_good_window_resets_the_count():
    t = ConfidenceTracker()
    now = feed(t, 2, a=region(up=0), http=A_HTTP_BAD)
    now = feed(t, 1, start=now)
    now = feed(t, 2, a=region(up=0), http=A_HTTP_BAD, start=now)
    assert t.failover("region-a", now).reason == "bad_windows:2/3"


def test_no_failover_when_other_region_is_not_clean():
    t = ConfidenceTracker()
    now = feed(t, 3, a=region(up=0), b=region(loss=40), http=A_HTTP_BAD)
    assert t.failover("region-a", now).reason == "other_region_not_clean"
    t = ConfidenceTracker()
    now = feed(t, 3, a=region(up=0), b=region(internal="stale"), http=A_HTTP_BAD)
    assert t.failover("region-a", now).reason == "other_region_not_clean"


def test_failover_soon_after_failback_is_blocked_but_continuing_is_not():
    t = ConfidenceTracker()
    t.record_traffic_change(100, towards="region-a")       # a failback step
    now = feed(t, 3, a=region(up=0), http=A_HTTP_BAD, start=110)
    assert t.failover("region-a", now).reason.startswith("cooldown_reversal:")
    assert t.failover("region-a", 221).ok
    t.record_traffic_change(221, towards="region-b")       # failover step applied
    assert t.failover("region-a", 222).reason == "bad_windows:0/3"   # fresh windows per step
    now = feed(t, 3, a=region(up=0), http=A_HTTP_BAD, start=225)
    assert t.failover("region-a", now).ok                  # same direction: no cooldown


# -- failback ----------------------------------------------------------------

def test_failback_after_120s_clean_and_bad_window_restarts_clock():
    t = ConfidenceTracker()
    feed(t, 1, start=0)
    assert t.failback(60).reason == "primary_healthy:60/120s"
    feed(t, 1, a=region(p95=900), start=65)                # one slow window
    assert t.failback(70).reason.startswith("primary_not_clean")
    feed(t, 1, start=75)
    assert not t.failback(190).ok
    assert t.failback(195).ok


def test_failback_needs_two_good_signals():
    t = ConfidenceTracker()
    feed(t, 30, a=region(external="stale"), http={})       # AWS only
    assert t.failback(1000).reason == "primary_not_clean(AWS_good)"


def test_failback_waits_after_failover_and_after_rollback():
    t = ConfidenceTracker()
    feed(t, 1, start=0)
    t.record_traffic_change(150, towards="region-b")
    assert t.failback(200).reason.startswith("cooldown_reversal:")
    assert t.failback(271).ok
    t.restart_failback_clock()
    assert not t.failback(272).ok


# -- scaling -----------------------------------------------------------------

def test_scale_up_needs_three_hot_windows_and_running_app():
    t = ConfidenceTracker()
    now = feed(t, 2, a=region(cpu=90))
    assert t.scale_up("region-a", now).reason == "cpu_high_windows:2/3"
    now = feed(t, 1, a=region(cpu=90), start=now)
    assert t.scale_up("region-a", now).ok
    t = ConfidenceTracker()
    now = feed(t, 5, a=region(cpu=90, up=0))               # stopped app: failover case
    assert not t.scale_up("region-a", now).ok


def test_scale_cooldown_and_scale_down():
    t = ConfidenceTracker()
    feed(t, 3, a=region(cpu=90))
    t.record_scale_change(15)
    now = feed(t, 3, a=region(cpu=90), start=20)
    assert t.scale_up("region-a", now).reason.startswith("cooldown_scale:")
    assert t.scale_up("region-a", 316).ok

    t = ConfidenceTracker()
    feed(t, 1, a=region(cpu=10), start=0)
    assert t.scale_down("region-a", 400, current_min=2, baseline_min=2).reason == "already_at_baseline"
    assert t.scale_down("region-a", 200, current_min=3, baseline_min=2).reason == "quiet:200/300s"
    assert t.scale_down("region-a", 300, current_min=3, baseline_min=2).ok
    feed(t, 1, a=region(cpu=10, errors=0.1), start=305)     # errors break the quiet streak
    assert not t.scale_down("region-a", 700, current_min=3, baseline_min=2).ok


def test_rules_from_env():
    rules = ConfidenceRules.from_env({"CONFIDENCE_FAILOVER_BAD_WINDOWS": "5",
                                      "CONFIDENCE_FAILBACK_HEALTHY_SEC": "30"})
    assert rules.failover_bad_windows == 5 and rules.failback_healthy_sec == 30.0
    with pytest.raises(ValueError):
        ConfidenceRules.from_env({"CONFIDENCE_FAILOVER_BAD_WINDOWS": "x"})


def test_total_ripe_loss_counts_even_without_rtt():
    """RIPE_TOTAL_LOSS_RULE. All probes lost leaves no RTT, so ingestion marks RIPE
    quality "loss_only"; the single-snapshot path (end_to_end) still says unknown."""
    from adaptivenet.controller.end_to_end import network_health
    s = snap(region(external="loss_only", full=4, loss=100, rtt=None))
    assert signal_votes(s, "region-a")["ripe"] == "bad"
    assert network_health(s, "region-a")[0] == "unknown"   # unchanged existing rule
    s = snap(region(external="loss_only", full=0, loss=0, rtt=None))
    assert signal_votes(s, "region-a")["ripe"] == "good"
    s = snap(region(external="insufficient_or_stale"))
    assert signal_votes(s, "region-a")["ripe"] == "unknown"
