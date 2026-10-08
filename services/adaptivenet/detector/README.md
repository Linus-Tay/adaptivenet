# detector

Status: planned integration boundary, not implemented.

Consume SQS, deduplicate into PostgreSQL and commit before delete. Assemble event-time windows from shared persisted data; no per-worker independent windows.
