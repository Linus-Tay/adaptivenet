"""CloudFront display-only last-known values, strict live freshness and ML safety."""
import pytest

from adaptivenet.ingestion.models import Observation
from adaptivenet.ingestion.store import Store
from adaptivenet.ingestion.features import make_snapshot
from adaptivenet.ingestion.handoff import build_model_input
from adaptivenet.ingestion.api import HTML

NOW = 1800000000.0
CF = "E1234567890ABC"
OTHER = "EOTHER1234567"


def event(metric, value, at, *, distribution=CF):
    return Observation("global", "aws_cloudwatch", metric, value, "%" if "pct" in metric else "requests",
                       at, at + 1, distribution)


def test_stale_samples_retained_separately_and_never_enter_live_model(tmp_path):
    store = Store(tmp_path / "cf.db")
    old = event("cloudfront_requests", 5, NOW - 3600)
    err = event("cloudfront_4xx_pct", 0, NOW - 3600)
    store.record([old, err], [], "ok", at=NOW - 3500)
    # Deliberately use a short 35m live window: old traffic is no longer there.
    live_events = store.events_since(NOW - 2100, end=NOW)
    assert live_events == []
    historical = store.latest_cloudfront_observations(CF, at=NOW)
    snap = make_snapshot(live_events, NOW, cloudfront_distribution=CF,
                         cloudfront_last_observations=historical)
    assert snap["cloudfront"]["status"] == "no_fresh_metrics"
    assert snap["cloudfront"]["features"]["cloudfront_requests"] is None
    assert snap["cloudfront"]["features"]["cloudfront_4xx_pct"] is None
    assert snap["cloudfront"]["last_known"]["cloudfront_requests"]["value"] == 5
    assert snap["cloudfront"]["last_known"]["cloudfront_4xx_pct"]["value"] == 0
    assert snap["cloudfront"]["last_known"]["cloudfront_requests"]["display_only"] is True
    model = build_model_input(snap, now=NOW, mode="combined")
    assert model["cloudfront"]["features"]["cloudfront_requests"] is None
    assert "last_known" not in model["cloudfront"]
    assert model["ready_for_inference"] is False


def test_last_known_preserved_even_after_multiple_quiet_cycles(tmp_path):
    store = Store(tmp_path / "events.db")
    store.record([event("cloudfront_requests", 12, NOW - 300)], [], "ok", at=NOW)
    for offset in (900, 2101, 20000, 86400):
        clock = NOW + offset
        rows = store.latest_cloudfront_observations(CF, at=clock)
        snap = make_snapshot(store.events_since(clock - 2100, end=clock),clock,
                             cloudfront_distribution=CF,
                             cloudfront_last_observations=rows)
        assert snap["cloudfront"]["features"]["cloudfront_requests"] is None
        assert snap["cloudfront"]["last_known"]["cloudfront_requests"]["value"] == 12


def test_last_known_is_distribution_scoped_and_retention_bounded(tmp_path):
    store = Store(tmp_path / "isolation.db")
    store.record([event("cloudfront_requests", 3, NOW - 300),
                  event("cloudfront_requests", 99, NOW - 200, distribution=OTHER)], [], "ok", at=NOW)
    assert [x.value for x in store.latest_cloudfront_observations(CF, at=NOW)] == [3]
    assert store.latest_cloudfront_observations(CF, at=NOW + 8 * 86400) == []


def test_last_known_is_not_confused_with_current_zero_value():
    fresh = event("cloudfront_5xx_pct", 0, NOW - 1)
    snap = make_snapshot([fresh], NOW, cloudfront_distribution=CF)
    assert snap["cloudfront"]["features"]["cloudfront_5xx_pct"] == 0
    assert snap["cloudfront"]["last_known"]["cloudfront_5xx_pct"]["value"] == 0


def test_ui_explicitly_displays_history_but_never_labels_it_live():
    assert "function cloudfrontReading" in HTML
    assert "cf.last_known?.[key]" in HTML
    assert "Last observed" in HTML
    assert "display_only" not in HTML or "last_known" in HTML
    assert "No CloudFront observations are stored yet" in HTML
