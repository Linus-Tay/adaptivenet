"""HTTP black-box signals for each region from three vantage points; no invented data.

internal: probes the regional workload directly (an in-cloud health check) and reads
          its measured host/process stats.
external: probes the same region through the user-facing path (the local gateway,
          standing in for the Internet path a RIPE Atlas probe measures).
global:   probes the user-facing path without pinning a region: what users get now.
"""
import asyncio
from datetime import datetime, timezone
import os
import statistics
import time
import httpx

TARGETS = {'region-a': 'INTERNAL_REGION_A_URL', 'region-b': 'INTERNAL_REGION_B_URL'}
PROBE_HEADER = 'x-adaptivenet-probe-region'

async def sample(url: str, count: int = 5, headers: dict | None = None, source: str = 'http_internal_blackbox') -> dict:
    """Send `count` probes concurrently so one slow region cannot stretch the control tick."""
    async def probe(client):
        start = time.monotonic()
        try:
            r = await client.get(url.rstrip('/') + '/api/catalog', headers=headers)
            return (time.monotonic() - start) * 1000, r.status_code >= 500
        except httpx.HTTPError:
            return None, True
    async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
        results = await asyncio.gather(*(probe(client) for _ in range(count)))
    timings = sorted(t for t, _ in results if t is not None)
    errors = sum(failed for _, failed in results)
    # Five samples cannot support a p95 (it would just be the maximum). The median
    # is the robust statistic; one slow outlier cannot trigger action on its own.
    return {'status': 'ok', 'age_seconds': 0, 'samples': count,
            'error_ratio': errors/count, 'latency_stat': 'median',
            'latency_ms': round(statistics.median(timings), 2) if timings else None,
            'max_ms': round(timings[-1], 2) if timings else None,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'source': source}

async def stats(url: str) -> dict | None:
    """Measured host/process stats from the workload; None when unavailable."""
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            r = await client.get(url.rstrip('/') + '/internal/stats')
        return r.json() if r.is_success else None
    except (httpx.HTTPError, ValueError):
        return None

async def collect() -> dict:
    urls = {name: os.getenv(env) for name, env in TARGETS.items()}
    async def one(name, url):
        if not url: return name, {'status': 'not_configured'}
        try:
            probe, host = await asyncio.gather(sample(url), stats(url))
            return name, {**probe, 'host': host}
        except (ValueError, TypeError): return name, {'status': 'invalid_configuration'}
    return dict(await asyncio.gather(*(one(n, u) for n,u in urls.items())))

async def collect_external() -> dict:
    url = os.getenv('EXTERNAL_PROBE_URL')
    async def one(name):
        if not url: return name, {'status': 'not_configured'}
        try: return name, await sample(url, headers={PROBE_HEADER: name}, source='http_external_path')
        except (ValueError, TypeError): return name, {'status': 'invalid_configuration'}
    return dict(await asyncio.gather(*(one(n) for n in TARGETS)))

async def collect_global() -> dict:
    """Unpinned user-path probes: the recovery signal a user actually experiences."""
    url = os.getenv('EXTERNAL_PROBE_URL')
    if not url:
        return {'status': 'not_configured'}
    try:
        return await sample(url, source='http_user_path')
    except (ValueError, TypeError):
        return {'status': 'invalid_configuration'}
