#!/usr/bin/env python3
"""Prepare minimal immutable shadow inference container context and evidence report.

No AWS, Docker, kubectl, infrastructure mutation, data edits, or training.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
GROUPS = ('aws_only','aws_ripe','aws_ripe_http')
REPORT_PATH = ROOT/'data/ml-results/native/step4-network-evaluation/step4-evaluation.json'
MODEL_DIR = ROOT/'data/ml-results/native/step3-network-models'
CONTEXT = ROOT/'data/ml-results/native/step7-container-context'
OUT = ROOT/'data/ml-results/native/step7-shadow-handoff'
SOURCE = Path(__file__).resolve().parent


def sha(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def audit_models(root:Path=ROOT)->dict:
    report_file=root/'data/ml-results/native/step4-network-evaluation/step4-evaluation.json'
    report=json.loads(report_file.read_text(encoding='utf-8-sig'))
    if report.get('evaluation_status')!='completed_with_explicit_heldout_healthy_limitation':
        raise ValueError('Step 4 frozen evaluation report invalid')
    records=report.get('models',{})
    if set(records)!=set(GROUPS):
        raise ValueError('Need exactly three Step 4 models')
    result={}
    for group in GROUPS:
        record=records[group]
        model_file=Path(str(record['model_file']).replace('\\','/'))
        path=root/'data/ml-results/native/step3-network-models'/model_file.name
        if path.parent.resolve()!=(root/'data/ml-results/native/step3-network-models').resolve() or not path.is_file():
            raise ValueError(f'{group}: model artifact missing/untrusted path')
        digest=sha(path)
        if digest!=record['model_sha256']:
            raise ValueError(f'{group}: original Step 4 model hash changed')
        result[group]={'model_id':record['model_id'],'filename':path.name,'sha256':digest,'size_bytes':path.stat().st_size}
    return result


def safe_copy_py(source:Path, dest:Path):
    if not source.is_dir():
        raise FileNotFoundError(f'Missing existing repository source {source}')
    count=0
    for path in source.rglob('*.py'):
        if any(part in ('__pycache__','tests','.venv','venv','node_modules') for part in path.parts):
            continue
        target=dest/path.relative_to(source)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
        count+=1
    if not count:
        raise ValueError(f'No Python sources found in {source}')
    return count


def prepare_context(models:dict, root:Path=ROOT, context:Path=CONTEXT)->dict:
    # Fixed allowlist only. Never send data archives, .env, credentials, git history,
    # unreviewed data, or whole repository contents into the image build context.
    context.mkdir(parents=True,exist_ok=True)
    if any(context.iterdir()):
        raise ValueError(f'Container context already exists: {context}; move it aside to rebuild safely')
    counts={}
    counts['ml_python_files']=safe_copy_py(root/'ml',context/'ml')
    counts['services_python_files']=safe_copy_py(root/'services/adaptivenet',context/'services/adaptivenet')
    (context/'step5_finish').mkdir(parents=True)
    shutil.copyfile(root/'step5_finish/finalize_step5.py',context/'step5_finish/finalize_step5.py')
    (context/'step7_finish').mkdir(parents=True)
    shutil.copyfile(root/'step7_finish/service.py',context/'step7_finish/service.py')
    (context/'data/ml-results/native/step4-network-evaluation').mkdir(parents=True)
    shutil.copyfile(root/'data/ml-results/native/step4-network-evaluation/step4-evaluation.json',
                    context/'data/ml-results/native/step4-network-evaluation/step4-evaluation.json')
    dest=context/'data/ml-results/native/step3-network-models'
    dest.mkdir(parents=True)
    for group, m in models.items():
        from_path=root/'data/ml-results/native/step3-network-models'/m['filename']
        shutil.copyfile(from_path,dest/m['filename'])
        if sha(dest/m['filename'])!=m['sha256']:
            raise ValueError(f'Copied model modified: {group}')
    for name in ('Dockerfile','requirements-service.txt'):
        shutil.copyfile(root/'step7_finish'/name,context/name)
    return counts


def run(out_root:Path=OUT, context:Path=CONTEXT):
    models=audit_models(ROOT)
    lock=ROOT/'ml/team_starter/requirements-ml-inference.lock'
    if not lock.is_file():
        raise FileNotFoundError('Missing original inference dependency lock')
    import sklearn
    ml_lock=lock.read_text(encoding='utf-8')
    version=sklearn.__version__
    if f'scikit-learn=={version}' not in ml_lock:
        raise RuntimeError('Installed scikit-learn differs from original ML inference lock')
    counts=prepare_context(models,ROOT,context)
    out_root.mkdir(parents=True,exist_ok=True)
    report={
        'phase':'step7_model_service_handoff','status':'STEP7_ML_SERVICE_PACKAGED_EKS_NOT_YET_VERIFIED',
        'created_at_utc':datetime.now(timezone.utc).isoformat(),
        'model_count':3,'models':models,'scikit_learn_version':version,
        'python_version':sys.version.split()[0],
        'training':False,'model_artifact_hashes_verified':True,
        'image_context_created':str(context.relative_to(ROOT)),
        'source_file_counts':counts,
        'shadow_only':True,'automatic_mitigation':False,'aws_mutations':False,
        'local_service_tested':False,'docker_image_build_verified':False,
        'eks_deployment_verified':False,'secure_telemetry_transport_verified':False,
        'security_controls':{'token_required':True,'failing_closed':True,'max_snapshot_age_seconds':180,
                             'no_cloud_write_permissions_in_service':True,
                             'no_automatic_actuation':True,
                             'k8s_service_internal_only':True},
        'remaining_checks':['run local Step 7 inference integration smoke test',
                            'build and inspect Docker image',
                            'verify EKS Deployment and ClusterIP after teammate deployment',
                            'verify internal authenticated transport and NetworkPolicy in-cluster'],
    }
    (out_root/'STEP7_REPORT.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    lines=['# AdaptiveNet — Step 7 ML inference handoff','',
           f"**Status:** {report['status']}",
           '', '## Frozen model artifact manifest','',
           '| Group | Model ID | SHA-256 |', '|---|---|---|',
           *[f"| {g} | `{m['model_id']}` | `{m['sha256']}` |" for g,m in models.items()],
           '',f"Python version: `{report['python_version']}`. scikit-learn: `{version}`.",
           'In-image ML inference dependencies come from step7_finish/requirements-service.txt.',
           '', '## Source and security','',
           'The container build context was generated from allowlisted source files and the three pinned model artifacts only.',
           'Service: read-only `POST /v1/infer` with mandatory bearer authentication; liveness `/healthz`, readiness `/readyz`.',
           'Every proposal is passed to the independent reviewer and must remain `effective_action=hold`, `executed=false`, `approved_for_execution=false`.',
           '', '## Still needs deployment verification','',
           '- Image has not been built or tested in Docker by this local preparation command.',
           '- No ECR push, EKS rollout or in-cluster service test has happened.',
           '- Shared token secret and service-level network controls must be installed/approved by infrastructure teammate.',
           '- Full external AWS metrics and live mitigation remain out of scope.',
           '', '## Run next','',
           '```powershell',
           'docker build -t adaptivenet-shadow-ml:step7 data/ml-results/native/step7-container-context',
           '```',
           'Build only from the allowlisted generated context. Do **not** run `docker build .` (which could package `.env`, raw data and private files).',
           'See `docs/ml/STEP7_SHADOW_SERVICE.md` for local smoke, security and EKS handoff instructions.',
           '']
    (out_root/'STEP7_REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('STEP 7 STATUS:',report['status'])
    print('FROZEN MODELS: 3/3 SHA256 PASS; scikit-learn:',version)
    print('ALLOWLISTED DOCKER BUILD CONTEXT:',context)
    print('MODEL VERSION REPORT:',out_root/'STEP7_REPORT.md')
    print('NEXT: local integration and Docker/EKS verification; no infrastructure changes performed')
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check-only',action='store_true',help='Verify model versions and packages without preparing a Docker context')
    args=ap.parse_args()
    if args.check_only:
        models=audit_models()
        print('STEP7 MODEL HASHES PASS:',','.join(models));return 0
    run();return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(f'STEP 7 FAIL-CLOSED: {type(exc).__name__}: {exc}',file=sys.stderr)
        sys.exit(2)
