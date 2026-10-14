# Implementation checkpoint — 12 October 2026

Implemented:
- Existing registry, dashboard, GitHub CI, immutable ECR builds and AWS foundation retained.
- Read-only RIPE Atlas measurements; explicit opt-in creation CLI with dry-run default.
- Internal HTTP synthetic sampling (NOT Prometheus CPU/replica metrics).
- 3-consecutive-breach decision policy, cooldown, no automatic failback, no action on missing or stale inputs.
- Token-protected `/controller/observations` and `/controller/tick` endpoints.
- Local gateway mitigation ONLY; off by default via `LOCAL_AUTO_MITIGATION=false`.
- Separate optional AWS demo Terraform for two public minimal HTTP instances, Route 53 weighted records + health checks.

Not implemented (do not represent as implemented):
- Actual RIPE account/credits or new measurements; operator must supply an API key and real targets.
- AWS infrastructure creation; requires authenticated AWS account, enabled services and approval of Terraform plan.
- Prometheus/Kubernetes metrics, SQS consumption, automated AWS mitigation, multi-region EKS, full hosted dashboard,
  closed-loop external/internal fusion, live RIPE data validation, experimental evaluation against HPA.
- Real customer onboarding and production-grade secrets, TLS, auth, and endpoint connectivity.

Starting fresh:
1. `bash scripts/setup-local.sh` and `docker compose up --build -d` (Docker required).
2. Local dashboard: http://localhost:3000, demo: http://localhost:3001.
3. `curl -H "X-Lab-Token: $(grep '^LAB_TOKEN=' .env | cut -d= -f2-)" http://localhost:8000/controller/observations`
4. `curl -X POST -H "X-Lab-Token: $(grep '^LAB_TOKEN=' .env | cut -d= -f2-)" http://localhost:8000/controller/tick`
5. Plan RIPE measurement creation: `python scripts/atlas_setup.py --region-a sg.example.com --region-b us.example.com`
6. Only AFTER public endpoints are running and your RIPE account has credits:
   `export ATLAS_API_KEY=...; python scripts/atlas_setup.py --region-a ACTUAL_SG_TARGET --region-b ACTUAL_US_TARGET --create`.
7. Put returned IDs in .env as `ATLAS_MEASUREMENTS=region-a=ID,region-b=ID`; `docker compose up -d --force-recreate control-api`.
8. Check `GET /telemetry/atlas/latest` with the lab token.
9. For real cloud demo, inspect `infra/terraform/environments/demo-cloud/README.md` and **review Terraform plan before apply**.

CAUTION: RIPE credit costs and AWS costs may accrue. No credentials belong in Git.
