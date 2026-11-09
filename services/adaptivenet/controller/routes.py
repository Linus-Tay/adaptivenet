"""Local-only observe/decide/apply endpoint. Never changes AWS resources."""
import asyncio
import os
from pathlib import Path
import uuid
from fastapi import APIRouter, Header
from ..gateway import authorize
from ..collectors.internal import collect
from .engine import Controller
from ..experiment_runner import gateway_json
from ..mitigation.audit import AuditLog

router = APIRouter(prefix='/controller', tags=['Controller'])
engine = Controller()
lock = asyncio.Lock()
MAX_ROUTE_STEP = 25  # same per-change limit review_proposal enforces on model proposals

def bounded_target(current: int, target: int) -> int:
    return max(current - MAX_ROUTE_STEP, min(current + MAX_ROUTE_STEP, target))

def apply_block_reason(obs: dict, current: int, target: int) -> str | None:
    """Kill switch and audit trail shared with mitigation/; the audit record precedes the write."""
    if Path(os.getenv('MITIGATION_KILL_SWITCH_FILE', 'data/mitigation.disabled')).exists():
        return 'kill_switch_engaged'
    try:
        AuditLog(os.getenv('MITIGATION_AUDIT_PATH', 'data/mitigation-audit.jsonl')).append(
            'step_applied', cycle_id=uuid.uuid4().hex[:12], idempotency_key=f'controller-tick:{uuid.uuid4().hex}',
            step={'kind': 'shift_traffic', 'target': target, 'source': 'controller_tick'},
            previous=current, baseline_health=obs)
    except (OSError, ValueError):
        return 'audit_unavailable'
    return None

@router.get('/observations')
async def observations(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return await collect()

@router.post('/tick')
async def tick(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    async with lock:
        obs = await collect()
        state = await gateway_json('GET', '/admin/state')
        current = state['region_a_percent']
        decision = engine.evaluate(obs['region-a'], obs['region-b'], current)
        applied, applied_target, blocked = False, None, None
        if decision['action'] == 'propose_shift' and os.getenv('LOCAL_AUTO_MITIGATION', 'false').lower() == 'true':
            target = bounded_target(current, decision['target_region_a_percent'])
            blocked = apply_block_reason(obs, current, target)
            if not blocked:
                await gateway_json('PUT', '/admin/routes', {'region_a_percent': target})
                engine.mark_applied()
                applied, applied_target = True, target
        return {'observations': obs, 'decision': decision, 'applied': applied, 'applied_target_region_a_percent': applied_target,
                'blocked_reason': blocked, 'scope': 'local_gateway_only', 'automation_enabled': os.getenv('LOCAL_AUTO_MITIGATION', 'false').lower() == 'true'}
