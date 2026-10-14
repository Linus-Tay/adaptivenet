"""HTTP black-box signals from each deployed demo API; no invented CPU data."""
import asyncio
from datetime import datetime, timezone
import os
import statistics
import time
import httpx

TARGETS = {'region-a': 'INTERNAL_REGION_A_URL', 'region-b': 'INTERNAL_REGION_B_URL'}

async def sample(url: str, count: int = 5) -> dict:
    timings, errors = [], 0
    async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
        for _ in range(count):
            start = time.monotonic()
            try:
                r = await client.get(url.rstrip('/') + '/api/catalog')
                timings.append((time.monotonic() - start) * 1000)
                errors += int(r.status_code >= 500)
            except httpx.HTTPError:
                errors += 1
    timings.sort()
    return {'status': 'ok', 'age_seconds': 0, 'samples': count,
            'error_ratio': errors/count, 'p95_ms': round(timings[min(len(timings)-1, int(len(timings)*.95))], 2) if timings else None,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'source': 'http_internal_blackbox'}

async def collect() -> dict:
    urls = {name: os.getenv(env) for name, env in TARGETS.items()}
    async def one(name, url):
        if not url: return name, {'status': 'not_configured'}
        try: return name, await sample(url)
        except (ValueError, TypeError): return name, {'status': 'invalid_configuration'}
    return dict(await asyncio.gather(*(one(n, u) for n,u in urls.items())))
