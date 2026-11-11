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

The controller supports `local` execution and a `disabled` stop mode. Callers must
explicitly choose a mode when constructing `SafetyController`; there is no default
and no shadow mode. Local execution retains proposal validation, three distinct
qualifying observations, bounded changes, a pending-action journal, cooldown and
client recovery verification.

The standalone shadow assessment module and `model_handoff.py shadow` command
have been removed. The existing `input`, `demo` and `review` commands remain model
handoff utilities; policy review alone does not execute an action.

Local routing changes the local gateway. Scaling currently uses an emulated HPA
and synthetic client measurements. `KubernetesHpaReader` in `kubernetes_hpa.py`
reads real workload state for capacity checks; it has no write operations. Live
EKS scaling and trained-model-to-action integration are not implemented by this
removal. Missing execution support must not be presented as a successful action.

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
