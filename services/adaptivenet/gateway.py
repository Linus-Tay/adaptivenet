"""LOCAL simulator, not an implementation of AWS Global Accelerator.

Each request selects a region. Failures are not silently retried. Faults expire
on a monotonic clock even if whoever set them disappears. A fault sits on the path
in front of the region, so the region's own metrics never see it.
"""
import asyncio
from contextlib import asynccontextmanager
import os
import random
import secrets
import time
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, Field
from .contracts import Fault


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=3, trust_env=False) as upstream:
        app.state.upstream = upstream
        yield


app = FastAPI(title="LOCAL route and fault simulator", lifespan=lifespan)
faults: dict = {}
weight_a = 100

def authorize(token: str | None):
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(401, "Lab token required")

class Weights(BaseModel):
    region_a_percent: int = Field(ge=0, le=100)
    expected_region_a_percent: int | None = Field(default=None, ge=0, le=100)

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
async def route(body: Weights, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    global weight_a
    if body.expected_region_a_percent is not None and body.expected_region_a_percent != weight_a:
        raise HTTPException(409, "Route weight changed")
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

@app.get("/api/catalog")
async def catalog(request: Request):
    region = "region-a" if random.random() * 100 < weight_a else "region-b"
    return await regional_catalog(region, request.app.state.upstream)


@app.get("/admin/probe/{region}")
async def probe(
    region: Literal["region-a", "region-b"], request: Request,
    x_lab_token: str | None = Header(default=None),
):
    authorize(x_lab_token)
    return await regional_catalog(region, request.app.state.upstream)


async def regional_catalog(region: str, upstream: httpx.AsyncClient):
    fault, expiry = faults.get(region, ({}, 0))
    if expiry > time.monotonic():
        if random.random() * 100 < fault.get("loss_pct", 0):
            return JSONResponse({"region": region, "error": "injected packet loss"}, status_code=504)
        await asyncio.sleep(fault["latency_ms"] / 1000)
        if random.random() < fault["error_rate"]:
            return JSONResponse({"region": region, "error": "injected HTTP error"}, status_code=503)
    url = os.getenv("REGION_A_URL" if region == "region-a" else "REGION_B_URL",
                    "http://127.0.0.1:8101" if region == "region-a" else "http://127.0.0.1:8102")
    try:
        response = await upstream.get(url + "/api/catalog")
        return JSONResponse(response.json(), status_code=response.status_code)
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"region": region, "error": "upstream unavailable"}, status_code=502)
