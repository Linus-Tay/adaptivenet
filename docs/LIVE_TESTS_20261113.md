# Live AWS tests, 13 November 2026

Two live tests, approved by MY and the team in the project thread before they ran. Times are UTC.
Evidence files are kept locally on MY's Mac under `data/` (git ignores that folder). The paths are listed below.

## 1. CloudFront failover (Singapore service stop)

**What was done.** `scripts/aws_fault.py --region region-a --duration 60 --execute --confirm STOP-REGION-A`
stopped `adaptivenet-demo.service` on the Singapore EC2 through SSM. A systemd restore timer was set up
before the stop (SSM command `3cecc4ea-cf1d-4264-b6a1-1bef0096c545`, status Success, stop at 04:02:01).
`scripts/continuous_probe.py --run --interval 10 --count 3` sent 3 GETs every 10 s to CloudFront and to
each region directly, and recorded which region served each request.

**Result.** All 108 CloudFront requests (04:01:26 to 04:07:16) returned HTTP 200.

| Phase | CloudFront requests | Served by | Median latency |
| --- | --- | --- | --- |
| Before the stop | 12 | Singapore | 56.6 ms |
| During the stop (04:02:06 to 04:03:27) | 27 | Virginia | 531.8 ms |
| After the restore | 69 | Singapore | 59.7 ms |

* The first request served by Virginia came at 04:02:06, 5 s after the stop and within one probe cycle.
  The last successful direct Singapore request before the stop was at 04:01:56.
* Direct Singapore requests failed during the stop. CloudFront was back on Singapore from about 04:03:30,
  about 90 s after the stop: the 60 s timer plus service start-up.
* This was CloudFront's own origin-group failover on 5xx (`infra/terraform/environments/cloudfront/main.tf`).
  The AdaptiveNet controller did not act, because there is no AWS traffic actuator. This is the first
  run that shows which origin served customers during an outage. The 23 Oct and 9 Nov runs showed only HTTP 200.

Evidence: `data/live-failover-20261113/` (`http_witness.jsonl`, `ssm-invocation.json`, `fault-events.jsonl`, `README.md`).

## 2. EKS scale-up (Singapore cluster `adaptivenet-sg`)

**What was done.** `scripts/eks_load_test.py` started one bounded in-cluster load Job (8 workers, up to
120 requests/s for 90 s against the internal catalogue, 120 s deadline) at 04:20:16. The safety controller
(`SafetyController(mode="eks")`) watched live HPA metrics with a fixture scale-out proposal. A separate
`kubectl get hpa,pods` watch ran every 5 s.

**Result.**

| Time | HPA CPU (target 60%) | Pods |
| --- | --- | --- |
| 04:20:16 | 2% | 2 |
| 04:20:57 | 81% | 2 to 3 (HPA event "New size: 3") |
| 04:21:08 | 97% | 3 |
| 04:21:25 | 73% | 3 to 4 (HPA event "New size: 4") |
| 04:21:37 to 04:22:10 | 73% to 52% | 4 ready |
| 04:22:34 | 2% (load ended) | 4 |
| 04:27:23 | 2% | 2 (scaled back down after the 300 s stabilisation window) |

* Load Job: 10,762 successful requests, 0 failed, 90 s.
* Kubernetes' own HPA scaled 2 to 4 pods in about 30 s. The HPA minimum and maximum were never changed,
  and the run ended at 2/6 with 2 pods.
* The AdaptiveNet controller did not request capacity. Its rule needs 3 consecutive fresh metric readings
  with CPU at or above target, and it got only one (`qualifying` 1 at 04:21:48) before the HPA had already
  brought CPU under 60%. The test script reports `no_controller_action_qualified`, which is the expected
  safe outcome when the native HPA keeps up.
* Cleanup note: the script's own Job delete failed (`kubectl_connection_or_permission_failed`). The Job had
  already completed. It was deleted manually with kubectl at about 04:23, and the Job TTL would have removed it anyway.

Evidence: `data/live-eks-scale-20261113/` (`observations.jsonl`, `summary.json`, `load-job.json`,
`load-job-log.txt`, `hpa-events.txt`, `final-state.txt`), `data/live-eks-scale-stdout.jsonl`, and the
5 s watch in `data/live-eks-watch-20261113/watch.txt`.

## Access change made for test 2

Before this test, the EKS public endpoint accepted only one address (`public_access_cidrs` in
`infra/terraform/modules/observability-region/main.tf`). With MY's approval, MY's Mac's public IP was
added as a second `/32` on both `adaptivenet-sg` and `adaptivenet-us` with `aws eks update-cluster-config`
(both updates Successful). The original address stays. Terraform was not edited, so a future
`terraform apply` will remove the extra address again. This is a known, deliberate drift until the team
decides to keep or remove it.

## What these tests do not show

* One run of each test, not a statistical result.
* The confidence rules (`services/adaptivenet/controller/confidence.py`) run in the lab controller only.
  Neither live test exercised them.
* Virginia was not stopped, and no scale test ran on `adaptivenet-us`.
