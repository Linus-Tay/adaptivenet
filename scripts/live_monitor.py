#!/usr/bin/env python3
"""Supervise real AWS/RIPE/CloudFront watch + read-only UI + opt-in bounded GETs."""
from __future__ import annotations
import argparse
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def command_set(*, profile, interval, traffic, probe_interval, with_ui=False):
    cmds = [
        ([sys.executable, str(ROOT/'scripts'/'ingestion.py'), 'watch', '--profile', profile,
          '--interval', str(interval)], {}),
        ([sys.executable, str(ROOT/'scripts'/'ingestion.py'), 'serve', '--port', '8766'], {})
    ]
    if traffic:
        cmds.append(([sys.executable, str(ROOT/'scripts'/'continuous_probe.py'), '--run',
                      '--profile', profile, '--interval', str(probe_interval)], {}))
    if with_ui:
        from continuous_probe import resolve_targets
        targets = resolve_targets(profile, ROOT / '.env')
        demo_vars = {
            'DEMO_TARGET_URL': targets['cloudfront'].removesuffix('/api/catalog'),
            'DEMO_REGION_A_URL': targets['region-a'].removesuffix('/api/catalog'),
            'DEMO_REGION_B_URL': targets['region-b'].removesuffix('/api/catalog'),
        }
        cmds.extend([
            (['npm', 'run', 'dev:demo'], {'ADAPTIVENET_MODE': 'aws', **demo_vars}),
            (['npm', 'run', 'dev:dashboard'], {'ADAPTIVENET_MODE': 'aws'}),
        ])
    return cmds

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='adaptivenet-admin')
    parser.add_argument('--interval', type=int, default=120, help='AWS polling, minimum 60 seconds')
    parser.add_argument('--probe-interval', type=int, default=60, help='HTTP GET witness interval')
    parser.add_argument('--traffic', action='store_true', help='Opt in to bounded real HTTP GET traffic')
    parser.add_argument('--with-ui', action='store_true', help='Launch separate workload/operator Next.js apps')
    args = parser.parse_args(argv)
    if args.interval < 60 or not 60 <= args.probe_interval <= 3600:
        parser.error('polling and probe intervals must be at least 60 seconds')
    if args.with_ui and not (ROOT / 'node_modules').is_dir():
        parser.error('run npm ci in the repository root before --with-ui')
    env = {**os.environ, 'AWS_PROFILE': args.profile}
    processes = []
    try:
        for command, extra in command_set(profile=args.profile, interval=args.interval,
                traffic=args.traffic, probe_interval=args.probe_interval, with_ui=args.with_ui):
            print('Starting:', ' '.join(command), flush=True)
            processes.append(subprocess.Popen(command, cwd=str(ROOT), env={**env, **extra},
                                            start_new_session=(os.name != 'nt')))
        print('Observer: http://127.0.0.1:8766', flush=True)
        if args.with_ui:
            print('Real-workload test: http://127.0.0.1:3001 | Separate operator: http://127.0.0.1:3000', flush=True)
        print('Ctrl+C stops children. This supervisor never triggers AWS faults or route changes.', flush=True)
        while all(proc.poll() is None for proc in processes):
            time.sleep(.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                if os.name != 'nt':
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
        for proc in reversed(processes):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                proc.wait()

if __name__ == '__main__':
    raise SystemExit(main())
