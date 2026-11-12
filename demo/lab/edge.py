"""Emulated internet path between external observers and the two lab regions.

Local stand-in for the path RIPE Atlas probes and the HTTP witness traverse.
Network faults (added latency, packet loss) are applied ONLY here, so the
origin's internal metrics stay healthy: the gray-failure case where AWS-internal
monitoring looks fine but users on the external path suffer.

``/ping/{region}`` does a real round trip to the origin and adds any injected
delay; a "lost" ping returns 504 without contacting the origin.
``/http/{scope}`` forwards a real catalogue request: ``cloudfront`` goes through
the routing gateway (user path), ``region-a``/``region-b`` go to one origin.
"""
from __future__ import annotations

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

Region = Literal["region-a", "region-b"]
_faults: dict[str, tuple[dict, float]] = {}


class NetworkFault(BaseModel):
    region: Region
    latency_ms: int = Field(default=0, ge=0, le=5000)
    loss_pct: float = Field(default=0, ge=0, le=100)
    duration_seconds: int = Field(default=120, ge=1, le=900)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(title="AdaptiveNet LAB edge path emulator", lifespan=lifespan)


def _authorize(token: str | None) -> None:
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(401, "Lab token required")


def active(region: str, now: float | None = None) -> dict | None:
    now = time.monotonic() if now is None else now
    item = _faults.get(region)
    return item[0] if item and item[1] > now else None


def origin_url(region: str) -> str:
    return os.environ["REGION_A_URL" if region == "region-a" else "REGION_B_URL"]


async def _impair(region: str) -> bool:
    """Apply the region's path fault. Returns False when the packet is lost."""
    fault = active(region)
    if not fault:
        return True
    if fault["loss_pct"] and random.random() * 100 < fault["loss_pct"]:
        return False
    if fault["latency_ms"]:
        # Half on the way out, half on the way back, like a slow path.
        await asyncio.sleep(fault["latency_ms"] / 2000)
    return True


@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-lab-edge-emulator"}


@app.get("/ping/{region}")
async def ping(region: Region, request: Request):
    if not await _impair(region):
        return JSONResponse({"region": region, "outcome": "lost"}, status_code=504)
    started = time.perf_counter()
    try:
        response = await request.app.state.client.get(origin_url(region) + "/healthz")
        reachable = response.status_code < 500 or response.status_code == 503
    except httpx.HTTPError:
        reachable = False
    if not reachable:
        return JSONResponse({"region": region, "outcome": "lost"}, status_code=504)
    fault = active(region)
    if fault and fault["latency_ms"]:
        await asyncio.sleep(fault["latency_ms"] / 2000)
    return {"region": region, "outcome": "reply", "origin_rtt_ms": round((time.perf_counter() - started) * 1000, 3)}


@app.get("/http/{scope}")
async def http(scope: Literal["cloudfront", "region-a", "region-b"], request: Request):
    client: httpx.AsyncClient = request.app.state.client
    if scope != "cloudfront":
        if not await _impair(scope):
            return JSONResponse({"region": scope, "error": "lab path packet loss"}, status_code=504)
        url = origin_url(scope) + "/api/catalog"
    else:
        url = os.environ["GATEWAY_URL"] + "/api/catalog"
    try:
        response = await client.get(url)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)
    region = body.get("region") if isinstance(body, dict) else None
    if scope == "cloudfront" and region in ("region-a", "region-b"):
        # The gateway picked a region; that region's path impairment now applies.
        if not await _impair(region):
            return JSONResponse({"region": region, "error": "lab path packet loss"}, status_code=504)
    if region in ("region-a", "region-b"):
        fault = active(region)
        if fault and fault["latency_ms"]:
            await asyncio.sleep(fault["latency_ms"] / 2000)
    return JSONResponse(body, status_code=response.status_code)


@app.post("/admin/faults")
def inject(body: NetworkFault, x_lab_token: str | None = Header(default=None)):
    _authorize(x_lab_token)
    _faults[body.region] = (body.model_dump(), time.monotonic() + body.duration_seconds)
    return {"status": "active", **body.model_dump()}


@app.delete("/admin/faults")
def clear(x_lab_token: str | None = Header(default=None)):
    _authorize(x_lab_token)
    _faults.clear()
    return {"status": "cleared"}


@app.get("/admin/faults")
def state(x_lab_token: str | None = Header(default=None)):
    _authorize(x_lab_token)
    now = time.monotonic()
    return {r: {**f, "remaining_seconds": round(e - now, 1)} for r, (f, e) in _faults.items() if e > now}
