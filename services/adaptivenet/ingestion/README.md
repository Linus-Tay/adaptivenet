# Ingestion — implemented, read-only

**Start at the [root README](../../../README.md)** or the [ML/controller handoff](../../../docs/TEAM_HANDOFF.md).

| Module | Responsibility |
|---|---|
| `sources.py` | Read AWS AMP and RIPE; validate/normalize provider responses |
| `models.py` | Frozen `Observation` dataclass; stable SHA-256 event ID |
| `store.py` | SQLite WAL with deduplication and time indexes |
| `features.py` | Freshness rules, per-probe aggregation, EWMA, regional comparisons |
| `handoff.py` | Deterministic model input; missingness mask; safe rule example |
| `decisions.py` | Validate proposals, always return shadow-only result |
| `cli.py` | Collect/watch/export/serve/review commands |
| `api.py` | Local `127.0.0.1:8766` read-only feature API |

**Safe boundaries:** Python scripts don't reconfigure AWS. RIPE probes can stop and old samples are marked stale. Readiness is a data gate; no model output causes traffic changes. Missing p95 is **not** 0 milliseconds.

Tests: `python -m pytest -q`; collection: `python scripts/ingestion.py once --profile adaptivenet-admin`.
