# AdaptiveNet ML — Step 8 submission handoff

**Scope:** frozen network binary ML and existing earlier public/application studies. Shadow-only.

## Verified in local ML work

- Network dataset: 158 labeled snapshots, 10 episodes.
- Three frozen native logistic-regression arms; same held-out records; model hashes checked.
- Prospective shadow run; no automatic AWS writes or mitigation.
- Step 7 authenticated HTTP inference verified; 14 local service security tests passed previously.
- Docker image: sha256:ddb51268aed9ecde827846aea3f0e5a1b291c34473a90e5476b66168a34e1234.
- Ephemeral Docker runtime startup, readiness, unauthenticated request check: EPHEMERAL_DOCKER_SERVICE_VERIFIED.
- Full tests in this Step 8 run: PASS (131 passed, 2 warnings in 17.06s).

## Scientifically unresolved (do not mark PASS)

- Six-class native ground truth incomplete (particularly partial outage, transient, recovery).
- Held-out globally healthy false-alarm rate/day not estimable.
- External telemetry improvement over AWS-only not demonstrated.
- Original native 5-second witness completeness not independently established from 15-second local logs.
- Production/EKS internal inference and secure telemetry delivery not certified.

## Controlled code handoff

- Branch at latest local inspection: `feature/ml-team-starter-20261104`.
- Safe-path modified candidate count: 121; blocked private/risky paths: 28; unrelated paths: 101.
- Inspect the private `data/ml-results/native/step8-finalization/STEP8_CANDIDATES.json` before staging.
- Do not run `git add .`; do not push private datasets, credentials, `.env`, model pickle files, local archives, generated Docker build context or unrelated SQS/Terraform scripts.
- Perform a reviewed, explicit-file Git stage; `git diff --cached --check`; `git diff --cached --name-only`; review staged content and secret scan before committing.
- Push feature branch and open a PR only after independent project/team approval; never assert submission before remote confirmation.
- Infrastructure operator must independently verify ECR digest, EKS readiness, NetworkPolicy, authentication, and fresh telemetry.

## Evidence paths (local only; do not commit)

- `data/ml-results/native/step6-network-validation/STEP6_REPORT.md`
- `data/ml-results/native/step6-network-validation/STEP6_REPORT.json`
- `data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json`
- `data/ml-results/native/step7-shadow-handoff/STEP7_REPORT.md`
- `data/ml-results/native/step8-finalization/STEP8_ACCEPTANCE.json`

## Exit gate

Step 8 local evidence/report/testing is considered complete only when current full tests pass, Git diff and staged scope are clean, and a reviewer accepts the documented limitations. Git submission is a distinct unverified gate until a push/PR is confirmed.
