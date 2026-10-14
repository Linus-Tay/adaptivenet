# Feature dictionary — `adaptivenet.features.v1`

Features are grouped under `regions["region-a"|"region-b"]["features"]`; percentages are in **0–100**, not 0–1.

| Key | Unit | Meaning / when null |
|---|---|---|
| `app_up` | 0 / 1 | Instrumented app health sample; `null` if AMP absent/stale |
| `cpu_busy_pct` | % | CPU busy estimated from node idle time, 5m rate |
| `memory_available_bytes` | bytes | Host available memory |
| `memory_total_bytes` | bytes | Host total memory |
| `memory_available_pct` | % | 100 × available / total, only with valid pair |
| `node_load1` | load | Host 1-minute load average |
| `http_rps` | requests/s | 5m rate of `/api/catalog` counters; **0 is valid** |
| `http_5xx_rps` | requests/s | 5m server-error rate; no observed counter => null |
| `http_error_ratio` | 0–1 | 5xx RPS / all RPS, null if total RPS is 0 or unavailable |
| `http_p95_ms` | milliseconds | Catalog HTTP histogram p95; null if histogram rates insufficient |
| `cpu_busy_ewma_pct` | % | Rolling exponentially weighted CPU mean |
| `http_p95_ewma_ms` | milliseconds | Rolling HTTP p95 mean; null if no valid p95 |
| `http_p95_robust_z` | dimensionless | Robust z-score from historical observed p95; null if insufficient baseline |
| `network_rtt_p50_ms` | milliseconds | Median RIPE ICMP ping RTT from distinct reporting probes |
| `network_rtt_p95_ms` | milliseconds | RIPE ICMP RTT p95 from reporting probes |
| `packet_loss_pct` | % | Mean probe-level ICMP packet loss |
| `ripe_probes_reporting` | count | Distinct RIPE probes with fresh loss results; zero can be valid |

## Quality and timing

| Field | Description |
|---|---|
| `ready_for_internal_only_model` | Both regions have the pipeline's **minimum** required fresh internal signals. Not proof all optional features exist. |
| `ready_for_model` | Both regions have minimum internal signals **and** fresh RIPE quorum. |
| `ready_for_automatic_action` | Always `false` in this version. |
| `quality.internal` | `fresh` or `missing`; required internal set currently app health + both memory quantities. |
| `quality.external` | `fresh`, `loss_only`, or `insufficient_or_stale`. |
| `quality.required_missing` | Array of absent **required** internal raw signals; optional p95 may still be null. |
| `provenance` | Source timestamps/age per feature; RIPE probe age/quorum where available. |
| `source_errors` | Query errors and `no_series`; the latter is *missing data*, not necessarily a provider outage. |

The implementation's freshness limits: AWS 180s; RIPE 2100s; at least 3 distinct fresh RIPE probes with RTT for external `fresh`. Up to 30s positive AWS clock skew is tolerated and annotated; RIPE timestamps are never freshened.

**Do not build live predictors from an old stored snapshot merely because it has a `ready` flag.** The model adapter rechecks the snapshot's wall-clock age (180 seconds maximum by default).

`comparison` always follows **region-a minus region-b**, or `null` if a side is unavailable. A negative CPU delta means region-a CPU is lower; it does not by itself justify shifting traffic.

The HTTP histogram and RIPE ping use different protocols; absolute latency values are **not interchangeable**.
