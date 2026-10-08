"""Bounded local fault runner. No arbitrary URL, shell, AWS or Kubernetes access."""
import os
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
import httpx
from .contracts import Fault
from .gateway import authorize

app = FastAPI(title="AdaptiveNet experiment runner")

@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-only"}

async def forward(method: str, path: str, body=None):
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        r = await client.request(method, os.getenv("GATEWAY_URL", "http://127.0.0.1:8080") + path,
                                 json=body, headers={"X-Lab-Token": os.environ["LAB_TOKEN"]})
    return JSONResponse(r.json(), status_code=r.status_code)

@app.post("/experiments")
async def start(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("POST", "/admin/faults", body.model_dump())

@app.delete("/experiments")
async def stop(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("DELETE", "/admin/faults")
