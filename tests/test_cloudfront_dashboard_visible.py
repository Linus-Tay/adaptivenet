"""Regression checks for CloudFront visibility and stale-source semantics."""
from adaptivenet.ingestion.api import HTML


def test_cloudfront_is_above_regions_and_nav_matches():
    assert HTML.index('<section id="cloudfront"') < HTML.index('<section id="regions"')
    assert HTML.index('<a href="#cloudfront"') < HTML.index('<a href="#regions"')


def test_stale_snapshots_do_not_appear_healthy():
    assert "const healthy=fresh?[a,b].filter(regionHealthy).length:0" in HTML
    assert 'Snapshot stale / partial' in HTML
    assert 'snapshot stale' in HTML


def test_collector_warning_without_implicit_collection():
    assert 'id="collector-alert"' in HTML
    assert 'The Refresh button does NOT collect' in HTML
    assert "fetch('/v1/features/latest'" in HTML
    assert 'global:cloudfront:' in HTML
