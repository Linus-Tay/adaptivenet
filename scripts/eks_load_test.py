"""Bounded, operator-started EKS load/scale-to-readiness experiment (fixture proposal)."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from adaptivenet.controller.kubectl_connection import KubectlConnection
from adaptivenet.controller.kubernetes_hpa import KubernetesReadError
from adaptivenet.controller.live_hpa import LiveHpa, ANNOTATION
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.safety import SafetyController

LOAD_SECONDS = 90
LOAD_CODE = r'''import concurrent.futures, json, time, urllib.request
url = "http://demo-sg.adaptivenet.svc.cluster.local/api/catalog"
stop = time.monotonic() + 90

def worker(_):
    good = failed = 0
    while time.monotonic() < stop:
        start = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = json.loads(response.read(65536))
                if response.status == 200 and body.get("region") == "region-a":
                    good += 1
                else:
                    failed += 1
        except Exception:
            failed += 1
        time.sleep(max(0, 1/15 - (time.monotonic()-start)))
    return good, failed
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    counts = list(pool.map(worker, range(8)))
print(json.dumps({"http_success":sum(x[0] for x in counts), "http_failed":sum(x[1] for x in counts),
                  "duration_seconds":90,"maximum_offered_rps":120}), flush=True)
'''

def job_manifest(name, image):
    return {"apiVersion":"batch/v1", "kind":"Job", "metadata":{"name":name,"namespace":"adaptivenet"},
            "spec":{"backoffLimit":0,"activeDeadlineSeconds":120,"ttlSecondsAfterFinished":300,
                    "template":{"metadata":{"labels":{"app":"adaptivenet-controller-load-test"}},
                                "spec":{"restartPolicy":"Never","automountServiceAccountToken":False,
                                        "securityContext":{"runAsNonRoot":True,"runAsUser":10001},
                                        "containers":[{"name":"load","image":image,"command":["python","-u","-c",LOAD_CODE],
                                            "resources":{"requests":{"cpu":"100m","memory":"64Mi"},
                                                         "limits":{"cpu":"500m","memory":"128Mi"}},
                                            "securityContext":{"allowPrivilegeEscalation":False,
                                                               "capabilities":{"drop":["ALL"]}}}]}}}}

def restoration_patch(obj, action_id, replicas, original_min, original_max):
    record = json.loads(obj.get("metadata",{}).get("annotations",{}).get(ANNOTATION,"null"))
    if (not record or record.get("action_id") != action_id
            or obj["spec"]["minReplicas"] != replicas or obj["spec"]["maxReplicas"] != original_max):
        raise ValueError("cleanup_ownership_or_bounds_changed_manual_inspection_required")
    return [{"op":"test","path":"/metadata/resourceVersion","value":obj["metadata"]["resourceVersion"]},
            {"op":"test","path":"/metadata/annotations/adaptivenet.io~1capacity-action",
             "value":obj["metadata"]["annotations"][ANNOTATION]},
            {"op":"test","path":"/spec/minReplicas","value":replicas},
            {"op":"test","path":"/spec/maxReplicas","value":original_max},
            {"op":"replace","path":"/spec/minReplicas","value":original_min}]

def emit(obj):
    print(json.dumps(obj,allow_nan=False),flush=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig",required=True)
    parser.add_argument("--evidence-dir",required=True)
    args=parser.parse_args(argv)
    directory=Path(args.evidence_dir).expanduser()
    directory.mkdir(parents=True,exist_ok=False)
    connection=KubectlConnection("sg",args.kubeconfig)
    journal=ActionJournal(directory/"actions.db")
    adapter=LiveHpa(connection,journal)
    controller=SafetyController(journal,None,mode="eks",live_hpa=adapter)
    initial=adapter.observe()
    if (initial.min_replicas,initial.max_replicas,initial.current,initial.desired,initial.ready,initial.available,initial.pending)!=(2,6,2,2,2,2,0):
        raise ValueError("test_requires_original_2_ready_replicas_and_2_to_6_bounds")
    deployment=connection(f"/apis/apps/v1/namespaces/adaptivenet/deployments/demo-sg")
    image=next(c["image"] for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"]=="demo-api")
    if not image.startswith("468354413374.dkr.ecr.ap-southeast-1.amazonaws.com/adaptivenet/services:"):
        raise ValueError("unexpected_demo_image")
    name="controller-load-"+uuid4().hex[:10]
    manifest=directory/"load-job.json"
    manifest.write_text(json.dumps(job_manifest(name,image)), encoding="utf-8")
    summary={"test":"eks_controller_load_to_readiness","input_source":"fixture_proposal; live_eks_metrics",
             "job":name,"controller_requested_capacity":False,"controller_action_journaled":False,"controller_replica_readiness_verified":False,
             "client_recovery_verified":False,"cleanup_errors":[]}
    attempted=False
    try:
        attempted=True
        connection._call(["create","-f",str(manifest),"-o","json"])
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            now=time.time()
            ident=f"features-load-test-fixture-{int(now)}"
            snapshot={"contract_version":"adaptivenet.features.v1","snapshot_id":ident,
                      "computed_at_unix":now,"ready_for_model":True}
            proposal={"contract_version":"adaptivenet.proposal.v1","snapshot_id":ident,"action":"scale_out",
                      "affected_region":"region-a","confidence":.95,"model_id":"fixture-not-trained-ml",
                      "evidence":["operator_load_test_fixture"]}
            result=controller.step_scale(snapshot,proposal,compute_overloaded=True)
            row={"at":time.time(),"controller":asdict(result),
                 "observation":asdict(adapter.last_observation) if adapter.last_observation else None}
            with (directory/"observations.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(row)+"\n")
            emit(row)
            pending=journal.pending()
            if pending:
                summary["controller_action_journaled"]=True
                summary["action_id"]=pending.action_id
                recorded=connection(adapter._path).get("metadata",{}).get("annotations",{}).get(ANNOTATION,"null")
                record=json.loads(recorded)
                summary["controller_requested_capacity"]=bool(record and record.get("action_id")==pending.action_id)
                if result.state=="verifying":
                    summary["controller_replica_readiness_verified"]=True
                    break
                if result.state=="blocked":
                    break
            job=connection._call(["get","job",name,"-n","adaptivenet","-o","json"])
            if (job.get("status",{}).get("succeeded") or job.get("status",{}).get("failed")) and not pending:
                break
            time.sleep(5)
        if not summary["controller_requested_capacity"]:
            summary["result"]="no_controller_action_qualified; inspect_HPA_changes_in_observations"
        else:
            summary["result"]="readiness_verified" if summary["controller_replica_readiness_verified"] else "controller_action_not_verified"
    except (OSError,ValueError,KubernetesReadError) as exc:
        summary["error"]=str(exc)
    finally:
        # Job deadline bounds load even if this terminal disconnects.
        if attempted:
            try:
                connection._call(["delete","job",name,"-n","adaptivenet","--ignore-not-found=true","--wait=false","-o","json"])
            except (ValueError,KubernetesReadError) as exc:
                summary["cleanup_errors"].append("load_job_cleanup: "+str(exc))
        pending=journal.pending()
        if pending:
            try:
                obj=connection(adapter._path)
                patch=restoration_patch(obj,pending.action_id,pending.expected["replicas"],2,6)
                connection.patch_hpa(patch)
                confirmed=connection(adapter._path)
                if confirmed["spec"]["minReplicas"] != 2 or confirmed["spec"]["maxReplicas"] != 6:
                    raise ValueError("minimum_restore_readback_failed")
                summary["original_minimum_restored"]=True
                journal.resolve(pending.action_id,state="blocked",reason="operator_test_cleanup_no_client_recovery_claim")
            except (ValueError,KubernetesReadError) as exc:
                summary["cleanup_errors"].append("hpa_restore: "+str(exc))
        (directory/"summary.json").write_text(json.dumps(summary,indent=2), encoding="utf-8")
        emit(summary)
    return 0 if summary["controller_replica_readiness_verified"] and not summary["cleanup_errors"] else 2

if __name__=="__main__":
    raise SystemExit(main())
