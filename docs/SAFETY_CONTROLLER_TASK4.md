# AdaptiveNet Task 4 safety controller

## Local routing evaluation

Both runs used the same gateway code, a 900 ms delay on the path to Region A, a 120-second fault duration, and a 500 ms client deadline. The static run replayed the adaptive run's client burst timing relative to fault acceptance. Each mode sent 192 catalogue requests in four windows of 48, with up to four requests in flight.

| Mode | Final A/B split | Shifts | HTTP 200 | Within 500 ms | Final window within 500 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Static local route | 100/0 | 0 | 192/192 | 0/192 (0.0%) | 0/48 |
| Local safety controller | 0/100 | 4 | 192/192 | 125/192 (65.1%) | 48/48 |

The controller first shifted traffic 1.93 seconds after the first observed slowdown. It confirmed recovery 60.18 seconds after that observation. Region B served all 48 requests in the final 11.39-second window, with latencies from 40.3 to 74.6 ms. The static route had no qualifying recovery window before its last sample. The fault was still active at the end of that run.

These were two single local runs. The static run checked that the fault was active and replayed the client schedule. The adaptive run also sent controller probes to qualify each shift and verify B. The proposal and capacity readings were fixtures. These results describe the tested path delay and workload. CloudFront failover, live EKS scaling, and trained-model performance require their own measurements.

| Local evidence | Workspace file |
| --- | --- |
| Adaptive route trial at 500 ms | [20261109-133312-3d848005.jsonl](../data/safety-controller-runs/20261109-133312-3d848005.jsonl) |
| Matched static route trial | [20261109-135639-de46afb0-static-baseline.jsonl](../data/safety-controller-runs/20261109-135639-de46afb0-static-baseline.jsonl) |
| Gateway benchmark before and after connection reuse | [20261109-132345-6d7f94ae-gateway-latency.jsonl](../data/safety-controller-runs/20261109-132345-6d7f94ae-gateway-latency.jsonl) |
| Earlier blocked run with overlapping requests | [20261109-131419-a49bb9cc.jsonl](../data/safety-controller-runs/20261109-131419-a49bb9cc.jsonl) |

The logs are stored under the workspace's ignored `data/` directory. Keep them with the evaluation bundle when sharing the report.

## Controller operation

The controller supports `local` execution, `eks` scaling and a `disabled` stop mode. Callers must
explicitly choose a mode when constructing `SafetyController`; there is no default
and no shadow mode. Local execution retains proposal validation, three distinct
qualifying observations, bounded changes, a pending-action journal, cooldown and
client recovery verification.

The standalone shadow assessment module and `model_handoff.py shadow` command
have been removed. The existing `input`, `demo` and `review` commands remain model
handoff utilities; policy review alone does not execute an action.

Local routing changes the local gateway. Scaling currently uses an emulated HPA
and synthetic client measurements. `KubernetesHpaReader` in `kubernetes_hpa.py`
reads real workload state for capacity checks; it has no write operations. Live EKS scaling is now available through the separately configured `LiveHpa` adapter.
Trained-model-to-action integration remains pending. Missing execution support must not be presented as a successful action.

The emulated HPA exercises two-to-three replicas, pending readiness, client
verification, cooldown, failures and timeouts. Routing and scaling share one
pending-action journal slot.

## Historical EKS read-access validation

These results were collected before shadow mode was removed; the old runner is no longer supported.

On 9 November 2026, the team read the HPA, Deployment, pods, and pod metrics from both clusters using kubectl in their respective CloudShell VPC environments. The controller replayed each capture offline using its capture timestamp, a fixture feature snapshot, and a fixture scale-out proposal.

| Cluster | Namespace | Deployment and HPA | CPU current / target | Ready / desired | Oldest pod metric at capture | Shadow result |
| --- | --- | --- | ---: | ---: | ---: | --- |
| adaptivenet-sg | adaptivenet | demo-sg | 2% / 60% | 2 / 2 | 23 s | [Blocked](../data/safety-controller-runs/1794217801-sg-eks-shadow-replay.json) |
| adaptivenet-us | adaptivenet | demo-us | 2% / 60% | 2 / 2 | 21 s | [Blocked](../data/safety-controller-runs/1794217907-us-eks-shadow-replay.json) |

Both HPAs reported `ScalingLimited=True` with reason `TooFewReplicas`, indicating they held at their configured minimum of two. The controller returned `hpa_scaling_limited_or_unknown`, `effective_action=hold`, and `executed=false` for both. These results verify authenticated reads via kubectl and the controller's handling of captured EKS responses.

The controller also ran directly against both private Kubernetes APIs from their respective CloudShell VPC environments. Each run used a fixture feature snapshot and scale-out proposal. The oldest pod metric was about 26 seconds old at the start of the Singapore read and 23 seconds old at the start of the Virginia read. Both runs reported two ready replicas, 2% CPU utilization against a 60% target, and `ScalingLimited=True`. Both returned `hpa_scaling_limited_or_unknown`, `effective_action=hold`, and `executed=false`. Validation with trained-model output is still pending.

To evaluate real scaling, collect the conventional HPA run and the AdaptiveNet run with matched workloads and client observations. The local static route comparison covers the routing experiment.

## Regression checks

The final repository run passed 242 tests with one existing Bash invocation test deselected. That test passed a Windows path directly to Bash and failed to locate the script. The script passed a separate `bash -n` check with LF-normalized input. The TypeScript handoff contracts also type-checked.

## EKS connection check

The first connection step uses `scripts/eks_controller.py status --site sg
--kubeconfig ~/adaptivenet-sg.kubeconfig` (or `us` with the US kubeconfig).
Run it in the corresponding existing CloudShell VPC environment. It requires
Python 3 and authenticated kubectl. The connection pins the account, region,
cluster and workload, verifies an HTTPS EKS endpoint, and exposes only four
allowlisted GET paths. No cloud resources are changed by this command.

A successful read returns `connected: true` with HPA and readiness observations.
A blocked capacity recommendation can coexist with a successful connection.
This check does not establish live scaling execution. HPA writes require the separate `scale` command described below. The generated workspace-only pasteable commands
are `data/eks-connection-check/check-sg.txt` and `check-us.txt`.

## EKS scaling adapter (local tests passed; controller live write not yet exercised)

`LiveHpa` connects `SafetyController(mode="eks")` to the pinned EKS workload.
It raises only `HPA.spec.minReplicas` by one, within the existing 2..6 bounds.
It does not patch Deployment replicas or increase maxReplicas. An atomic JSON
patch checks resourceVersion, target and replica bounds. The action journal is
committed first; a matching HPA annotation supports readback after restart.
An uncertain write blocks subsequent actions instead of blindly retrying.

Run from a checkout in the authenticated CloudShell VPC environment:

```bash
python3 scripts/eks_controller.py scale --site sg \
  --kubeconfig ~/adaptivenet-sg.kubeconfig \
  --snapshot features.json --proposal proposal.json \
  --journal ~/adaptivenet-eks-actions.db
```

This command can change real capacity. It requires a fresh matching proposal,
three distinct fresh metric timestamps, supporting CPU load, and all existing
capacity checks. Current idle readings (2% CPU) do not qualify. The command polls
at most 12 times at five-second intervals; incomplete evidence returns exit 2.
The current conservative ScalingLimited check is retained, including its block
at the HPA minimum. This implementation does not bypass it for a demonstration.

Replica readiness returns `verifying`, not a recovery claim. Client HTTP
observations must still be submitted through `SafetyController.verify_scale`.
EKS routing is unavailable. Model artifacts and prediction-to-action mapping
remain separate integration work.

Raising the HPA minimum persists until explicitly restored. The pre-action
journal and HPA annotation retain the original minimum. No automatic rollback
is implemented; an operator must inspect an uncertain/failed action and restore
the original minimum if appropriate. This can affect Helm drift and idle cost.
CloudShell VPC home storage is ephemeral: preserve the journal outside the
session before relying on restart/recovery across environments. A live mutation
and recovery experiment has not yet been performed.

## Bounded EKS load-to-readiness test

`scripts/eks_load_test.py` is an operator-started experiment for Singapore only.
It checks for the original 2 ready replicas and 2..6 bounds, reuses the deployed
trusted ECR image, and creates a uniquely named Job against the internal demo
catalogue. Eight workers offer at most 120 requests/second for 90 seconds; the
Job has a 120-second deadline, no retries, resource limits and TTL cleanup.

The actual safety controller observes EKS metrics while processing explicitly
labelled fixture snapshots/proposals. It does not relax CPU, timestamp, evidence,
HPA-limit or readiness gates. Native HPA scaling is distinct from an annotated
controller action. Failure to qualify an action is reported as such. This test
may drive the native HPA up to the existing maximum of six replicas.

The runner deletes its load Job on exit and conditionally restores only its own
HPA minimum using annotation ownership and resource-version tests. Concurrent
changes are not overwritten; cleanup errors require operator inspection.
A readiness result is not client performance recovery. The action journal stays
blocked after test cleanup, preserving the evidence rather than allowing reuse.
Raw observations, load manifest and summary are saved to the chosen evidence
directory. VPC CloudShell storage is ephemeral; export required evidence before
the session ends. Keep the terminal open during the experiment; a lost process
can prevent HPA-floor cleanup, even though the Job deadline bounds the load.

Build a dependency-free pasteable bundle locally with
`python -m scripts.package_eks_load_test`. The generated command is
`data/eks-controller-load-test/run-sg.txt`. **Update 13 Nov 2026:** this load test ran
live on `adaptivenet-sg` (see `docs/LIVE_TESTS_20261113.md`). Kubernetes' own HPA scaled
2→3→4 pods with 0 of 10,762 requests failed, while the controller correctly held (it saw
CPU at or above target in only one reading, not three). The controller's own HPA write
was therefore not exercised live. Existing ML code has not been changed for this test.

## Branch checkpoint: 12 November 2026

Focused controller/EKS validation passed 115 tests before this checkpoint.
The separately rerun ML regression
`test_shadow_masks_future_ripe_even_when_old_quality_flag_is_fresh` failed at this
checkpoint; it was fixed on `submission/final` on 13 Nov 2026 (`ml/team_starter/shadow.py`
now abstains when RIPE data is future-dated). The branch is a progress checkpoint, not production acceptance.
Manual Singapore HPA scaling from two to three ready replicas and restoration
to two were verified by the operator. Controller-driven live scaling and client
recovery remain unverified (the 13 Nov live test above showed HPA scaling with the
controller holding).
