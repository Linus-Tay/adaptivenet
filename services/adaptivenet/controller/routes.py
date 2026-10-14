"""Local-only observe/decide/apply endpoint. Never changes AWS resources."""
import asyncio
import os
import httpx
from fastapi import APIRouter, Header, HTTPException
from ..gateway import authorize
from ..collectors.internal import collect
from .engine import Controller
from ..experiment_runner import forward

router = APIRouter(prefix='/controller', tags=['Controller'])
engine = Controller()
lock = asyncio.Lock()

@router.get('/observations')
async def observations(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await collect()

@router.post('/tick')
async def tick(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    async with lock:
        obs = await collect()
        state = await forward('GET', '/admin/state')
        current = state['region_a_percent']
        decision = engine.evaluate(obs['region-a'], obs['region-b'], current)
        applied = False
        if decision['action'] == 'propose_shift' and os.getenv('LOCAL_AUTO_MITIGATION', 'false').lower() == 'true':
            await forward('PUT', '/admin/routes', {'region_a_percent': decision['target_region_a_percent']})
            engine.mark_applied()
            applied = True
        return {'observations': obs, 'decision': decision, 'applied': applied, 'scope': 'local_gateway_only', 'automation_enabled': os.getenv('LOCAL_AUTO_MITIGATION', 'false').lower() == 'true'}
