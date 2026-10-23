"""Identical stateless demo workload deployed in each region.

Besides the catalogue it exposes measured host/process stats (`/internal/stats`)
so the local lab has an in-region view, and a bounded, token-protected compute
fault that emulates saturation inside the region: every observer sees the extra
service time, unlike a gateway (network-path) fault.
"""
import asyncio
import os
import random
import time
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from .contracts import Fault
from .gateway import authorize

app = FastAPI(title="AdaptiveNet demo catalogue")
compute_fault: dict = {}
inflight = 0
capacity = {"replicas": 1}  # local emulation of scale-out: service time is shared across replicas
_cpu_mark = (time.monotonic(), time.process_time())

@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.get("/api/catalog")
async def catalog():
    global inflight
    inflight += 1
    try:
        fault, expiry = compute_fault.get("active", ({}, 0))
        if expiry > time.monotonic():
            # Emulated capacity: an overload's extra service time and error share are
            # divided across replicas, as spreading the same load over more pods would do.
            replicas = capacity["replicas"]
            await asyncio.sleep(fault["latency_ms"] / 1000 / replicas)
            if random.random() < fault["error_rate"] / replicas:
                return JSONResponse({"region": os.getenv("REGION", "local"), "error": "overloaded"}, status_code=503)
        return {"region": os.getenv("REGION", "local"), "items": [
            {"id": "coffee", "name": "Coffee", "price": 4.5},
            {"id": "tea", "name": "Tea", "price": 3.5},
        ]}
    finally:
        inflight -= 1

def meminfo() -> tuple[int | None, int | None]:
    """Available/total bytes from /proc/meminfo (Linux containers); None elsewhere."""
    try:
        values = {}
        with open("/proc/meminfo") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                values[key] = int(rest.split()[0]) * 1024
        return values.get("MemAvailable"), values.get("MemTotal")
    except (OSError, ValueError, IndexError):
        return None, None

def cpu_busy_pct() -> float | None:
    """Process CPU busy share since the previous call; measured, never invented."""
    global _cpu_mark
    wall, cpu = time.monotonic(), time.process_time()
    elapsed = wall - _cpu_mark[0]
    busy = None if elapsed <= 0 else round(min(100.0, 100.0 * (cpu - _cpu_mark[1]) / elapsed), 3)
    _cpu_mark = (wall, cpu)
    return busy

@app.get("/internal/stats")
def stats():
    _, expiry = compute_fault.get("active", ({}, 0))
    available, total = meminfo()
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = None
    return {"region": os.getenv("REGION", "local"), "inflight": inflight,
            "compute_fault_active": expiry > time.monotonic(), "emulated_replicas": capacity["replicas"],
            "cpu_busy_pct": cpu_busy_pct(), "node_load1": load1,
            "memory_available_bytes": available, "memory_total_bytes": total}

@app.post("/admin/compute-fault")
def inject(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    compute_fault["active"] = (body.model_dump(), time.monotonic() + body.duration_seconds)
    return {"status": "active", **body.model_dump()}

@app.delete("/admin/compute-fault")
def clear(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    compute_fault.clear()
    capacity["replicas"] = 1
    return {"status": "cleared"}

class Capacity(BaseModel):
    replicas: int = Field(ge=1, le=8)

@app.put("/admin/capacity")
def set_capacity(body: Capacity, x_lab_token: str | None = Header(default=None)):
    """LOCAL emulation of a Deployment scale; in Kubernetes the controller patches replicas instead."""
    authorize(x_lab_token)
    capacity["replicas"] = body.replicas
    return {"status": "applied", "emulated_replicas": body.replicas}
