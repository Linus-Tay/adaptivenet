"""LOCAL simulator, not an implementation of AWS Global Accelerator.

Each request selects a region. Failures are not silently retried. Faults expire
on a monotonic clock even if the experiment runner disappears.
"""
import asyncio
import os
import random
import secrets
import time
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field
from .contracts import Fault

app = FastAPI(title="LOCAL route and fault simulator")
faults: dict = {}
weight_a = 100

# External probes pin one region, like a RIPE Atlas measurement targets one
# regional endpoint. User requests never set it and follow the route weights.
PROBE_HEADER = "x-adaptivenet-probe-region"

def authorize(token: str | None):
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(401, "Lab token required")

class Weights(BaseModel):
    region_a_percent: int = Field(ge=0, le=100)

@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-simulator"}

@app.get("/admin/state")
def state(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    now = time.monotonic()
    return {"region_a_percent": weight_a, "faults": {
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
    if body.kind != "network":
        raise HTTPException(422, "The gateway only injects network-path faults; compute faults belong to the regional workload")
    faults[body.region] = (body.model_dump(), time.monotonic() + body.duration_seconds)
    return {"status": "active", **body.model_dump()}

@app.delete("/admin/faults")
def clear(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    faults.clear()
    return {"status": "cleared"}

_upstream: httpx.AsyncClient | None = None

def upstream() -> httpx.AsyncClient:
    # One shared client: building a client per request costs tens of milliseconds of
    # event-loop time each, which under concurrent probes showed up as fake latency.
    global _upstream
    if _upstream is None:
        _upstream = httpx.AsyncClient(timeout=3, trust_env=False)
    return _upstream

@app.get("/api/catalog")
async def catalog(request: Request):
    pinned = request.headers.get(PROBE_HEADER)
    if pinned in ("region-a", "region-b"):
        region = pinned
    else:
        region = "region-a" if random.random() * 100 < weight_a else "region-b"
    fault, expiry = faults.get(region, ({}, 0))
    if expiry > time.monotonic():
        await asyncio.sleep(fault["latency_ms"] / 1000)
        if random.random() < fault["error_rate"]:
            return JSONResponse({"region": region, "error": "injected HTTP error"}, status_code=503)
    url = os.getenv("REGION_A_URL" if region == "region-a" else "REGION_B_URL",
                    "http://127.0.0.1:8101" if region == "region-a" else "http://127.0.0.1:8102")
    try:
        response = await upstream().get(url + "/api/catalog")
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"region": region, "error": "upstream unavailable"}, status_code=502)
