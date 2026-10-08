# Experiments and evidence

## Keep demonstrations honest

The local simulator injects HTTP delay/errors on the selected request path. It does not inject packet loss, consume real CPU, fail a region, emit RIPE results or perform automatic mitigation. It proves process separation and the manual demonstration flow only.

| Scenario | Implementation stage | Evidence expected |
|---|---|---|
| Local HTTP latency/error | Included gateway simulator | Slower/failed catalogue requests; controls stay alive |
| Compute overload | External load generator + CPU-bound workload endpoint | CPU saturation/queueing; replica response; external path may remain healthy |
| Network latency/loss/timeout | Scoped network fault tooling/proxy on a known path | Degradation seen by observers whose packets traverse that path |
| Pod failure | Scoped Kubernetes experiment executor | Readiness/endpoint loss and actual user impact |
| Node/AZ disruption | Later, only with isolated infrastructure | Scheduler/capacity effects; never label a pod kill an AZ outage |
| Recovery | Fault expiry/explicit cleanup | Stable evidence followed by gradual traffic restoration |

Use a Kubernetes-compatible fault tool only after verifying EKS/kernel/runtime support and privileges. Keep privileged executors isolated and target-restricted. Do not grant these privileges to the demo API or classifier.

An injected pod-path delay might affect HTTP clients but not ICMP probes of a regional target. An experiment must name the affected network hop and the protocols/observers that traverse it. Controlled replay of recorded/synthetic external observations can test the detector, but label that replay as synthetic; it is not evidence that live RIPE detected the injection.

## Run protocol

1. Create run_id; snapshot commit, image digest, infrastructure, policy, model, source/probe cohort and baseline mode.
2. Verify control-plane reachability, target allowlist, baseline health, load-client clock sync and cleanup access.
3. Warm up under fixed offered load; record normal internal/external metrics.
4. Start a bounded fault and record true start time, target and parameters in the experiment recorder.
5. Record classifier decision, proposed action, actual actuator acknowledgement and user-request outcome timestamps separately.
6. Expire/clear fault, observe sustained recovery, then restore the known baseline routing/capacity.
7. Export raw results + manifest to S3; calculate outcome metrics; repeat independent runs and randomize order.

Runner death must not leave unbounded faults. Real executors need TTL/reconciliation and startup cleanup, not just a browser timer. The included local gateway expires faults itself and loses them on restart. Keep ground-truth labels out of inference features.

## Baselines

Primary comparison: fixed configuration; health-check failover; internal-only adaptive/HPA; AdaptiveNet external + internal telemetry. Add Route53 latency routing as a separate routing baseline if the project scope permits. Keep infrastructure, maximum replicas, offered workload and fault conditions comparable. Do not let a hidden automatic router/health failover turn a nominally static baseline into an adaptive one without documenting it.

Use external HTTP clients independent of the stable demo frontend server. The starter Next.js proxy measures requests originating from the frontend server, not the visitor's entire direct network path. For cloud evaluation, use real multi-location load clients against the actual service front door, or direct browser requests with appropriately scoped CORS if that is the chosen client model.

Measure success rate, p50/p95/p99 end-to-end latency, detection time, action time, recovery time, resource/replica minutes, observed traffic share, unnecessary scaling and route flaps. Include all failed/timed-out requests rather than excluding them from latency/availability conclusions. Report repeated runs and variability; lower average latency alone does not establish better resilience.

## Model protocol

Start rules-only; collect healthy, overload, network, critical, mixed and insufficient-data examples. Split by whole runs/time/probe cohorts, not random rows from adjacent windows. Keep a held-out scenario/cohort to test generalization. Compare macro-F1/confusion matrix, calibration and decision impact. If the model does not improve over rules, report that result rather than making the pipeline depend on unproven predictions.
