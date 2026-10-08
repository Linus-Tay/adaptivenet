# Setup and integration runbook

For current AWS account, GitHub, ECR/OIDC/state and queue/bucket setup use [AWS_GITHUB_SETUP.md](AWS_GITHUB_SETUP.md). That guide supersedes overlapping setup instructions below. Bootstrap and dev foundation Terraform are now supplied; regional EKS/ALB/DNS modules and runtime integrations remain planned. No AWS credentials are required for the local lab.

## 1. Run the starter locally

Requirements: Node.js 24, Python 3.12; Docker with Compose for the container route. AWS CLI v2, kubectl, Helm and Terraform are needed only for subsequent cloud phases. On Windows, WSL2 is the simplest common shell for the commands below; Docker Desktop can provide the engine.

Extract the ZIP, enter `adaptivenet/`, copy `.env.example` to `.env`, and replace LAB_TOKEN with a random value. Generate one with:

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

Docker route:

```sh
docker compose up --build
```

Visit http://localhost:3000 and http://localhost:3001. Stop with `docker compose down`. The dashboard is local-only and has no operator login yet. Do not publish these local ports or deploy the local gateway as a production traffic layer.

Without Docker, terminal 1 (macOS/Linux/WSL):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
python scripts/local.py
```

Terminal 2:

```sh
npm ci
set -a
. ./.env
set +a
npm run dev:dashboard
```

Terminal 3:

```sh
npm run dev:demo
```

PowerShell equivalent environment loading for terminal 2:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  Set-Item -Path "Env:$name" -Value $value
}
npm run dev:dashboard
```

Activate Python on Windows using `.venv\Scripts\Activate.ps1`. The Python launcher reads root `.env`; Next.js needs LAB_TOKEN in its process environment. Never use `NEXT_PUBLIC_` for this token.

| Address | Service |
|---|---|
| localhost:3000 | Stable operator dashboard |
| localhost:3001 | Stable demo frontend |
| localhost:8000/docs | Control API OpenAPI |
| localhost:8001/docs | Separate experiment API OpenAPI |
| localhost:8080/api/catalog | Local simulated traffic path |
| localhost:8101/api/catalog | Direct region A |
| localhost:8102/api/catalog | Direct region B |

First demo: Route to A, fetch catalogue, inject 1.5-second latency or 503s, fetch again, route to B, fetch successfully. Clear faults and restore A. Faults expire automatically. All lab routing/fault state is in-memory and uses one process per service; do not increase uvicorn workers for these lab modules.

## 2. Add durable metadata and telemetry contracts

Next implementation milestone: PostgreSQL plus migrations, ingestion endpoint and one telemetry-to-window path.

Tables: tenants, memberships, applications, clusters, regional_endpoints, measurement_targets, telemetry_events, feature_windows, classifications, policies, incidents, actions, agent_commands, experiment_runs. Scope rows by tenant and add event/window uniqueness constraints. Use migrations and foreign keys; timestamps are UTC.

Generate OpenAPI/JSON Schema from Pydantic and generate TS types for the dashboard. Authenticate agents/producers before accepting events. Add explicit units, null semantics and source freshness. Build dashboard views for last observation, coverage and integration failures; never display disconnected sources as green.

Done when: one authenticated sample event is persisted once, duplicates are ignored, and the dashboard shows real receipt time/source.

## 3. Connect SQS and storage

Create an AWS development account/project boundary, MFA and a spending budget. Start single-tenant in your own account. Use the already selected Singapore/N. Virginia pair as starter defaults only; verify region availability, quotas and cost before provisioning.

Apply the supplied SQS/DLQ and artifact bucket foundation using AWS_GITHUB_SETUP.md. Then implement PostgreSQL, secret storage, and runtime service IAM roles. Outputs must include queue URL, bucket name, database secret reference and IAM role ARNs. Put real secrets in a secrets manager, not Git or Terraform variables committed to Git.

Use SDK default credential chains locally (AWS SSO/profile) and workload identity in cloud. Ingestion gets SendMessage only; detector gets Receive/Delete/ChangeMessageVisibility; artifact workers get only their S3 prefix. Add an outbox if an operation must atomically update PostgreSQL and enqueue work.

Implement ingestion → SQS → deduplicated PostgreSQL consumer → window assembler. Acknowledge only after persistence. Add long polling, DLQ and a replay command. Start with one assembler, then parallel consumers backed by shared persisted windows.

Done when: replaying one batch twice leaves the same feature counts; restarting workers loses no acknowledged event; a poison message reaches the DLQ.

## 4. Containerize and deploy the regional workload

Build the included image, publish it to ECR with an immutable commit tag, then deploy the same image in each regional EKS cluster. Bootstrap VPC/subnets, EKS/node groups, registry access and AWS Load Balancer Controller. A regional cluster should span multiple AZs for later tests. Keep the control plane and runner outside the target failure domain.

The demo Helm chart is a starting workload chart, not complete EKS/ALB provisioning. After clusters and image registry exist:

```sh
helm upgrade --install demo-a infra/helm/demo --kube-context REGION_A_CONTEXT \
  --namespace adaptivenet-demo --create-namespace \
  --set image.repository=YOUR_ECR_REPOSITORY --set image.tag=YOUR_COMMIT --set region=region-a
helm upgrade --install demo-b infra/helm/demo --kube-context REGION_B_CONTEXT \
  --namespace adaptivenet-demo --create-namespace \
  --set image.repository=YOUR_ECR_REPOSITORY --set image.tag=YOUR_COMMIT --set region=region-b
```

Replace uppercase placeholders. Add regional ALB Ingress resources, ACM certificates, DNS and health checks. Restrict security groups and confirm readiness. The included chart does not configure ingress, HPA or fault injection.

Done when: direct HTTPS requests reach both regions and return the correct region label. Capture image digest, chart version and infrastructure version in every experiment.

## 5. Connect internal telemetry and cluster agent

Instrument the demo API with Prometheus request counters, latency histograms and in-flight request gauges. Install Prometheus, kube-state-metrics and the required Kubernetes resource metrics pipeline. Collect CPU/request ratio, replicas, request rate, error rate, p95 latency and real queue depth if a queue-backed workload is later introduced. Do not invent queue depth for the catalogue.

Implement the outbound agent: scoped enrollment token → cluster registration → short-lived credentials → periodic Prometheus queries → normalized ingestion events. No inbound access to a private cluster API is required. RBAC initially permits reads; later permit only selected HPA/scale operations in the demo namespace. Agent commands require target ownership, expiry, deduplication, bounded parameters and acknowledgements.

Prometheus is canonical; add CloudWatch for ALB/EKS/infrastructure signals after the main path works. Avoid double-counting the same underlying metric.

Done when: load on region A appears in its own fresh feature window, and agent disconnect is shown as unknown/stale.

## 6. Connect RIPE Atlas

Create a RIPE Atlas account/API key and establish measurement credits, cadence and eligible probe coverage. Choose probes from meaningful geographies/ASNs. Start with a small measurement set against targets you control and can actually measure.

Implement the measurement manager with `POST https://atlas.ripe.net/api/v2/measurements/`; persist returned IDs and region/target/protocol/probe mappings. Verify responses and permissions before attempting sustained measurements. Atlas HTTP measurements may need special access. Use suitable supported network/TLS measurements plus your own HTTP clients.

Implement the streamer using the documented API/client, subscribe to assigned measurement IDs, reconnect and REST-backfill gaps. Normalize loss/timeouts without converting missing observations into zero. Stable IDs should incorporate measurement/probe/result identity. Stream and backfill must deduplicate. Archive raw results for reproducibility.

Measure both direct regional and actual global service paths where feasible. Document what each protocol observes: pod HTTP delays may have no effect on Atlas ICMP measurements.

Done when: real Atlas observations populate windows with source/target/coverage, and reconnecting does not duplicate them.

## 7. Build classification before mitigation

Implement deterministic, explainable baseline rules first. Inputs: external latency/loss/timeout deviation, probe coverage, internal CPU/load/queue/replica/endpoint indicators and source freshness. Output cause + severity + confidence + evidence. Missing external data must not force a network diagnosis.

Collect labeled experimental runs. Labels come from the experiment recorder and observed outcomes, not from a classifier's own predictions. Train a small scikit-learn classifier only after the dataset exists; compare it with rules. Separate complete runs/time/probe cohorts across train and test. Never feed injected-fault settings, scenario labels or future samples as features.

Save versioned feature specification, model checksum, training dataset manifest and evaluation in S3. Keep online inference in `classifier/`; offline training in `ml/`. A cause classifier is sufficient initially. Future-failure prediction is a different task requiring labels at a defined forecast horizon.

Done when: unseen-run metrics and confusion matrix demonstrate what the model does better (or worse) than rules.

## 8. Connect Route53 routing

Use Route53 weighted records for the two regional ALB endpoints, matching the proposal diagram. Keep direct regional monitoring names stable. Configure ownership, regional certificates and supported target-health evaluation. Store hosted-zone/record-set identifiers and scoped credential references in application bindings.

Start read-only inventory → dry-run action → audited manual update → confirmed DNS observation → automated policy. Weight changes affect DNS answers and are subject to caching, connection reuse and health-check semantics. A weight of zero is not an unconditional traffic block. Measure actual outcomes. ALB aliases inherit target TTL behaviour; do not promise immediate failover.

Global Accelerator is a future adapter, not required by the first deployment. It changes the observed network path and needs a separate experiment interpretation.

Done when: the controller can read and deliberately update only this application's routing records, and measured new requests shift as expected.

## 9. Close the control loop

Implement Healthy/Degraded/Critical/Recovering state transitions, evidence thresholds, sustained duration, cooldown, destination capacity and healthy-alternate checks. Serialize decisions per application. Persist action_id, before/after state, reason, expiry, actor/model version and rollback data. Distinguish requested action from confirmed success.

For compute overload, use a single scaling owner: HPA settings or direct replicas, not both. For path degradation, prefer routing action if the alternate path is viable. On stale data or low confidence, hold and alert. Restore traffic gradually after sustained recovery. Reject expired or duplicate commands.

Done when: a sustained fault triggers one bounded action; transient noise does not flap; stale telemetry cannot initiate new mitigation; recovery restores the intended steady state.

## 10. Add hosted onboarding and run research evaluation

Only after your own two-region system works, add operator login/tenant isolation, AWS AssumeRole integration with customer-specific trust and ExternalId where applicable, domain ownership checks, least-privilege roles, Helm enrollment and disconnect/revoke flows. Do not request users' root keys. Keep AWS connection, domain connection and cluster enrollment as separate states in the UI.

Support deployment of an image reference + port + resource limits + health path via an allowlisted Helm workload, before considering a full container-build platform. Do not accept arbitrary privileged manifests as the first onboarding workflow.

Run the baseline matrix in EXPERIMENTS.md with identical workloads, faults and capacity limits. Finish with reproducible datasets, charts, architecture and a teardown runbook. EKS, nodes, ALB, Global Accelerator, NAT, storage and telemetry can all continue billing while idle; stop Atlas measurements and remove tagged lab resources when finished. Budget alerts alone do not shut resources down.

## Required accounts and decisions

| Item | Needed for |
|---|---|
| GitHub repository | Source, CI, image and infrastructure workflows |
| AWS account + scoped identity | ECR, EKS, ALB, SQS, S3, DB, Route53 |
| Domain/subdomain with DNS control | Real HTTPS front door and ownership checks |
| RIPE account, API access, credits and probes | Real external measurement data |
| Two regional workloads + external HTTP clients | Measurable failover and latency effects |
| Experiment budget + region selection | Keep deployment scope affordable |
| Labeled independent runs | Train/evaluate a defensible cause classifier |

No paid resources are created by this starter. Do not run Terraform apply until the planned modules are implemented, reviewed and a cost estimate is accepted.
