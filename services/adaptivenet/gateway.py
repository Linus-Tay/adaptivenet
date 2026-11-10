"""LOCAL simulator, not an implementation of AWS Global Accelerator.

Each request selects a region. By default failures are not silently retried, so a
fault stays fully visible. Three request-level mitigations can be switched on through
/admin/mitigations (all off by default); every response they change carries an
X-Mitigation header so an experiment can still count what really happened:

* failover   - one retry on the other region when the first fails or is too slow
               (what a CloudFront origin group does on 5xx / origin timeout)
* serve stale - answer from the last good response when every attempt failed
               (what CloudFront does with a cached object and stale-if-error)
* load shed  - refuse requests above a concurrency cap with a fast 429
               (what a WAF rate rule or API throttle does)

Faults expire on a monotonic clock even if the experiment runner disappears.
"""
import asyncio
import os
import random
import secrets
import time
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field
from .contracts import Fault

app = FastAPI(title="LOCAL route and fault simulator")
faults: dict = {}
weight_a = 100

def authorize(token: str | None):
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(401, "Lab token required")

class Weights(BaseModel):
    region_a_percent: int = Field(ge=0, le=100)

class Mitigations(BaseModel):
    failover: bool = False
    attempt_timeout_ms: int = Field(default=500, ge=50, le=3000)  # first-attempt deadline; used only with failover
    serve_stale_seconds: int = Field(default=0, ge=0, le=300)     # 0 = off; max age of a response worth reusing
    max_in_flight: int = Field(default=0, ge=0, le=10000)         # 0 = unlimited

mitigations = Mitigations()
last_good: tuple[dict, float] | None = None  # (body, monotonic time) of the latest successful catalog response
in_flight = 0

@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-simulator"}

@app.get("/admin/state")
def state(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    now = time.monotonic()
    return {"region_a_percent": weight_a, "mitigations": mitigations.model_dump(), "faults": {
        r: {**f[0], "remaining_seconds": max(0, round(f[1] - now, 1))}
        for r, f in faults.items() if f[1] > now}}

@app.put("/admin/routes")
def route(body: Weights, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    global weight_a
    weight_a = body.region_a_percent
    return {"region_a_percent": weight_a}

@app.post("/admin/faults")
def inject(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    faults[body.region] = (body.model_dump(), time.monotonic() + body.duration_seconds)
    return {"status": "active", **body.model_dump()}

@app.delete("/admin/faults")
def clear(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    faults.clear()
    return {"status": "cleared"}

@app.put("/admin/mitigations")
def set_mitigations(body: Mitigations, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    global mitigations
    mitigations = body
    return mitigations.model_dump()

@app.delete("/admin/mitigations")
def clear_mitigations(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    global mitigations, last_good
    mitigations, last_good = Mitigations(), None
    return mitigations.model_dump()

async def upstream(region: str) -> tuple[int, dict]:
    url = os.getenv("REGION_A_URL" if region == "region-a" else "REGION_B_URL",
                    "http://127.0.0.1:8101" if region == "region-a" else "http://127.0.0.1:8102")
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(url + "/api/catalog")
        return response.status_code, response.json()
    except (httpx.HTTPError, ValueError):
        return 502, {"region": region, "error": "upstream unavailable"}

async def call_region(region: str) -> tuple[int, dict]:
    fault, expiry = faults.get(region, ({}, 0))
    if expiry > time.monotonic():
        await asyncio.sleep(fault["latency_ms"] / 1000)
        if random.random() < fault["error_rate"]:
            return 503, {"region": region, "error": "injected HTTP error"}
    return await upstream(region)

async def attempt(region: str, timeout_s: float | None) -> tuple[int, dict]:
    try:
        return await asyncio.wait_for(call_region(region), timeout_s)
    except asyncio.TimeoutError:
        return 504, {"region": region, "error": "attempt timed out"}

@app.get("/api/catalog")
async def catalog():
    global in_flight, last_good
    m = mitigations
    if m.max_in_flight and in_flight >= m.max_in_flight:
        return JSONResponse({"error": "load shed"}, status_code=429,
                            headers={"Retry-After": "1", "X-Mitigation": "shed"})
    in_flight += 1
    try:
        region = "region-a" if random.random() * 100 < weight_a else "region-b"
        status, body = await attempt(region, m.attempt_timeout_ms / 1000 if m.failover else None)
        headers = {}
        if status >= 500 and m.failover:
            other = "region-b" if region == "region-a" else "region-a"
            retry_status, retry_body = await attempt(other, None)
            if retry_status < 500:
                status, body, headers = retry_status, retry_body, {"X-Mitigation": "failover"}
        if status < 500:
            if status == 200:
                last_good = (body, time.monotonic())
        elif m.serve_stale_seconds and last_good and time.monotonic() - last_good[1] <= m.serve_stale_seconds:
            status, body, headers = 200, last_good[0], {"X-Mitigation": "stale"}
        return JSONResponse(body, status_code=status, headers=headers)
    finally:
        in_flight -= 1
