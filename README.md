# AdaptiveNet starter

A monorepo for network-aware resilience: a stable control plane, an isolated demo workload, telemetry processing, and bounded experiments.

**Implemented:** two Next.js/TypeScript/Tailwind apps, FastAPI control API, stateless regional catalogue API, local route/fault simulator, separate experiment runner, Docker Compose configuration, demo Helm chart, telemetry contract, CI and HTTP smoke test.

**Planned, not implemented:** RIPE collector, Prometheus agent, SQS ingestion/window processing, classifier/controller, PostgreSQL persistence, runtime AWS adapters, regional EKS/ALB/DNS Terraform modules and hosted multi-tenant onboarding. Bootstrap/ECR/OIDC and SQS/S3 foundation Terraform are supplied separately. Their folders contain explicit responsibilities, not fake working integrations. Local routing is manual; no model has been trained.

## AWS and GitHub milestone (7 October 2026)

Start with [AWS_GITHUB_SETUP.md](docs/AWS_GITHUB_SETUP.md) to create your human AWS SSO identity, private repository, OIDC/ECR/state bootstrap and delivery workflows. The account setup and reviewed apply commands run in your own signed-in session.

A persistent application/endpoint registry is now available at `/applications`. Registered endpoints remain unverified/disconnected until their integrations are implemented. Route53 is the first production routing adapter, matching the proposal; Global Accelerator is a future option.

## Start here

1. Read [SETUP.md](docs/SETUP.md) for local commands and cloud integration order.
2. Read [ARCHITECTURE.md](docs/ARCHITECTURE.md) for boundaries and data flow.
3. Read [EXPERIMENTS.md](docs/EXPERIMENTS.md) before treating local faults as research evidence.
4. See [STATUS.md](docs/STATUS.md) for validation and remaining work.

## Folder map

| Directory | Owns |
|---|---|
| `apps/dashboard/` | Operator UI; eventually onboarding, telemetry, incidents and policies |
| `apps/demo-web/` | Stable catalogue frontend showing regional request outcomes |
| `services/adaptivenet/` | Python package; deploy selected modules as separate processes |
| `services/adaptivenet/demo_api.py` | Same stateless API image in each region |
| `services/adaptivenet/experiment_runner.py` | Private experiment API; independent of target workload |
| `services/adaptivenet/gateway.py` | Local-only route/fault simulation |
| `services/adaptivenet/control_api.py` | Local operator API; future tenant and application management |
| `services/adaptivenet/collectors/` | Atlas and optional CloudWatch adapters |
| `services/adaptivenet/ingestion/` | Authenticated normalization and durable enqueue |
| `services/adaptivenet/agent/` | Customer-cluster metrics and scoped command execution |
| `services/adaptivenet/detector/` | Shared, deduplicated event-time windows |
| `services/adaptivenet/classifier/` | Cause/severity inference, independent of actuation |
| `services/adaptivenet/controller/` | Policy, state transitions and mitigation journal |
| `services/adaptivenet/adapters/` | AWS and cluster interfaces |
| `packages/contracts/` | Generated cross-language contracts |
| `infra/terraform/` | Planned AWS resources; module boundaries only |
| `infra/helm/` | Working starter demo chart; planned agent/control charts |
| `infra/observability/` | Prometheus rules and dashboard integration boundary |
| `experiments/` | Scenarios, load generation and result manifests |
| `ml/` | Dataset manifests, offline training/evaluation, model versions |
| `tests/`, `scripts/` | Validation and local development entry points |
| `docs/` | Architecture, integration and research runbooks |

Keep a single Python package initially. A folder is not automatically a microservice: ingestion and the management API can share a deployment early on; detector and controller should run separately. Split further only when execution, security or scaling requires it.

## Quick start with Docker

Install Docker with Compose, copy `.env.example` to `.env`, replace `LAB_TOKEN`, then:

```sh
docker compose up --build
```

Open http://localhost:3000 (control room) and http://localhost:3001 (catalogue). See the setup guide for a Docker-free option. All published ports bind to loopback. This local dashboard has no user-login system: do not expose it publicly.

## First demonstration

Route to A → fetch catalogue → inject A latency or errors → fetch and observe → route to B → fetch successfully → clear faults → restore A. Faults expire after 30 seconds by default. The page stays loaded, but affected API requests honestly show failures. The simulator does not silently retry requests or automatically detect faults.

## Reproducibility

`package-lock.json` pins the installed JS dependency graph. Python version ranges are in `pyproject.toml`; `requirements-dev.lock` records the validated Python 3.12 environment; Docker installs `requirements-runtime.lock`. Published image tags use immutable full commit SHAs. Cloud node/runtime digest selection remains a deployment-stage task. No credentials, paid resources or deployments have been created.
