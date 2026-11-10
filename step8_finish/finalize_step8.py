#!/usr/bin/env python3
"""AdaptiveNet ML Step 8: read-only verification, report and PR-scope inventory.

Never trains, re-labels, writes AWS, stages, commits, pushes or deploys.
All private evidence and logs remain under data/ml-results/native/step8-finalization.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import secrets
import time
import urllib.request
import urllib.error
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'data/ml-results/native/step8-finalization'
DOCS = ROOT / 'docs/ml'
STEP6 = ROOT / 'data/ml-results/native/step6-network-validation/STEP6_REPORT.json'
STEP7 = ROOT / 'data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json'
GROUPS = ('aws_only', 'aws_ripe', 'aws_ripe_http')
REPORT = DOCS / 'NETWORK_DEGRADATION_FINAL_EVALUATION.md'
HANDOFF = DOCS / 'STEP8_ML_HANDOFF.md'
ALLOWED_PREFIXES = ('ml/team_starter/', 'step2_finish/', 'step3_finish/', 'step4_finish/',
                    'step5_finish/', 'step6_finish/', 'step7_finish/', 'step8_finish/',
                    'docs/ml/')
ALLOWED_EXACT = ('ml/predictor.py',)
ALLOWED_SCRIPT_RE = re.compile(r'^scripts/(?:step\d[\w-]*|ml_[\w-]*)(?:\.py|\.ps1)$')
BLOCKED_PARTS = frozenset(('data', 'node_modules', '.git', '.venv', 'venv', '__pycache__', '.aws', 'secrets', 'private'))
BLOCKED_SUFFIXES = frozenset(('.pkl', '.joblib', '.db', '.sqlite', '.sqlite3', '.jsonl', '.csv',
                              '.zip', '.7z', '.pem', '.key', '.p12', '.pfx', '.env', '.uf2', '.log', '.parquet'))
SECRET_PATTERNS = [
    re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(r'(?i)(?:aws_secret_access_key|aws_session_token)\s*[:=]\s*[^\s#]{10,}'),
    re.compile(r'(?i)\b(?:api_key|password|secret_token)\s*[:=]\s*[\"\']?[A-Za-z0-9_+/-]{24,}'),
]


def require(condition, explanation):
    if not condition:
        raise ValueError(explanation)


def load(path):
    require(path.is_file(), f'Essential evidence missing: {path}')
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    require(isinstance(result, dict), f'Invalid JSON object: {path}')
    return result


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def clean_rel_path(raw):
    raw = str(raw).replace('\\','/')
    p = Path(raw)
    require(not p.is_absolute() and '..' not in p.parts, 'Path traversal/absolute path in frozen model manifest')
    return p


def collect_evidence(root=ROOT):
    s6 = load(root / STEP6.relative_to(ROOT))
    s7 = load(root / STEP7.relative_to(ROOT))
    require(s6.get('status') == 'STEP6_ANALYSIS_COMPLETED_WITH_UNESTIMABLE_GROUND_TRUTH_METRICS',
            'Step 6 validation status mismatched or changed')
    require(s6.get('automatic_mitigation_enabled') is False and s6.get('global_healthy_false_alarm_rate') is None,
            'Step 6 safety or false-alarm limit mismatched')
    require(s6.get('heldout',{}).get('paired_identical_heldout_snapshot_keys') is True,
            'Three models lack identical held-out keys')
    source=s6.get('source_integrity') or {}
    require(source.get('episode_count',0)>=10 and source.get('dataset_rows',0)>=150,
            'Frozen binary evidence unexpectedly smaller; refresh report before submission')
    require(source.get('episode_counts',{}).get('network_degradation',0)>=6,
            'Verified network incidents missing')
    require(source.get('episode_counts',{}).get('healthy',0)>=4,
            'Reviewed healthy controls missing from dataset')
    path=root/'data/ml-results/native/step2-network-approved/verified_network_dataset.json'
    require(path.is_file() and sha256(path)==source.get('dataset_sha256'),
            'Original reviewed binary dataset changed after Step 6')
    manifest=source.get('model_artifacts') or {}
    require(set(manifest)==set(GROUPS), 'Three frozen model artifacts not present')
    models=s6.get('heldout',{}).get('models') or {}
    require(set(models)==set(GROUPS), 'Three held-out models missing')
    for g in GROUPS:
        ent=manifest[g]
        p=root/clean_rel_path(ent['artifact_path'])
        require(p.is_file() and sha256(p)==ent['sha256'],f'{g}: frozen model hash mismatch')
        require(models[g]['test_rows']==source['test_rows'],f'{g}: held-out snapshot count mismatch')
        require(models[g]['verified_healthy_rows']==0, 'Nonzero healthy holdout: evaluate new global false-alarm rate properly before using this limited report')
    pro=s6.get('prospective') or {}
    require(pro.get('unique_snapshots',0)>=6 and pro.get('span_seconds',0)>=300 and
            pro.get('safe_hold_verified') is True and pro.get('ground_truth_labels') is False,
            'Prospective study did not pass reviewed shadow conditions')
    require(s7.get('status')=='STEP7_LOCAL_AUTHENTICATED_SERVICE_VERIFIED_SHADOW_ONLY' and
            s7.get('authentication_rejects_unauthorized') is True and
            s7.get('all_three_model_groups_returned') is True and
            s7.get('independent_reviews_valid') is True and s7.get('no_execution') is True,
            'Step 7 local authenticated smoke or safety review missing')
    require(set(s7.get('model_statuses',{}))==set(GROUPS),'Step 7 model groups incomplete')
    return s6,s7


def shell(args, *, root=ROOT, timeout=240, env=None):
    try:
        r=subprocess.run(args,cwd=root,text=True,encoding='utf-8',errors='replace',
                         stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                         timeout=timeout,check=False,env=env)
        return r.returncode,r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return 124, f'{type(e).__name__}: {str(e)}'


def git_inventory(root=ROOT):
    code,branch=shell(['git','branch','--show-current'],root=root,timeout=15)
    require(code==0,'Not an accessible Git repository')
    branch=branch.strip()
    code,raw=shell(['git','status','--porcelain=v1','--untracked-files=all'],root=root,timeout=30)
    require(code==0,'Cannot inspect Git changed paths')
    candidates=[]; blocked=[]; unrelated=[]
    for line in raw.splitlines():
        if len(line)<4:continue
        path=line[3:].replace('\\','/')
        # Git porcelain quoted file names are considered unsafe to stage automatically.
        if path.startswith('"') or ' -> ' in path:
            blocked.append({'path':path,'reason':'rename/quoted path requires manual inspection'})
            continue
        if safe_for_pr(path):
            full=root/path
            if full.is_file():
                try:
                    content=full.read_text(encoding='utf-8')
                    if any(p.search(content) for p in SECRET_PATTERNS):
                        blocked.append({'path':path,'reason':'possible secret pattern; manual review required'})
                    else:
                        candidates.append(path)
                except (OSError, UnicodeError):
                    blocked.append({'path':path,'reason':'file not readable as UTF-8 source'})
            else:
                unrelated.append(path)
        elif forbidden_path(path):
            blocked.append({'path':path,'reason':'private data, credential, model binary or unsupported format'})
        else:
            unrelated.append(path)
    code,diff=shell(['git','diff','--check'],root=root,timeout=20)
    code2,staged=shell(['git','diff','--cached','--check'],root=root,timeout=20)
    code3,staged_paths=shell(['git','diff','--cached','--name-only'],root=root,timeout=20)
    staged_risky=[p for p in staged_paths.splitlines() if not safe_for_pr(p)] if code3==0 else ['cannot read staged paths']
    return {'branch':branch,'candidate_files':sorted(set(candidates)),
            'blocked_files':blocked,'unrelated_files':unrelated,
            'git_diff_check_passed':code==0,'git_staged_diff_check_passed':code2==0,
            'staged_risky_paths':staged_risky,'diff_messages':(diff+'\n'+staged).strip()[:1500]}


def forbidden_path(path):
    parts=path.replace('\\','/').lower().split('/')
    basename=parts[-1]
    return (any(p in BLOCKED_PARTS for p in parts) or basename.startswith('.env')
            or Path(basename).suffix in BLOCKED_SUFFIXES or 'credentials' in basename
            or 'secret' in basename or 'token' in basename or 'private' in basename)


def safe_for_pr(path):
    path=path.replace('\\','/')
    if forbidden_path(path):return False
    if Path(path).suffix.lower() not in ('.py','.md','.txt','.yaml','.yml','.ps1','.toml','.lock','.json'):
        return False
    return (path.startswith(ALLOWED_PREFIXES) or path in ALLOWED_EXACT or bool(ALLOWED_SCRIPT_RE.fullmatch(path)))


def docker_image_status(root=ROOT):
    code,out=shell(['docker','image','inspect','adaptivenet-shadow-ml:step7','--format','{{.Id}}'],root=root,timeout=30)
    digest=out.strip()
    if code==0 and re.fullmatch(r'sha256:[0-9a-f]{64}',digest):
        return {'status':'LOCAL_DOCKER_IMAGE_PRESENT','image_id':digest}
    return {'status':'LOCAL_DOCKER_IMAGE_NOT_CHECKED','image_id':None,'reason':'Docker API not reachable or image missing; previous user terminal showed successful build'}


def docker_container_test(image_status, root=ROOT):
    """Ephemeral localhost-only container; never contacts AWS and always stops container."""
    if not image_status.get('image_id'):
        return {'status':'NOT_VERIFIED','reason':'Docker image not available'}
    token=secrets.token_urlsafe(40)
    cid=None
    try:
        code,out=shell(['docker','run','--rm','-d','-p','127.0.0.1::8090',
                        '-e',f'ADAPTIVENET_SHADOW_TOKEN={token}',
                        'adaptivenet-shadow-ml:step7'],root=root,timeout=60)
        require(code==0, 'Local Docker container failed to start: '+out[:300])
        cid=out.strip().splitlines()[-1].strip()
        require(bool(re.fullmatch(r'[a-f0-9]{12,64}',cid)), 'Invalid ephemeral container ID')
        code,port=shell(['docker','port',cid,'8090/tcp'],root=root,timeout=15)
        require(code==0, 'Cannot get mapped localhost-only ephemeral port')
        m=re.search(r'127\.0\.0\.1:(\d+)',port)
        require(m is not None,'Docker did not map a localhost-only port')
        base='http://127.0.0.1:'+m.group(1)
        deadline=time.monotonic()+35
        ready=False
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen(base+'/readyz',timeout=2) as resp:
                    obj=json.load(resp)
                    if resp.status==200 and obj.get('status')=='ready' and obj.get('shadow_only') is True:
                        ready=True;break
            except (OSError,ValueError):time.sleep(1)
        require(ready,'Local Docker /readyz did not become ready')
        req=urllib.request.Request(base+'/v1/infer',data=b'{}',headers={'Content-Type':'application/json'},method='POST')
        try:
            urllib.request.urlopen(req,timeout=5)
            raise ValueError('Unauthenticated inference was not rejected')
        except urllib.error.HTTPError as e:
            require(e.code==401, 'Unauthenticated inference had unexpected HTTP status')
        return {'status':'EPHEMERAL_DOCKER_SERVICE_VERIFIED','readiness_valid':True,
                'unauthenticated_inference_rejected':True,'container_stopped_in_finally':True}
    except Exception as exc:
        return {'status':'NOT_VERIFIED','reason':type(exc).__name__+': '+str(exc)[:400]}
    finally:
        if cid:
            shell(['docker','stop','-t','2',cid],root=root,timeout=15)


def pct(v):return 'N/A' if v is None else f'{100*v:.1f}%'


def report_md(s6,s7,git,tests,docker,container):
    source=s6['source_integrity']; pro=s6['prospective']; models=s6['heldout']['models']
    rows=[
        '# AdaptiveNet — Final network-degradation ML evaluation (Step 8)', '',
        '**Scope:** two-class current-condition network-degradation study; November 2026.',
        '**Status:** completed offline ML engineering evaluation with explicitly limited scientific acceptance.',
        '**Safety:** shadow-only; no model-triggered AWS writes, traffic shifting, scaling or automatic mitigation.', '',
        '## Relationship to earlier application-failure and public-dataset reports', '',
        'This is a **new native network-degradation experiment**, distinct from the earlier 118-row',
        'healthy-versus-application-failure experiment (9 episodes) in `docs/ml/FINAL_ML_EVALUATION.md`.',
        'Do **not** merge their confusion matrices or claim they are the same native holdout.',
        'Earlier public Edge Fault and SOFI results remain separate benchmark evidence, not equivalent live-network validation.',
        '', '## Verified original observations and experimental design', '',
        f"- Binary dataset: **{source['dataset_rows']} original labeled snapshot rows**, **{source['episode_count']} reviewed episodes**: {source['episode_counts'].get('healthy',0)} healthy and {source['episode_counts'].get('network_degradation',0)} network-degradation.",
        f"- Episode split: **{source['split']}**. Test: **{source['test_rows']} fault-only rows**; held-out healthy test rows: **0**.",
        '- Identical held-out snapshot IDs across all three models: **verified**.',
        f"- Original reviewed dataset SHA-256: `{source['dataset_sha256']}`.",
        '- Frozen models: SHA-256 reverified from saved training artifacts; preprocessing preserves missingness/freshness.',
        '- Detection horizon: **0 seconds** (present-condition classification, not forecasting).', '',
        '## Three-arm paired network-degradation evaluation', '',
        '| Model | Fault recall | Fault-class F1* | Fault-only accuracy† | Abstentions | Verified healthy held-out |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for g in GROUPS:
        m=models[g]
        rows.append(f"| `{g}` | {pct(m['fault_recall_including_abstentions_as_misses'])} | {pct(m['fault_f1_including_abstentions_as_misses'])} | {pct(m['accuracy_including_abstentions_as_errors'])} | {m['abstentions']}/{m['test_rows']} | {m['verified_healthy_rows']} |")
    eps=s6['heldout'].get('episode_results',[])
    rows.extend(['', '*Fault F1 is conditional on the **fault-only** holdout, not a population-wide F1 or healthy-condition quality measure.',
                 '†Abstentions are counted as wrong in fault-only accuracy.',
                 '', '### Incident detection delay and paired interpretation', '',
                 '| Test episode | AWS-only | AWS+RIPE | AWS+RIPE+HTTP |', '|---|---:|---:|---:|'])
    for ep in eps:
        cells=[]
        for g in GROUPS:
            m=ep['models'][g]
            v=m.get('onset_to_first_detection_seconds')
            cells.append(f'{v:.3f}s' if m.get('detected') is True and v is not None else 'miss / abstain')
        rows.append(f"| `{ep['episode_id']}` | "+' | '.join(cells)+' |')
    rows.extend(['', '**Scientific conclusion:** external RIPE/HTTP signals did **not** demonstrate earlier or more reliable detection than AWS-only on this eligible held-out study. This is a negative or inconclusive comparison, **not** proof that external signals are useless.',
                 '', '## Prospective shadow integration and safety', '',
                 f"- **{pro['unique_snapshots']} new observed snapshots** spanning **{pro['span_seconds']} s**; **{pro['trained_predictions']} trained predictions**, **{pro['abstentions']} abstentions**.",
                 '- Every prospective response passed independent policy review; `approved_for_execution=false`, `executed=false`, `effective_action=hold`.',
                 f"- Local authenticated HTTP smoke: `{s7['status']}`. Unauthorized inference rejected and 3 model groups returned.",
                 '- Local Docker image: '+(f"`{docker['image_id']}`." if docker['image_id'] else 'previous terminal build succeeded; current Docker inspection not verified.') ,
                 '- Local container startup/health/auth check: **'+container['status']+'**.',
                 '- ECR push, actual EKS deployment and authenticated in-cluster telemetry: **NOT VERIFIED**.',
                 '', '## Required scientific limitations', '',
                 '1. **No globally verified healthy held-out test**; false alarms/day, operational specificity and healthy-class precision are **not estimable** from these predictions.',
                 '2. **No separately verified recovery class**; recovery classification and mitigation-to-recovery delays are not estimable.',
                 '3. The combined six-condition native dataset (`healthy`, `application_failure`, `network_degradation`, `partial_outage`, `transient`, `recovery`) has not been validated as one study.',
                 '4. The local HTTP journal in earlier audits was predominantly 15-second cadence; independent original 5-second HTTP evidence must not be inferred from it.',
                 '5. Two held-out fault episodes and a short prospective run are insufficient to demonstrate robust field accuracy, calibrated confidence or long-term reliability.',
                 '6. The Virginia-only normal-window approval is **not** global healthy ground truth. Single-probe RIPE loss is not proof of a new regional outage.',
                 '7. A previous Step 4 AWS/CloudTrail management-event audit had incomplete coverage; do not describe it as comprehensive.',
                 '', '## Verification and reproducibility', '',
                 f"- Step 8 full repository pytest: **{tests['status']}** ({tests.get('summary','not run')}).",
                 f"- Verified Git branch during this run: `{git['branch']}`.",
                 '- Existing saved reports: `data/ml-results/native/step6-network-validation/STEP6_REPORT.json` and `.md`, `data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json`.',
                 '- Run: `python -m pytest -q` then `python step8_finish/finalize_step8.py --skip-tests` (requires prior passing test evidence; skip does not certify new test success).',
                 '- Raw telemetry, credentials, model binaries and private evidence remain under ignored/private `data/`; **do not commit**.',
                 '', '## Final assessment', '',
                 '**Engineering delivery:** complete for the frozen local two-class network detector and the shadow-only service. **Scientific acceptance:** conditional/incomplete for six-class coverage, global false alarms, recovery and generalization. **Deployment:** not certified in EKS. **Repository handoff:** requires reviewed Git staging/commit/push/PR acceptance.', '',
                 f'_Generated from local evidence at {datetime.now(timezone.utc).isoformat()}._', ''])
    return '\n'.join(rows)


def handoff_md(s6,git,tests,docker,container):
    return '\n'.join([
        '# AdaptiveNet ML — Step 8 submission handoff', '',
        '**Scope:** frozen network binary ML and existing earlier public/application studies. Shadow-only.', '',
        '## Verified in local ML work', '',
        f"- Network dataset: {s6['source_integrity']['dataset_rows']} labeled snapshots, {s6['source_integrity']['episode_count']} episodes.",
        '- Three frozen native logistic-regression arms; same held-out records; model hashes checked.',
        '- Prospective shadow run; no automatic AWS writes or mitigation.',
        '- Step 7 authenticated HTTP inference verified; 14 local service security tests passed previously.',
        f"- Docker image: {docker.get('image_id') or 'not visible to current Docker API'}.",
        f"- Ephemeral Docker runtime startup, readiness, unauthenticated request check: {container['status']}.",
        f"- Full tests in this Step 8 run: {tests['status']} ({tests.get('summary','not run')}).", '',
        '## Scientifically unresolved (do not mark PASS)', '',
        '- Six-class native ground truth incomplete (particularly partial outage, transient, recovery).',
        '- Held-out globally healthy false-alarm rate/day not estimable.',
        '- External telemetry improvement over AWS-only not demonstrated.',
        '- Original native 5-second witness completeness not independently established from 15-second local logs.',
        '- Production/EKS internal inference and secure telemetry delivery not certified.', '',
        '## Controlled code handoff', '',
        f"- Branch at latest local inspection: `{git['branch']}`.",
        f"- Safe-path modified candidate count: {len(git['candidate_files'])}; blocked private/risky paths: {len(git['blocked_files'])}; unrelated paths: {len(git['unrelated_files'])}.",
        '- Inspect the private `data/ml-results/native/step8-finalization/STEP8_CANDIDATES.json` before staging.',
        '- Do not run `git add .`; do not push private datasets, credentials, `.env`, model pickle files, local archives, generated Docker build context or unrelated SQS/Terraform scripts.',
        '- Perform a reviewed, explicit-file Git stage; `git diff --cached --check`; `git diff --cached --name-only`; review staged content and secret scan before committing.',
        '- Push feature branch and open a PR only after independent project/team approval; never assert submission before remote confirmation.',
        '- Infrastructure operator must independently verify ECR digest, EKS readiness, NetworkPolicy, authentication, and fresh telemetry.', '',
        '## Evidence paths (local only; do not commit)', '',
        '- `data/ml-results/native/step6-network-validation/STEP6_REPORT.md`',
        '- `data/ml-results/native/step6-network-validation/STEP6_REPORT.json`',
        '- `data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json`',
        '- `data/ml-results/native/step7-shadow-handoff/STEP7_REPORT.md`',
        '- `data/ml-results/native/step8-finalization/STEP8_ACCEPTANCE.json`',
        '', '## Exit gate', '',
        'Step 8 local evidence/report/testing is considered complete only when current full tests pass, Git diff and staged scope are clean, and a reviewer accepts the documented limitations. Git submission is a distinct unverified gate until a push/PR is confirmed.', ''
    ])


def run_tests(root=ROOT,skip=False):
    if skip:return {'status':'NOT_RUN_THIS_TURN','summary':'--skip-tests selected','return_code':None}
    env=os.environ.copy()
    msys=Path('C:/msys64/usr/bin')
    if os.name=='nt' and msys.is_dir():
        env['PATH']=str(msys)+os.pathsep+env.get('PATH','')
    status,output=shell([sys.executable,'-m','pytest','-q'],root=root,timeout=240,env=env)
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'STEP8_PYTEST.log').write_text(output,encoding='utf-8')
    tail=[line.strip() for line in output.splitlines() if 'passed' in line or 'failed' in line or 'error' in line.lower()]
    return {'status':'PASS' if status==0 else 'FAIL','summary':tail[-1] if tail else 'no pytest summary',
            'return_code':status,'log':str((OUT/'STEP8_PYTEST.log').relative_to(root))}


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--skip-tests',action='store_true',help='Diagnostic only; cannot certify final current test results')
    args=ap.parse_args(argv)
    s6,s7=collect_evidence()
    git=git_inventory()
    docker=docker_image_status()
    container=docker_container_test(docker)
    tests=run_tests(skip=args.skip_tests)
    DOCS.mkdir(parents=True,exist_ok=True)
    OUT.mkdir(parents=True,exist_ok=True)
    report_text=report_md(s6,s7,git,tests,docker,container)
    handoff_text=handoff_md(s6,git,tests,docker,container)
    REPORT.write_text(report_text,encoding='utf-8')
    HANDOFF.write_text(handoff_text,encoding='utf-8')
    git=git_inventory()  # include newly generated reports
    result={
        'status':'STEP8_LOCAL_ACCEPTANCE_READY_FOR_MANUAL_GIT_REVIEW' if
        tests['status']=='PASS' and container['status']=='EPHEMERAL_DOCKER_SERVICE_VERIFIED' and git['branch']=='feature/ml-team-starter-20261104' and
        git['git_diff_check_passed'] and git['git_staged_diff_check_passed'] and
        not git['staged_risky_paths'] else 'STEP8_REVIEW_REQUIRED_NOT_SUBMITTED',
        'generated_at_utc':datetime.now(timezone.utc).isoformat(),
        'evidence_verified':True, 'model_artifacts_verified':3, 'episode_count':s6['source_integrity']['episode_count'],
        'heldout_healthy_support':0,'global_healthy_false_alarm_rate':None,
        'recovery_class_evaluation':None,'external_superiority_demonstrated':False,
        'full_repository_tests':tests,'git':git,'docker':docker,'local_container_check':container,
        'eks_deployment_verified':False,'end_to_end_in_cluster_telemetry_verified':False,
        'committed':False,'pushed':False,'pr_created':False,
        'generated_docs':[str(REPORT.relative_to(ROOT)),str(HANDOFF.relative_to(ROOT))]
    }
    (OUT/'STEP8_ACCEPTANCE.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    (OUT/'STEP8_CANDIDATES.json').write_text(json.dumps(git,indent=2)+'\n',encoding='utf-8')
    print('STEP 8 STATUS:',result['status'])
    print('EVIDENCE: 3 frozen models checked | episodes:',result['episode_count'],'| identical held-out test: True')
    print('FULL PROJECT TESTS:',tests['status'], '|',tests['summary'])
    print('DOCKER LOCAL:',docker['status'],'| ephemeral service:',container['status'])
    print('GIT BRANCH:',git['branch'],'| Safe-path changed files:',len(git['candidate_files']),
          '| Blocked:',len(git['blocked_files']),'| Unrelated:',len(git['unrelated_files']))
    print('EKS: NOT VERIFIED | GLOBAL HEALTHY FALSE ALARMS: NOT ESTIMABLE | SIX-CLASS: INCOMPLETE')
    print('FINAL REPORT:',REPORT)
    print('PR HANDOFF:',HANDOFF)
    print('GIT REVIEW INVENTORY:',OUT/'STEP8_CANDIDATES.json')
    print('NO FILES STAGED, COMMITTED, PUSHED OR DEPLOYED')
    return 0 if result['status']=='STEP8_LOCAL_ACCEPTANCE_READY_FOR_MANUAL_GIT_REVIEW' else 2

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('STEP 8 FAILED SAFELY:',type(exc).__name__+': '+str(exc),file=sys.stderr)
        raise SystemExit(2)
