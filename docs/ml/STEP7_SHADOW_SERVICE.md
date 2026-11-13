# AdaptiveNet Step 7 — Frozen ML inference and EKS handoff

**Scope:** ML-only preparation and independent reviewer handoff. Does not perform AWS writes, deploy to EKS, modify SQS, alter the controller, or enable mitigation.

## Minimum Windows commands

From the repository root (PowerShell shown; the same Python commands work on macOS and Linux):

```powershell
python -m unittest discover -s step7_finish -p test_step7.py -v
if ($LASTEXITCODE -ne 0) { throw "Step 7 tests failed" }
python .\step7_finish\finish_step7.py
if ($LASTEXITCODE -ne 0) { throw "Step 7 verification failed" }
```

Expected status: `STEP7_ML_SERVICE_PACKAGED_EKS_NOT_YET_VERIFIED`. This is **not an EKS deployment success**.

## Test the actual local HTTP service

Run after the archived feature and HTTP witnesses are being collected by the existing pipeline. It starts a local server with an ephemeral random secret, rejects unauthenticated requests, submits a genuine fresh snapshot, tests all three frozen models and reviewer decisions, then shuts down. No Docker/AWS calls.

```powershell
python .\step7_finish\smoke_step7.py
if ($LASTEXITCODE -ne 0) { throw "Local HTTP smoke unavailable or failed" }
```

## Build the optional, allowlisted Docker image

The one-command packager creates a new local `data/ml-results/native/step7-container-context/` containing only Python source and three previously SHA-256-verified model artifacts with the frozen evaluation report. Do not build from the repo root; it can include private `.env`, raw events, SQL databases and credentials.

```powershell
docker build -t adaptivenet-shadow-ml:step7 .\data\ml-results\native\step7-container-context
if ($LASTEXITCODE -ne 0) { throw "Step 7 container build failed" }
```

This is a local image only; it is not pushed to ECR or deployed to EKS. Confirm the image can start using a random local token without hard-coding it into a file or Git. The Docker image includes model pickle files — only images built from **locally trained, trusted, hash-verified artifacts** may be used.

```powershell
$env:ADAPTIVENET_SHADOW_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:ADAPTIVENET_SHADOW_BIND = "127.0.0.1"
python -m step7_finish.service
```

The above foreground command starts a local, token-protected Python service on port `8090`; stop using Ctrl+C. Prefer a cryptographic random token generator (below) for any real integration secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`GET http://127.0.0.1:8090/healthz` and `GET http://127.0.0.1:8090/readyz` should return JSON; only the authenticated `POST /v1/infer` accepts originals. The request schema is:

```json
{
  "original_snapshot": { "contract_version": "adaptivenet.features.v1", "snapshot_id": "features-...", "computed_at_unix": 0 },
  "http_witness_events": []
}
```

This is **illustrative**, not valid measurement data. Use actual current feature snapshots and original witnesses (no modified timestamps). The service reconstructs model inputs from the original snapshot, invokes all three frozen models and the existing `shadow_review`, and returns explicit `effective_action=hold`, `approved_for_execution=false`, `executed=false`. Any bad input is refused. It cannot actuate anything.

**Security:** The bearer token and internal Kubernetes NetworkPolicy are required; a ClusterIP alone is not authentication. The template permits only `app: adaptivenet-feature-worker` to reach the service; the telemetry teammate must check that the real producer pod label and namespace match. Use private VPC transport/TLS where required by team policy. Do not expose this endpoint over the public Internet, and never place real tokens in commands/screenshots you submit.

## Infrastructure teammate handoff (not automatically executed)

1. Review generated `STEP7_REPORT.md` and locally built image, then build/push a **digest-pinned** image to the team's existing ECR account/registry, using team CI and approved IAM identity. Do not build a second AWS account.
2. Provision `adaptivenet-shadow-ml-token` as a Kubernetes Secret via an approved secret manager. The deployment manifest `step7_finish/k8s-shadow.yaml` intentionally contains only `secretKeyRef` — **no token value**.
3. Update the image placeholder in the manifest to the actual ECR URI at `@sha256:...` and verify namespace and `app: adaptivenet-feature-worker` source selector with the EKS operator.
4. Have the EKS operator deploy the manifest to the **approved cluster(s)**. Avoid assuming both SG and Virginia need their own inference replicas; decide the placement based on team architecture. Maintain `Service` type `ClusterIP`, read-only service account, disabled Kubernetes API token, and zero-privilege container.
5. Observe pod readiness, service endpoints, network policy, authenticated inference with a fresh genuine snapshot, and independent reviewer output. Record image digest and deployment evidence. No `scale_out`, `shift_traffic`, controller action or auto-mitigation.

Read-only verification commands for the EKS operator (replace the context with the real one):

```powershell
kubectl --context <approved-context> -n adaptivenet get deployment adaptivenet-shadow-ml
kubectl --context <approved-context> -n adaptivenet get pods -l app=adaptivenet-shadow-ml
kubectl --context <approved-context> -n adaptivenet get service adaptivenet-shadow-ml
kubectl --context <approved-context> -n adaptivenet get networkpolicy adaptivenet-shadow-ml-ingress
```

**Step 7 acceptance:** model manifest/version documented, exact SHA verification, local service/container built and tested, deployment healthy inside the approved EKS cluster, telemetry transport authenticated, every real proposal reviewed and on hold. Do not label Step 7 fully deployed unless EKS verification and secure telemetry handoff actually happen. Offline/local work can proceed to Step 8 documentation while awaiting the infrastructure teammate's deploy approval.

**Limitations carried forward:** external-signal improvement not demonstrated; held-out global healthy false alarms not estimable; no recovery-class ground truth; no autonomous mitigation.

Optional read-only deployment check after the operator deploys:

```powershell
python .\step7_finish\verify_eks_readonly.py --context APPROVED_EKS_CONTEXT
```

This checks immutable image reference, pod readiness, service type/endpoints, Secret reference and NetworkPolicy. It does not prove in-cluster HTTP delivery or end-to-end security; those are operator-owned acceptance tests.
