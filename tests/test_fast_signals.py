"""Fast witness signal semantics: independent timestamps, freshness and no invented zeros."""
from datetime import datetime, timezone
import json
from pathlib import Path

from adaptivenet.ingestion.fast_signals import build_fast_signals, load_fast_signals
from adaptivenet.ingestion.witness import append_probe
from scripts import live_monitor


def sample(scope, seconds_ago, *, now=100000.0, success=True, latency=40.0):
    return {
        "timestamp": datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat(),
        "scope": scope,
        "http_status": 200 if success else None,
        "latency_ms": latency,
        "origin": "region-a",
        "outcome": "ok" if success else "network_error",
        "kind": "http_witness",
    }


def test_short_window_uses_actual_distinct_observation_timestamps():
    now = 100000.0
    events = [
        sample("cloudfront", 12, now=now, success=False),
        sample("cloudfront", 27, now=now, success=False),
        sample("cloudfront", 42, now=now, success=True),
        sample("cloudfront", 84, now=now, success=False),
        sample("region-a", 11, now=now),
        sample("region-b", 14, now=now),
    ]
    result = build_fast_signals(events, now=now)
    assert result["contract_version"] == "adaptivenet.fast_signals.v1"
    assert result["ready_for_fast_assessment"] is True
    cf = result["scopes"]["cloudfront"]
    assert cf["windows"]["60s"]["count"] == 3
    assert cf["windows"]["60s"]["failure_count"] == 2
    assert cf["windows"]["60s"]["failure_ratio"] == round(2 / 3, 4)
    assert cf["windows"]["300s"]["count"] == 4
    assert cf["consecutive_failed_requests"] == 2
    assert cf["windows"]["60s"]["http_latency_p95_ms"] == 40
    assert result["independent_vantage_count"] == 1


def test_absence_and_stale_data_are_unknown_not_healthy():
    now = 100000.0
    result = build_fast_signals([sample("region-a", 200, now=now)], now=now)
    assert not result["ready_for_fast_assessment"]
    assert result["scopes"]["cloudfront"]["windows"]["60s"]["failure_ratio"] is None
    assert result["scopes"]["region-a"]["age_seconds"] == 200
    assert result["scopes"]["region-a"]["fresh"] is False


def test_future_naive_and_invalid_records_not_counted():
    now = 100000.0
    x = sample("cloudfront", -60, now=now)
    y = sample("region-a", 0, now=now)
    y["timestamp"] = "2026-10-20T12:01:00"
    z = sample("region-b", 0, now=now)
    z["outcome"] = "made_up"
    result = build_fast_signals([x, y, z], now=now)
    assert not result["ready_for_fast_assessment"]
    assert all(v["windows"]["60s"]["count"] == 0 for v in result["scopes"].values())


def test_reads_journal_across_15_minute_window(tmp_path):
    path = tmp_path / "probe-events.jsonl"
    now = 100000.0
    for age in (10, 20, 80, 800):
        append_probe(path, sample("region-a", age, now=now))
    result = load_fast_signals(path, now=now)
    region = result["scopes"]["region-a"]
    assert region["windows"]["60s"]["count"] == 2
    assert region["windows"]["300s"]["count"] == 3
    assert region["windows"]["900s"]["count"] == 4


def test_external_docker_workers_do_not_duplicate_sqs_pollers():
    cmds = live_monitor.command_set(
        profile="adaptivenet-admin", interval=60, traffic=True,
        probe_interval=15, sqs=False, external_workers=True,
    )
    joined = " ".join(" ".join(args) for args, _ in cmds)
    assert "publish-watch" not in joined
    assert "consume-watch" not in joined
    assert "ingestion.py watch" not in joined
    assert "ingestion.py serve" in joined
    assert "continuous_probe.py" in joined
