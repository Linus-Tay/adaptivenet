# ML teammates: start here

Model **input** is produced by `python scripts/model_handoff.py input --mode internal_only --out data/model-input.json`. `data/` is ignored by Git.

1. Read `docs/DATA_DICTIONARY.md` for units and missing values.
2. Use `snapshot_id` to tie every prediction to **exactly one** feature snapshot.
3. Write `predict(model_input: dict) -> dict` that returns `adaptivenet.proposal.v1` (`hold`, `investigate`, `shift_traffic`, `scale_out`). Do **not** call AWS from this function.
4. Use `python scripts/model_handoff.py demo` to see a non-trained *rule baseline* pass through policy review. This does **not** constitute trained ML.
5. Train with time-based splits and controlled fault labels; compare internal-only vs combined mode on matched eligible windows.

Code entry points:
- `services/adaptivenet/ingestion/handoff.py` — stable input builder + missing-value masks + conservative example.
- `services/adaptivenet/ingestion/decisions.py` — the **independent** controller review; never executes.
- `packages/contracts/src/model_handoff.ts` — TypeScript model-input shape for UI integration.

**Current limitations:** missing live RIPE data means combined mode is not ready; an HTTP p95 may be `null` even while app health and request counters are valid. Do not impute missing measurements as zero.
