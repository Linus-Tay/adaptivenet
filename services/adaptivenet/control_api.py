"""Local control API: manual routes plus the model-driven mitigation controller."""
import asyncio
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Header
from .gateway import authorize, Weights
from .experiment_runner import forward
from .registry import router as registry_router
from .collectors.atlas.client import router as atlas_router
from .controller.routes import router as controller_router, tick_forever, automation_enabled

@asynccontextmanager
async def lifespan(_: FastAPI):
    # CONTROLLER_TICK_SECONDS > 0 runs the control loop in-process; 0 = manual POST /controller/tick.
    interval = float(os.getenv("CONTROLLER_TICK_SECONDS", "0") or 0)
    task = asyncio.create_task(tick_forever(interval)) if interval > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()

app = FastAPI(title="AdaptiveNet control API", lifespan=lifespan)
app.include_router(registry_router)
app.include_router(atlas_router)
app.include_router(controller_router)

@app.get("/healthz")
def health():
    return {"status": "ok", "controller": "model-driven", "mode": "local-lab",
            "automation_enabled": automation_enabled(),
            "tick_seconds": float(os.getenv("CONTROLLER_TICK_SECONDS", "0") or 0)}

@app.get("/state")
async def state(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("GET", "/admin/state")

@app.put("/routes")
async def route(body: Weights, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("PUT", "/admin/routes", body.model_dump())
