"""Regression tests for time cutoff and catalog metric semantics. Zero external calls."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "scripts"))
from adaptivenet.ingestion.cli import cycle
from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.sources import PROMQL, Collection
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.store import Store
import catalog_probe

BASE = 1800000000.0

def sample(region: str, metric: str, val: float, t: float):
    return Observation(region, "aws_amp", metric, val, "unit", t, BASE, "fixture")

def test_real_cycle_uses_finish_time_not_start_time_for_later_region(tmp_path):
    future_ts = BASE + 8
    observations = []
    for region in ("region-a", "region-b"):
        observations.extend([sample(region, "app_up", 1, future_ts),
                             sample(region, "memory_available_bytes", 550, future_ts),
                             sample(region, "memory_total_bytes", 1000, future_ts)])
    with patch("adaptivenet.ingestion.cli.time.time", side_effect=[BASE, BASE+12]):
        with patch("adaptivenet.ingestion.cli.region_configs", return_value=[]):
            with patch("adaptivenet.ingestion.cli.collect_all", return_value=Collection(observations, "ok", [])):
                snapshot, stats=cycle(tmp_path / "journal.db")
    assert stats["inserted"] == 6
    assert snapshot["regions"]["region-b"]["quality"]["internal"] == "fresh"
    assert snapshot["regions"]["region-a"]["quality"]["internal"] == "fresh"
    assert snapshot["ready_for_internal_only_model"] is True
    assert snapshot["ready_for_model"] is False
    assert snapshot["computed_at_unix"] == BASE+12

def test_missing_5xx_falls_back_to_zero_only_if_catalog_request_series_exists():
    q = PROMQL["http_5xx_rps"][0]
    assert 'status=~"5.."' in q
    assert 'or (0 * sum(rate(' in q
    assert 'route="/api/catalog"' in q
    assert 'region="{name}"' in q

def test_no_series_annotated_as_absent_data_not_upstream_failure():
    s=make_snapshot([],BASE,errors=["region-a:http_rps:no_series"])
    assert "one_or_more_prometheus_series_absent" in s["quality_reasons"]
    assert "one_or_more_upstream_fetches_failed" not in s["quality_reasons"]
    s2=make_snapshot([],BASE,errors=["region-a:http_rps:no_series", "region-b:app_up:amp_access_denied"])
    assert "one_or_more_upstream_fetches_failed" in s2["quality_reasons"]

def test_old_ripe_results_never_become_fresh_due_to_new_cycle_clock(tmp_path):
    s=make_snapshot([Observation("region-a", "ripe_atlas", "packet_loss_pct", 0, "%", BASE-5000, BASE, "1/42")],BASE)
    assert s["regions"]["region-a"]["quality"]["external"]=="insufficient_or_stale"

def test_catalog_probe_is_dry_run_without_network_by_default():
    with patch.object(catalog_probe,"probe",side_effect=AssertionError("unexpected HTTP call")):
        assert catalog_probe.main(["--count", "2"]) == 0

def test_catalog_probe_bounded_and_real_endpoint_shape():
    with patch.object(catalog_probe,"probe",return_value=(200,"region-a")) as remote:
        with patch.object(catalog_probe,"time") as tm:
            assert catalog_probe.main(["--run","--count","2"])==0
    assert remote.call_count==4
