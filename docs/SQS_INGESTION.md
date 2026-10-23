# AdaptiveNet SQS ingestion — implementation and operations

This implements **the ingestion → Amazon SQS → detector/feature-worker segment** of the system architecture. RIPE Atlas, AMP and CloudFront are existing external measurements; this change does not create RIPE probes, EKS clusters, models or actuators.

## Actual runtime architecture

~~~text
RIPE Atlas measurement REST API  ──┐
AWS AMP PromQL (SG + Virginia) ──────┼── collector (ONE polling process)
CloudFront CloudWatch (global) ──────┘         │
                                     normalized Observation objects
                                               │ source timestamps preserved
                                         durable local JSON outbox
                                               │ SendMessage
                                    Amazon SQS standard queue (SG)
                                      adaptivenet-dev-telemetry
                                               │ ReceiveMessage long poll
                                      detector/feature worker
                                               │ validate+deduplicate
                                               │ persist SQLite events
                                               │ materialize snapshot
                                               │ ACK (DeleteMessage) ONLY on success
                                     existing model_input.v1 contract
                                               │
                                     future ML model → future controller
                                                          ↳ mitigation NOT built

                           Failed processing: SQS visibility timeout (120s)
                           → retries (max receives 5) → dedicated 14-day DLQ
                           CloudWatch: oldest message + DLQ depth alarms
~~~

**Important accuracy:** the existing Atlas integration **polls the RIPE Atlas REST API**, not a pushed, live RIPE streaming subscription. SQS buffers completed source polls. This matches the diagram's functional decoupling but is not proof of an EKS-hosted streaming service.

## What changed

- Shared existing source collection in `collect_source_poll()` instead of duplicating AMP, Atlas or CloudFront collectors.
- Added `adaptivenet.telemetry_poll.v1`: versioned one-message-per-poll envelope; original source timestamps, status/error details, per-observation SHA256 IDs, whole-batch checksum; bounded to 200KB.
- The **producer** durably stages a complete poll under ignored `data/sqs-outbox/`, retries staged messages first, and deletes the local staged copy only after SQS returns a `MessageId`.
- The **consumer** validates the entire envelope before writing, uses the existing SQLite SHA256 event ID to suppress SQS redelivery duplicates, creates the same `adaptivenet.features.v1` snapshot, and only then calls SQS `DeleteMessage`.
- SQS Standard delivery is **at least once and not guaranteed in order**. Retrying a message can recompute a snapshot, but cannot insert duplicate observation IDs. No bad message is acknowledged; SQS retries and moves it to the DLQ per configured redrive policy.
- `live_monitor.py --sqs` starts producer + consumer + unchanged read-only dashboard. Without `--sqs`, the original **direct SQLite** collector still works as a fallback. **Do not run both source-collection modes simultaneously**.
- The existing SG Terraform queue/DLQ are reused. New least-privilege *attachable* producer and consumer IAM policies and CloudWatch backlog/DLQ alarms are defined; AWS identities/roles are **not modified automatically** by merely cloning or running tests.

## Prerequisites and initial verification

Use the existing checked-out feature branch and virtualenv:

~~~bash
cd "$(git rev-parse --show-toplevel)"
git fetch origin
git switch feature/sqs-telemetry-ingestion  # or --track origin/feature/sqs-telemetry-ingestion
git pull --ff-only

source .venv/bin/activate
python -m pip install -e '.[queue]' awscurl certifi

export AWS_PROFILE=adaptivenet-admin
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
aws sso login --profile adaptivenet-admin
aws sts get-caller-identity --profile adaptivenet-admin

# Check that the dev queue already EXISTS before running producers:
aws sqs get-queue-url --queue-name adaptivenet-dev-telemetry \
  --region ap-southeast-1 --profile adaptivenet-admin

# Existing source health (does not publish SQS messages):
python scripts/ingestion_doctor.py --profile adaptivenet-admin
python scripts/cloudfront_doctor.py --lookback-minutes 180
python scripts/ripe_measurements.py status
~~~

If the queue lookup says it doesn't exist, **the Terraform resource is only code**, not deployed. First inspect the existing `infra/terraform/environments/dev` state (remote backend/workspace, state ownership, Terraform plan) before applying. If the queue already exists outside this Terraform state, **import it** instead of trying to create a duplicate. Do not run an unreviewed `terraform apply`: dev Terraform also manages other resources.

Terraform files:
- `infra/terraform/environments/dev/main.tf`: standard queue, 120-second visibility, 4-day retention, encrypted with SQS-managed keys; DLQ maxReceiveCount=5 and 14-day retention.
- `infra/terraform/environments/dev/telemetry_iam_and_alarms.tf`: scoped publish/consume IAM policies; alarms for oldest message >5min and DLQ nonzero.

The two IAM policies **are not magically assigned to everybody**. A local `adaptivenet-admin` SSO session needs `GetQueueUrl`, `GetQueueAttributes`, `SendMessage`, `ReceiveMessage` and `DeleteMessage` against this queue. For future EKS deployment, attach the separate policy to its specific workload identity after cluster/IAM integration; never bake AWS access keys into a pod.

## Start SQS-backed telemetry with ONE command

Stop any old `ingestion.py watch` and `live_monitor.py` processes first, to avoid parallel direct collection. Then:

~~~bash
# SQS-backed ingestion, read-only observer, independent real HTTP witness
# and two separate local Next.js UIs:
python scripts/live_monitor.py --sqs --traffic --with-ui
~~~

This launches (1) the SQS producer every 120 seconds, (2) an independent SQS consumer that long-polls for up to 20 seconds, (3) the existing read-only observatory on `127.0.0.1:8766`, (4) optional real HTTP witness, and (5) local workload and operator UIs on `:3001` and `:3000`. No fault is automatically injected.

To run each part independently in different terminals:

~~~bash
python scripts/sqs_ingestion.py publish-once --profile adaptivenet-admin
python scripts/sqs_ingestion.py consume-once --profile adaptivenet-admin
python scripts/sqs_ingestion.py status --profile adaptivenet-admin
# For continuous collection, use publish-watch and consume-watch instead.
~~~

**Your Mac must remain running** for this local producer, consumer and observer. The queue buffers messages if the consumer stops, until the four-day queue retention expires. An EKS-hosted producer/consumer (as in the target diagram) is a **separate deployment**, not automatically fulfilled by this code.

## Verify actual SQS end-to-end, not just healthy dashboards

1. Run `publish-once`. Observe `"enqueued": 1`, or confirm an outbox file remains for recovery if SQS is unreachable.
2. Run `status`. Check messages available/inflight. Counters are approximate; zero is plausible if a consumer is active.
3. Run `consume-once` with no other consumer running. Expect `"processed": 1` and a `snapshot_id`. Then check `http://127.0.0.1:8766/v1/features/latest`.
4. Publish the *same serialized message twice* in an isolated mock test: observation IDs dedupe, second database insert count stays zero. `pytest tests/test_sqs_ingestion.py -q` does this offline.
5. Stop the consumer but keep producer running. SQS backlog should grow; restarting consumer should drain it. Simulate failed send with the mocked test; `data/sqs-outbox/` retains the unsent poll until it can be replayed.
6. Validate retry/DLQ behavior in **a controlled non-production queue**; don't deliberately poison the team's shared queue. CloudWatch alarms should show backlog age and DLQ depth, but these alarms have **no SNS notification target configured**.

**Never conclude that a button click or SQS `SendMessage` response proves model inference or mitigation.** This stage only guarantees the presence of the ingestion code path and integrity checks; actual deployment, IAM grants, queue state and EKS scheduling require separate AWS verification.

## Caveats / boundaries

- Each poll is one full message; if serialized data exceed the conservative 200KB limit, the producer raises instead of dropping observations. Add explicit chunk+reassembly protocol before supporting much larger RIPE datasets.
- Source timestamps are not adjusted during redelivery. Old polls cannot magically satisfy current freshness checks; outdated CloudFront last-known values remain display-only. Standard SQS can deliver messages out of order.
- Queue producer uses disk-spooled polls for publish failures. If the Mac disk itself fails or the collection machine disappears before sending, local outbox data can be lost; for fully managed/cloud-native ingestion host the producer on EKS with durable requirements or use a managed streaming ingestion service.
- SQLite is still the single-worker local feature store; it is **not a multi-replica shared EKS database**. EKS migration will need a shared durable DB, e.g. PostgreSQL, plus workload-specific IAM and deployment.
- ML teammates continue using the same `model_input.v1` contract and `scripts/export_ml_dataset.py`. No re-training data schema migration is forced by SQS.
