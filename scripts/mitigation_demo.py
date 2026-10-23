#!/usr/bin/env python3
"""End-to-end local check of the model-driven mitigation loop with real processes.

Starts the five lab services, enables automation, then for each scenario injects a
fault, ticks the controller every --tick seconds and reports: ticks to detect,
the action taken, the route/scale actuation and the recovery verification result.
Exit status is non-zero when a scenario does not produce its expected action.

    python scripts/mitigation_demo.py            # all scenarios
    python scripts/mitigation_demo.py --scenario network --tick 2
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local import start, stop, ROOT  # noqa: E402

SCENARIOS = {
    # name: (fault body, expected controller action, expected final weight or None)
    'healthy':  (None, 'hold', 100),
    'network':  ({'region': 'region-a', 'kind': 'network', 'latency_ms': 1500, 'duration_seconds': 120}, 'shift_traffic', None),
    'compute':  ({'region': 'region-a', 'kind': 'compute', 'latency_ms': 1200, 'duration_seconds': 120}, 'scale_out', 100),
    'critical': ({'region': 'region-a', 'kind': 'compute', 'error_rate': 1.0, 'duration_seconds': 120}, 'failover', 0),
}


def wait_ready(client, processes):
    for port in (8101, 8102, 8080, 8000, 8001):
        for _ in range(100):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError('a service could not start; check for port conflicts')
            try:
                if client.get(f'http://127.0.0.1:{port}/healthz').is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError(f'port {port} did not become ready')


def run_scenario(client, headers, name, tick, max_ticks, verbose):
    fault, expected, expected_weight = SCENARIOS[name]
    client.post('http://127.0.0.1:8000/controller/reset', headers=headers).raise_for_status()
    client.delete('http://127.0.0.1:8001/experiments', headers=headers).raise_for_status()
    client.put('http://127.0.0.1:8000/routes', headers=headers, json={'region_a_percent': 100}).raise_for_status()
    if fault:
        client.post('http://127.0.0.1:8001/experiments', headers=headers, json=fault).raise_for_status()
    started = time.monotonic()
    outcome = {'scenario': name, 'expected': expected, 'action': None, 'ticks_to_action': None,
               'seconds_to_action': None, 'actuation': None, 'verification': None, 'causes': []}
    for i in range(1, max_ticks + 1):
        body = client.post('http://127.0.0.1:8000/controller/tick', headers=headers).json()
        decision = body['decision']
        causes = {r: body['assessment'][r]['cause'] for r in ('region-a', 'region-b')}
        outcome['causes'].append(causes['region-a'])
        if verbose:
            print(f"  tick {i:2d} {causes['region-a']:>20}/{causes['region-b']:<8} proposal={body['proposal']['action']:<13} "
                  f"decision={decision['action']:<13} weight={body['current_region_a_percent']:>3} {decision['reason']}")
        if body['applied'] and outcome['action'] is None:
            outcome.update(action=decision['action'], ticks_to_action=i,
                           seconds_to_action=round(time.monotonic() - started, 1), actuation=body['actuation'])
        if body['applied']:
            outcome['steps'] = outcome.get('steps', 0) + 1
        closed = [v for v in body['verification_closed'] if v['status'] in ('recovered', 'not_recovered')]
        if outcome['action'] and closed:
            outcome['verification'] = closed[-1]
            outcome['seconds_to_recovery'] = round(time.monotonic() - started, 1)
            break
        if expected == 'hold' and i >= 4:
            outcome['action'] = 'hold'
            break
        time.sleep(tick)
    outcome['final_region_a_percent'] = client.get('http://127.0.0.1:8000/state', headers=headers).json()['region_a_percent']
    outcome['ok'] = outcome['action'] == expected and (expected_weight is None or outcome['final_region_a_percent'] == expected_weight)
    client.delete('http://127.0.0.1:8001/experiments', headers=headers)
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=sorted(SCENARIOS), action='append')
    parser.add_argument('--tick', type=float, default=2.0)
    parser.add_argument('--max-ticks', type=int, default=25)
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--out', help='write outcomes as JSON')
    args = parser.parse_args(argv)
    env = {**os.environ, 'PYTHONPATH': f"{ROOT / 'services'}{os.pathsep}{ROOT}", 'LAB_TOKEN': secrets.token_hex(24),
           'LOCAL_AUTO_MITIGATION': 'true', 'CONTROLLER_COOLDOWN_SECONDS': env_default('CONTROLLER_COOLDOWN_SECONDS', '30'),
           'CONTROLLER_VERIFY_AFTER_SECONDS': env_default('CONTROLLER_VERIFY_AFTER_SECONDS', '4'),
           'CONTROLLER_VERIFY_DEADLINE_SECONDS': env_default('CONTROLLER_VERIFY_DEADLINE_SECONDS', '30'),
           'INTERNAL_REGION_A_URL': 'http://127.0.0.1:8101', 'INTERNAL_REGION_B_URL': 'http://127.0.0.1:8102',
           'EXTERNAL_PROBE_URL': 'http://127.0.0.1:8080', 'SCALE_ACTUATOR': env_default('SCALE_ACTUATOR', 'emulated')}
    processes = start(env)
    headers = {'X-Lab-Token': env['LAB_TOKEN']}
    outcomes = []
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            wait_ready(client, processes)
            for name in args.scenario or list(SCENARIOS):
                print(f'== scenario {name}')
                outcomes.append(run_scenario(client, headers, name, args.tick, args.max_ticks, not args.quiet))
                o = outcomes[-1]
                print(f"   -> {'PASS' if o['ok'] else 'FAIL'}: action={o['action']} after {o['ticks_to_action']} ticks "
                      f"({o['seconds_to_action']} s), final weight={o['final_region_a_percent']}, "
                      f"steps={o.get('steps', 0)}, verification={(o['verification'] or {}).get('status')} at {o.get('seconds_to_recovery')} s")
    finally:
        stop(processes)
    if args.out:
        Path(args.out).write_text(json.dumps(outcomes, indent=2) + '\n')
    return 0 if all(o['ok'] for o in outcomes) else 1


def env_default(name, default):
    return os.environ.get(name, default)


if __name__ == '__main__':
    raise SystemExit(main())
