"""Bounded local fault runner. No arbitrary URL, shell, AWS or Kubernetes access."""
import os
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import httpx
from .contracts import Fault
from .gateway import authorize

app = FastAPI(title="AdaptiveNet experiment runner")

@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-only"}

async def _gateway_request(method: str, path: str, body=None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        return await client.request(method, os.getenv("GATEWAY_URL", "http://127.0.0.1:8080") + path,
                                    json=body, headers={"X-Lab-Token": os.environ["LAB_TOKEN"]})

async def forward(method: str, path: str, body=None):
    r = await _gateway_request(method, path, body)
    return JSONResponse(r.json(), status_code=r.status_code)

async def gateway_json(method: str, path: str, body=None) -> dict:
    """For callers that act on the answer: returns parsed JSON, and a failed call is an error."""
    try:
        r = await _gateway_request(method, path, body)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"gateway call failed: {type(exc).__name__}") from exc

@app.post("/experiments")
async def start(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("POST", "/admin/faults", body.model_dump())

@app.delete("/experiments")
async def stop(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("DELETE", "/admin/faults")
