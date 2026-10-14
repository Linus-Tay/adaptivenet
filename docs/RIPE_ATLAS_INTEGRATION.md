# RIPE Atlas collector (read-only, implemented)

1. Deploy a publicly reachable **pingable IP or hostname** (localhost is not reachable from Atlas probes). A running web application alone does not guarantee ICMP is allowed; verify the target/network policy.
2. In the RIPE Atlas UI, create one **ping** measurement per public region/endpoint using available Atlas credits. Alternatively, select existing measurements that genuinely target your endpoint. Save the measurement IDs. This version is read-only and does **not** provision measurements or spend credits.
3. Set `ATLAS_MEASUREMENTS=region-a=MEASUREMENT_ID,region-b=MEASUREMENT_ID` in your local environment; use real numeric IDs. Set `LAB_TOKEN` to a strong value.
4. Start API: `uvicorn adaptivenet.control_api:app --app-dir services --host 127.0.0.1 --port 8000`.
5. Run: `curl -H 'X-Lab-Token: YOUR_TOKEN' 'http://127.0.0.1:8000/telemetry/atlas/latest'`.
6. Check `sample_count`, timestamps, RTT, packet loss, stale/timeout statuses. Empty and stale results are **not** successful reachability measurements. RIPE latest is cached and can lag by approximately five minutes.

The API uses the RIPE Atlas `GET /api/v2/measurements/{id}/latest/` endpoint (no token necessary for public measurements). Atlas **ping** observations are the only currently normalized type. Do not point this adapter at traceroute/HTTP/DNS results and interpret RTT as HTTP latency. No automatic routing, AWS changes, or autoscaling occurs.

Next required work: connect result polling to a durable ingest store; implement internal Prometheus metrics, joined detector, guarded controller, IAM-scoped AWS actions, integration tests and a deployable cloud workload. Nothing in this document implies these exist already.
