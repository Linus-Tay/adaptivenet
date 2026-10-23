"""Local-only observe -> predict -> review -> decide -> act -> verify endpoint.

Acts on the local gateway (route weights) and, when running in Kubernetes with
SCALE_TARGETS set, on Deployment replicas. Never changes AWS resources.
"""
import asyncio
import json
import logging
import os
import time
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from ..gateway import authorize
from ..collectors.internal import collect, collect_external, collect_global
from ..experiment_runner import forward
from .actuators import GatewayRouteActuator, scaler_from_env
from .local_snapshot import make_local_snapshot
from .mitigation import MitigationController

router = APIRouter(prefix='/controller', tags=['Controller'])
lock = asyncio.Lock()
log = logging.getLogger('adaptivenet.controller')

actuator = GatewayRouteActuator(forward, scaler=scaler_from_env(forward))

def new_engine() -> MitigationController:
    return MitigationController(
        mode=os.getenv('CONTROLLER_MODE', 'combined'),
        cooldown_seconds=float(os.getenv('CONTROLLER_COOLDOWN_SECONDS', '30')),
        route_step_percent=int(os.getenv('CONTROLLER_ROUTE_STEP_PERCENT', '25')),
        verify_after_seconds=float(os.getenv('CONTROLLER_VERIFY_AFTER_SECONDS', '10')),
        verify_deadline_seconds=float(os.getenv('CONTROLLER_VERIFY_DEADLINE_SECONDS', '90')),
        auto_failback=os.getenv('CONTROLLER_AUTO_FAILBACK', 'false').lower() == 'true',
        scale_available=actuator.scale_available)

engine = new_engine()
automation_override: bool | None = None

def automation_enabled() -> bool:
    if automation_override is not None:
        return automation_override
    return os.getenv('LOCAL_AUTO_MITIGATION', 'false').lower() == 'true'

async def gateway_json(method: str, path: str, body=None) -> dict:
    response = await forward(method, path, body)
    if response.status_code >= 400:
        raise HTTPException(502, 'Gateway rejected the controller request')
    return json.loads(response.body)

async def observe() -> tuple[dict, dict]:
    internal, external, user_path = await asyncio.gather(collect(), collect_external(), collect_global())
    observations = {'internal': internal, 'external': external, 'user_path': user_path}
    return observations, make_local_snapshot(internal, external, user_path, time.time())

@router.get('/observations')
async def observations(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    obs, snapshot = await observe()
    return {**obs, 'snapshot': snapshot}

@router.get('/journal')
async def journal(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return {'actions': engine.journal, 'pending_verification': [e['seq'] for e in engine.pending],
            'escalated_regions': sorted(engine.escalated)}

class Automation(BaseModel):
    enabled: bool

@router.put('/automation')
async def set_automation(body: Automation, x_lab_token: str | None = Header(default=None)):
    """Toggle actuation at runtime (adaptive vs static runs) without a restart."""
    authorize(x_lab_token)
    global automation_override
    automation_override = body.enabled
    return {'automation_enabled': automation_enabled()}

@router.post('/reset')
async def reset(x_lab_token: str | None = Header(default=None)):
    """Experiment isolation: clear streaks, cooldown, verification and journal between runs."""
    authorize(x_lab_token)
    async with lock:
        engine.reset()
        if hasattr(actuator.scaler, 'reset'):
            actuator.scaler.reset()
    return {'status': 'reset'}

@router.post('/tick')
async def tick(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await run_tick()

async def tick_forever(interval: float):
    """Background control loop; one failed tick never stops it."""
    while True:
        try:
            await run_tick()
        except Exception as error:  # noqa: BLE001 - keep the control loop alive
            log.warning('tick failed: %s', error)
        await asyncio.sleep(interval)

async def run_tick() -> dict:
    async with lock:  # one writer: concurrent ticks cannot interleave decide/apply
        obs, snapshot = await observe()
        current = (await gateway_json('GET', '/admin/state'))['region_a_percent']
        result = engine.decide(snapshot, current)
        decision = result['decision']
        applied, actuation = False, None
        if decision['action'] != 'hold' and automation_enabled():
            if decision['action'] == 'scale_out':
                actuation = await actuator.scale_out(decision['region'])
            else:
                actuation = await actuator.set_route(decision['target_region_a_percent'])
            engine.mark_applied(result, current, actuation)
            applied = True
        return {**result, 'observations': obs, 'current_region_a_percent': current, 'applied': applied,
                'actuation': actuation, 'automation_enabled': automation_enabled(),
                'scope': 'local_gateway' + (f'+scale:{os.getenv("SCALE_ACTUATOR", "none")}' if actuator.scale_available else ''),
                'mode': engine.mode}
