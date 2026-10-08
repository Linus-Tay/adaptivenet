"""Local control API. Manual routes only; no automatic mitigation yet."""
from fastapi import FastAPI, Header
from .gateway import authorize, Weights
from .experiment_runner import forward
from .registry import router as registry_router

app = FastAPI(title="AdaptiveNet control API")
app.include_router(registry_router)

@app.get("/healthz")
def health():
    return {"status": "ok", "controller": "not-implemented", "mode": "local-lab"}

@app.get("/state")
async def state(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("GET", "/admin/state")

@app.put("/routes")
async def route(body: Weights, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await forward("PUT", "/admin/routes", body.model_dump())
