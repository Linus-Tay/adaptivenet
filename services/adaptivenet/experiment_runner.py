"""Bounded local fault runner. No arbitrary URL, shell, AWS or Kubernetes access.

Network faults go to the gateway (user path); compute faults go to the regional
workload itself, so in-region probes see them too.
"""
import os
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
import httpx
from .contracts import Fault
from .gateway import authorize

app = FastAPI(title="AdaptiveNet experiment runner")

REGION_ENV = {"region-a": "REGION_A_URL", "region-b": "REGION_B_URL"}
REGION_DEFAULT = {"region-a": "http://127.0.0.1:8101", "region-b": "http://127.0.0.1:8102"}

@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-only"}

async def forward(method: str, path: str, body=None, base: str | None = None):
    base = base or os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        r = await client.request(method, base + path, json=body, headers={"X-Lab-Token": os.environ["LAB_TOKEN"]})
    return JSONResponse(r.json(), status_code=r.status_code)

def region_base(region: str) -> str:
    return os.getenv(REGION_ENV[region], REGION_DEFAULT[region])

@app.post("/experiments")
async def start(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    if body.kind == "compute":
        return await forward("POST", "/admin/compute-fault", body.model_dump(), base=region_base(body.region))
    return await forward("POST", "/admin/faults", body.model_dump())

@app.delete("/experiments")
async def stop(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    for region in REGION_ENV:
        try:
            await forward("DELETE", "/admin/compute-fault", base=region_base(region))
        except httpx.HTTPError:
            pass  # a region that is down has no compute fault left to clear
    return await forward("DELETE", "/admin/faults")
