#!/usr/bin/env python3
"""Guarded, bounded native packet-loss experiment on the existing AdaptiveNet demo EC2.

NOT an ML model, and NOT a service-stop fault. Uses AWS SSM on one fixed tagged
DEMO origin only when an operator passes explicit execute+confirmation. Schedules
independent systemd rollback BEFORE adding two narrow outbound iptables rules
for randomly dropping ICMP echo replies and port-80 HTTP responses. RIPE ping
measurement target must match this exact EC2 public IPv4 before execution.
No Terraform, SQS, Docker, ML model, or collector modification.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'region-a': {'aws_region': 'ap-southeast-1', 'instance_id': 'i-0039feaedbd2b3266', 'ripe_id': 234316056},
    'region-b': {'aws_region': 'us-east-1', 'instance_id': 'i-0ea5125bcb2d201ec', 'ripe_id': 234317384},
}
OUT = ROOT / 'data' / 'ml-results' / 'native' / 'authorized-network-trials'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _aws(arguments: list[str], profile: str, *, write: bool = False) -> dict:
    args = ['aws', *arguments, '--profile', profile, '--output', 'json', '--no-cli-pager']
    p = subprocess.run(args, capture_output=True, text=True, check=False, timeout=55 if write else 35)
    if p.returncode:
        raise RuntimeError(f'AWS {"write" if write else "read"} refused/failed: {p.stderr.strip()[:600]}')
    try:
        result = json.loads(p.stdout)
    except ValueError as exc:
        raise RuntimeError('Malformed AWS JSON response') from exc
    if not isinstance(result, dict):
        raise RuntimeError('AWS response must be a JSON object')
    return result


def _atlas_metadata(msm_id: int) -> dict:
    url = f'https://atlas.ripe.net/api/v2/measurements/{msm_id}/'
    req = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AdaptiveNet-AuthorizedNetworkPreflight/1'})
    with urlopen(req, timeout=20) as response:
        data = json.load(response)
    if not isinstance(data, dict) or data.get('id') != msm_id or data.get('type') != 'ping':
        raise RuntimeError('RIPE Atlas measurement ID/type mismatch')
    return data


def _target_matches_ip(target: str, ip: str) -> bool:
    if not target or not ip:
        return False
    try:
        return str(ipaddress.ip_address(target)) == ip
    except ValueError:
        try:
            return ip in {r[4][0] for r in socket.getaddrinfo(target, None, family=socket.AF_INET)}
        except OSError:
            return False


def _check_preflight(profile: str, expected_account: str, region: str, ripe_measurement_id: int | None = None) -> dict:
    if not re.fullmatch(r'\d{12}', expected_account):
        raise RuntimeError('--expected-account must contain exactly 12 digits')
    if region not in TARGETS:
        raise RuntimeError('Unknown region')
    cfg = TARGETS[region]
    active_ripe_id = cfg['ripe_id'] if ripe_measurement_id is None else ripe_measurement_id
    if not isinstance(active_ripe_id, int) or isinstance(active_ripe_id, bool) or active_ripe_id <= 0:
        raise RuntimeError('RIPE measurement ID must be a positive integer')
    identity = _aws(['sts', 'get-caller-identity'], profile)
    account = identity.get('Account')
    if account != expected_account:
        raise RuntimeError('AWS account does not match --expected-account')
    resp = _aws(['ec2', 'describe-instances', '--region', cfg['aws_region'],
                 '--instance-ids', cfg['instance_id']], profile)
    try:
        inst = resp['Reservations'][0]['Instances'][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError('Expected demo EC2 not found') from exc
    tags = {x.get('Key'): x.get('Value') for x in inst.get('Tags', []) if isinstance(x, dict)}
    if (inst.get('InstanceId') != cfg['instance_id'] or
        inst.get('State', {}).get('Name') != 'running' or
        tags.get('Project') != 'AdaptiveNet' or
        tags.get('Environment') != 'demo'):
        raise RuntimeError('Refusing: EC2 missing the required AdaptiveNet/demo tags or is not running')
    public_ip = inst.get('PublicIpAddress')
    if not public_ip:
        raise RuntimeError('EC2 public IPv4 missing')
    try:
        if not ipaddress.ip_address(public_ip).is_global:
            raise RuntimeError('EC2 public IPv4 is not globally routable')
    except ValueError as exc:
        raise RuntimeError('Invalid EC2 public IPv4') from exc
    ssm = _aws(['ssm', 'describe-instance-information', '--region', cfg['aws_region'],
                '--filters', 'Key=InstanceIds,Values='+cfg['instance_id']], profile)
    if not any(x.get('InstanceId') == cfg['instance_id'] and x.get('PingStatus') == 'Online'
               for x in ssm.get('InstanceInformationList', [])):
        raise RuntimeError('SSM agent is not online; cannot guarantee command delivery')
    meta = _atlas_metadata(active_ripe_id)
    target = str(meta.get('target') or '')
    if not _target_matches_ip(target, public_ip):
        raise RuntimeError(f'RIPE target {target!r} does not resolve to current EC2 public IP {public_ip}. '
                           'Do NOT inject faults until the RIPE measurement actually observes this demo target.')
    state = meta.get('status') or {}
    name = str(state.get('name', '') if isinstance(state, dict) else state)
    if name.lower() not in ('ongoing','active'):
        raise RuntimeError(f'RIPE measurement is not currently ongoing: {name!r}')
    interval = meta.get('interval')
    if not isinstance(interval, int) or interval <= 0:
        raise RuntimeError('RIPE interval not reported; cannot plan a meaningful experiment')
    return {'utc':_now(),'aws_account':account,'region':region,'aws_region':cfg['aws_region'],
            'instance_id':cfg['instance_id'],'instance_public_ip':public_ip,
            'ripe_measurement_id':active_ripe_id,'ripe_target':target,
            'ripe_status':name,'ripe_interval_seconds':interval,
            'min_suggested_impairment_seconds':max(900,4*interval)}


def _fault_script(runid: str, duration: int, probability: int) -> str:
    if not re.fullmatch(r'[0-9a-f]{12}', runid):
        raise ValueError('Invalid run id')
    if not 900 <= duration <= 2400 or not 10 <= probability <= 35:
        raise ValueError('Duration 900..2400 and packet-loss percentage 10..35 required')
    tag = f'adaptivenet-net-{runid}'
    restore = f'/var/tmp/{tag}-restore.sh'
    # Keep AWS SSM 443/SSM telemetry/networking unaffected: only remote ICMP echo
    # replies and the demo application's HTTP responses on TCP source port 80.
    # Do not modify route tables, qdiscs, security groups or the application service.
    script = f'''set -eu
[ "$(id -u)" -eq 0 ] || {{ echo 'STOP: SSM shell is not root'; exit 21; }}
command -v iptables >/dev/null || {{ echo 'STOP: iptables unavailable'; exit 22; }}
command -v systemd-run >/dev/null || {{ echo 'STOP: systemd-run unavailable'; exit 23; }}
command -v bash >/dev/null || {{ echo 'STOP: bash unavailable'; exit 24; }}
iptables -m statistic -h >/dev/null 2>&1 || {{ echo 'STOP: iptables statistic module missing'; exit 25; }}
iptables -m comment -h >/dev/null 2>&1 || {{ echo 'STOP: iptables comment module missing'; exit 26; }}
if iptables -S OUTPUT | grep -q 'adaptivenet-net-'; then echo 'STOP: existing AdaptiveNet experiment firewall rule'; exit 27; fi
restore='{restore}'
cat > "$restore" <<'RESTORE_SCRIPT'
#!/bin/bash
set -euo pipefail
tag='{tag}'
rule_icmp=( -p icmp --icmp-type echo-reply -m statistic --mode random --probability {probability/100:.2f} -m comment --comment "$tag" -j DROP )
rule_http=( -p tcp --sport 80 -m statistic --mode random --probability {probability/100:.2f} -m comment --comment "$tag" -j DROP )
if iptables -w -C OUTPUT "${{rule_icmp[@]}}" 2>/dev/null; then iptables -w -D OUTPUT "${{rule_icmp[@]}}"; fi
if iptables -w -C OUTPUT "${{rule_http[@]}}" 2>/dev/null; then iptables -w -D OUTPUT "${{rule_http[@]}}"; fi
echo "NETWORK_ROLLBACK_DONE UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ) TAG=$tag"
rm -f -- "$0"
RESTORE_SCRIPT
chmod 700 "$restore"
if ! systemd-run --quiet --unit='{tag}-restore' --on-active={duration}s /bin/bash "$restore"; then
  rm -f "$restore"; echo 'STOP: cannot schedule independent automatic rollback'; exit 28
fi
# Both rules are bounded to only incoming remote ping replies and port-80 demo
# HTTP response packets; AWS SSM HTTPS 443 control traffic is not targeted.
if ! iptables -w -I OUTPUT 1 -p icmp --icmp-type echo-reply -m statistic --mode random --probability {probability/100:.2f} -m comment --comment '{tag}' -j DROP; then
  /bin/bash "$restore" || true; echo 'STOP: ICMP rule failed'; exit 30
fi
if ! iptables -w -I OUTPUT 1 -p tcp --sport 80 -m statistic --mode random --probability {probability/100:.2f} -m comment --comment '{tag}' -j DROP; then
  /bin/bash "$restore" || true; echo 'STOP: HTTP rule failed'; exit 31
fi
echo "NETWORK_EXPERIMENT_STARTED UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ) TAG={tag} LOSS_PCT={probability} DURATION_SECONDS={duration} ROLLBACK_ARMED=YES"
iptables -S OUTPUT | grep -F '{tag}'
'''
    return script


def _write_json_once(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def _run(args, preflight: dict) -> int:
    if not args.execute or args.confirm != 'NETWORK-IMPAIR-' + args.region.upper():
        raise RuntimeError('Refusing write: requires --execute --confirm NETWORK-IMPAIR-' + args.region.upper())
    if args.duration < preflight['min_suggested_impairment_seconds']:
        raise RuntimeError('Duration is shorter than 4 RIPE collection intervals; increase --duration')
    runid = uuid.uuid4().hex[:12]
    shell = _fault_script(runid, args.duration, args.loss_percent)
    cfg = TARGETS[args.region]
    print('Authorized temporary DEMO NETWORK FAULT:', args.region, preflight['instance_id'])
    print('Original RIPE target matches current EC2 IP:', preflight['ripe_target'])
    print('Duration:', args.duration, 'seconds; packet loss:', args.loss_percent, '%')
    print('Automatic rollback is armed in remote script BEFORE any packet-loss rule.')
    response = _aws(['ssm', 'send-command', '--region', cfg['aws_region'],
                     '--instance-ids', cfg['instance_id'], '--document-name', 'AWS-RunShellScript',
                     '--comment', f'Approved temporary network-loss evidence experiment {runid}; auto-rollback first',
                     '--timeout-seconds', '180',
                     '--parameters', json.dumps({'commands':[shell]})], args.profile, write=True)
    command_id = response.get('Command', {}).get('CommandId')
    if not isinstance(command_id,str) or not command_id:
        raise RuntimeError('SSM accepted response but no command ID. Inspect AWS SSM command history.')
    out = Path(args.out_dir) / f'network-trial-{runid}.json'
    record = {'schema':'adaptivenet.authorized_native_network_experiment.v1',
              'requested_utc':_now(), 'run_id':runid, 'region':args.region,
              'profile_name':args.profile,'preflight':preflight,
              'duration_seconds':args.duration,'configured_packet_loss_percent':args.loss_percent,
              'ssm_command_id':command_id, 'ssm_submitted':True,
              'actual_impairment_effect_verified':False, 'human_label_reviewed':False,
              'model_training_label':None,
              'note':'SSM submission is NOT proof of network effect. Confirm command output + RIPE + HTTP.'}
    _write_json_once(out, record)
    print('SSM command submitted:', command_id)
    print('Experiment request evidence:', out)
    print('Confirm the SSM execution output and independent RIPE/HTTP effects before labeling.')
    return 0


def _check(args) -> int:
    if not args.trial or not Path(args.trial).is_file():
        raise RuntimeError('--trial must identify an existing local trial JSON file')
    path = Path(args.trial)
    trial = json.loads(path.read_text(encoding='utf-8'))
    if trial.get('schema') != 'adaptivenet.authorized_native_network_experiment.v1':
        raise RuntimeError('Wrong trial schema')
    pf = trial.get('preflight') or {}
    if pf.get('aws_account') != args.expected_account:
        raise RuntimeError('Account mismatch in local trial record')
    cid = trial.get('ssm_command_id')
    if not isinstance(cid, str) or not re.fullmatch(r'[a-f0-9-]{20,80}', cid):
        raise RuntimeError('SSM command ID missing or malformed')
    identity = _aws(['sts', 'get-caller-identity'], args.profile)
    if identity.get('Account') != args.expected_account:
        raise RuntimeError('Refusing to check a trial under a different account')
    region = trial['region']
    cfg = TARGETS[region]
    try:
        result = _aws(['ssm', 'get-command-invocation', '--region', cfg['aws_region'],
                       '--instance-id', cfg['instance_id'], '--command-id',cid], args.profile)
    except RuntimeError as exc:
        print('SSM invocation not yet retrievable:',exc)
        return 3
    status = result.get('Status')
    output = str(result.get('StandardOutputContent') or '')
    errout = str(result.get('StandardErrorContent') or '')
    armed = 'NETWORK_EXPERIMENT_STARTED ' in output and 'ROLLBACK_ARMED=YES' in output
    print('SSM status:',status,'| remote rollback+fault command confirmed:',armed)
    print('SSM stdout:',output[-2500:])
    if errout:
        print('SSM stderr:',errout[-1200:])
    # Local record proves remote command execution only, never independent effect.
    trial['remote_check_utc'] = _now()
    trial['remote_command_status'] = status
    trial['remote_rollback_armed_and_rules_added'] = bool(status=='Success' and armed)
    trial['remote_stdout_excerpt'] = output[-4000:]
    trial['remote_stderr_excerpt'] = errout[-1000:]
    # Atomic rewrite of this experiment's local metadata only.
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(trial, indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)
    if status!='Success' or not armed:
        print('NOT PROVEN: check remote command failure and independent source evidence')
        return 3
    print('REMOTE FAULT COMMAND EXECUTED; still need independent HTTP+RIPE measurements.')
    return 0


def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=('preflight','run','check'))
    ap.add_argument('--profile', default='adaptivenet-ml')
    ap.add_argument('--region', choices=tuple(TARGETS), default='region-b')
    ap.add_argument('--ripe-measurement-id', type=int, help='Explicit replacement ongoing ping ID; must target the same approved EC2 public IPv4')
    ap.add_argument('--expected-account', required=True, help='12-digit AWS account ID previously checked by operator')
    ap.add_argument('--duration', type=int, default=900, help='900-2400 s; must span 4 RIPE intervals')
    ap.add_argument('--loss-percent', type=int, default=25, help='10..35 percent; narrow echo-reply/port-80 response loss')
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--confirm', default='')
    ap.add_argument('--out-dir', type=Path, default=OUT)
    ap.add_argument('--trial', type=Path, help='existing local trial record for check action')
    args=ap.parse_args(argv)
    try:
        if args.action=='check':
            return _check(args)
        preflight=_check_preflight(args.profile,args.expected_account,args.region,args.ripe_measurement_id)
        print(json.dumps(preflight,indent=2))
        if args.action=='preflight':
            print('READ-ONLY PREFLIGHT PASSED; NO AWS WRITE. Test will run only with explicit run --execute --confirm.')
            return 0
        return _run(args,preflight)
    except (RuntimeError,ValueError,OSError,subprocess.TimeoutExpired) as exc:
        print('STOP:',exc,file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
