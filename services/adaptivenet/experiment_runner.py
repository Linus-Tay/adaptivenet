"""Bounded local fault runner, served by the control API. No arbitrary URL, shell, AWS or Kubernetes access."""
import os
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
import httpx
from .contracts import Fault
from .gateway import authorize

router = APIRouter()


async def forward(method: str, path: str, body=None):
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        r = await client.request(method, os.getenv("GATEWAY_URL", "http://127.0.0.1:8080") + path,
                                 json=body, headers={"X-Lab-Token": os.environ["LAB_TOKEN"]})
    return JSONResponse(r.json(), status_code=r.status_code)


@router.post("/experiments")
async def start(body: Fault, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("POST", "/admin/faults", body.model_dump())


@router.delete("/experiments")
async def stop(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("DELETE", "/admin/faults")
