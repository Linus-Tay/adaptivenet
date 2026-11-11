#!/usr/bin/env python3
"""Read-only EKS deployment visibility gate. Never creates or updates resources.

Does NOT certify authentic live telemetry delivery, TLS/mTLS, or application model quality.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'data/ml-results/native/step7-shadow-handoff'


def kubectl(context:str,resource:str):
    args=['kubectl','--context',context,'-n','adaptivenet','get',resource,'-o','json']
    try:
        p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,text=True,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'kubectl unavailable: {type(exc).__name__}') from exc
    if p.returncode:
        # Avoid printing underlying authentication errors/secrets to shared reports.
        raise RuntimeError(f'kubectl read-only get {resource} failed (exit {p.returncode})')
    try:return json.loads(p.stdout)
    except ValueError as exc:raise RuntimeError('kubectl returned invalid JSON') from exc


def check_resources(dep,service,np,eps):
    failures=[]
    template=dep.get('spec',{}).get('template',{}).get('spec',{})
    containers=template.get('containers',[])
    c=next((r for r in containers if r.get('name')=='shadow-inference'),{})
    if dep.get('status',{}).get('readyReplicas',0)<1:
        failures.append('Deployment has no ready replicas')
    if not c.get('image','').count('@sha256:'):
        failures.append('Image is not digest pinned')
    if template.get('automountServiceAccountToken') is not False:
        failures.append('Kubernetes API token mounting not disabled')
    sec=c.get('securityContext',{})
    if sec.get('readOnlyRootFilesystem') is not True or sec.get('allowPrivilegeEscalation') is not False:
        failures.append('Container security context incomplete')
    env=c.get('env',[])
    token=next((e for e in env if e.get('name')=='ADAPTIVENET_SHADOW_TOKEN'),{})
    if token.get('valueFrom',{}).get('secretKeyRef',{}).get('name') != 'adaptivenet-shadow-ml-token':
        failures.append('Token not sourced from expected Kubernetes Secret')
    if service.get('spec',{}).get('type') != 'ClusterIP':
        failures.append('Inference service not ClusterIP')
    types=np.get('spec',{}).get('policyTypes',[])
    if not all(x in types for x in ('Ingress','Egress')):
        failures.append('Ingress and Egress NetworkPolicy coverage missing')
    if np.get('spec',{}).get('egress',[]) != []:
        failures.append('Unexpected egress permissions')
    if not eps.get('subsets'):
        failures.append('Service has no ready endpoint addresses')
    return failures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context',required=True,help='Explicit approved kubectl context; never guess')
    args=parser.parse_args()
    reads={
        'deployment':kubectl(args.context,'deployment/adaptivenet-shadow-ml'),
        'service':kubectl(args.context,'service/adaptivenet-shadow-ml'),
        'networkpolicy':kubectl(args.context,'networkpolicy/adaptivenet-shadow-ml-ingress'),
        'endpoints':kubectl(args.context,'endpoints/adaptivenet-shadow-ml'),
    }
    issues=check_resources(reads['deployment'],reads['service'],reads['networkpolicy'],reads['endpoints'])
    status='EKS_RESOURCE_READ_ONLY_CHECK_PASSED' if not issues else 'EKS_RESOURCE_READ_ONLY_CHECK_INCOMPLETE'
    report={'status':status,'cluster_context':args.context,'issues':issues,
            'image_digest_pinned':not any('digest' in x for x in issues),
            'e2e_telemetry_delivery_verified':False,'tls_or_mtls_verified':False,
            'controller_actuation_authorized':False,
            'notes':['Only read-only Kubernetes object metadata examined',
                     'End-to-end authenticated inference requires a separate in-cluster test by infrastructure owner']}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'STEP7_EKS_READONLY_CHECK.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('STEP 7 EKS READ-ONLY STATUS:',status)
    if issues:print('ISSUES:', '; '.join(issues))
    print('NO CLUSTER MUTATIONS PERFORMED; IN-CLUSTER AUTHENTICATED DELIVERY UNVERIFIED')
    return 0 if not issues else 2


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print('STEP 7 EKS READ-ONLY CHECK INCOMPLETE:',type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(2)
