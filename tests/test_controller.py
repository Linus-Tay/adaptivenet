from adaptivenet.controller.engine import Controller
from adaptivenet.collectors.internal import sample
import pytest
import httpx

GOOD = {'status':'ok','age_seconds':0,'samples':5,'error_ratio':0,'p95_ms':20}
BAD = {'status':'ok','age_seconds':0,'samples':5,'error_ratio':.8,'p95_ms':1200}

def test_three_consecutive_bad_samples_required():
    e=Controller()
    assert e.evaluate(BAD, GOOD, 50, now=1)['action']=='hold'
    assert e.evaluate(BAD, GOOD, 50, now=2)['action']=='hold'
    r=e.evaluate(BAD, GOOD, 50, now=3)
    assert r['action']=='propose_shift' and r['target_region_a_percent']==0
    e.mark_applied(now=3)
    assert e.evaluate(BAD, GOOD, 0, now=4)['reason']=='cooldown'

def test_unknown_never_causes_failover():
    e=Controller()
    for _ in range(10): assert e.evaluate({'status':'stale'}, BAD, 50)['action']=='hold'

def test_recovery_does_not_automatically_failback():
    e=Controller()
    for _ in range(5): assert e.evaluate(GOOD, GOOD, 0)['action']=='hold'

def test_bad_alternative_no_shift():
    e=Controller()
    for _ in range(5): assert e.evaluate(BAD, BAD, 50)['action']=='hold'
