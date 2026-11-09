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

## Scaling and shadow mode

The emulated HPA exercises a request from two to three replicas, pending readiness, client verification, cooldown, failures, and timeouts. The routing and scaling paths share one pending-action journal slot. The scaling client measurements are synthetic.

The shadow workflow reads a current `adaptivenet.features.v1` snapshot, builds `model_input.v1`, validates `proposal.v1`, and returns an advisory result with `executed=false`. It reads Kubernetes only for a valid scale proposal. The older `decision_review.v1` result remains separately available in the output. A shift review requires the observed route weight.

```powershell
python scripts/model_handoff.py shadow --db data/adaptivenet-ingestion.db --mode combined
python scripts/model_handoff.py shadow --snapshot data/features.json --proposal data/proposal.json --hpa-config data/hpa-shadow.json --mode combined --out data/shadow-review.json
```

For the second command, use a fresh feature snapshot and a proposal carrying its exact snapshot ID. Add `--current-a-percent` for a shift proposal. The first command uses the bundled `ml.predictor.predict()` entry point, which currently calls the conservative rule baseline. An ML teammate can supply a proposal JSON or update that entry point.

The optional HPA configuration maps each region to a confirmed cluster, namespace, Deployment, and HPA. For each cluster, supply its HTTPS API origin, CA bundle, and a file containing a current read-only bearer token. File paths are relative to the configuration file.

```json
{
  "workloads": {
    "region-a": {
      "cluster": "confirmed-sg-cluster",
      "namespace": "confirmed-namespace",
      "deployment": "confirmed-deployment",
      "hpa": "confirmed-hpa"
    }
  },
  "clusters": {
    "confirmed-sg-cluster": {
      "server": "https://api.example.invalid",
      "ca_file": "sg-ca.pem",
      "token_file": "sg-read-only-token"
    }
  }
}
```

## EKS read access and shadow replay

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
