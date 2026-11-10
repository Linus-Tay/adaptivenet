# Step 5.2 — Shadow-only acceptance testing

This is a test-only ML deliverable. It does not modify the installed model, predictor,
independent reviewer, telemetry pipeline, AWS, SQS, Terraform, Docker or any route.

The new `tests/test_step5_2_shadow_acceptance.py` contains **synthetic fixtures**
only. Nine tests (and four subtests) verify:

- `hold` and `investigate` proposals are validated but do not execute.
- Even a valid, high-confidence `shift_traffic` proposal is only **eligible for
  operator review**; it is **never executed**.
- `scale_out` is rejected because its adapter is unimplemented.
- Wrong snapshot IDs, contracts, invalid confidence and unknown actions fail closed.
- Expired feature snapshots cannot justify routing changes.
- The existing `offline_cli shadow` path runs the independent reviewer and
  refuses to save a shadow result when snapshot identity does not match.

Run from the AdaptiveNet repository root:

```powershell
python -m pytest -q ml/team_starter/tests/test_step5_2_shadow_acceptance.py
python -m pytest -q ml/team_starter/tests
python -m pytest -q
```

For a **real snapshot plus conservative rule baseline** demonstration (NOT a
trained-model result), the existing program can also be run:

```powershell
python scripts/model_handoff.py demo --mode combined --out data/ml-results/native-baseline-shadow-review.json
```

Expected safety fields in its `decision_review` object:
`approved_for_execution: false`, `executed: false`, `effective_action: hold`.
`policy_valid` depends on the input; it need not always be true.

The synthetic fixture tests are *engineering acceptance*, not a native
accuracy estimate. A truly trained native model, verified real incident data,
performance comparison, and real trained shadow invocation remain blocked
until sufficient independently reviewed aligned episodes are available.

This package changes no production files and leaves Git staging deferred.

## Step 7.9 - Verified baseline shadow demonstration

Date: 9 November 2026

Command: `python scripts/model_handoff.py demo --mode combined --out data/ml-results/step7_9-baseline-shadow-review.json`

| Field | Verified result |
|---|---|
| Input ready | true |
| Proposal contract | adaptivenet.proposal.v1 |
| Model ID | conservative_rule_baseline_v1 |
| Proposed action | hold |
| Baseline confidence | 0.8 |
| Policy valid | true |
| Approved for execution | false |
| Executed | false |
| Effective action | hold |

The independent reviewer returned a valid, non-executed hold decision.

This demonstrates the conservative rule baseline, not trained ML.
The confidence value is not a calibrated probability.
All three native classifiers remain not_trained.
Private demonstration evidence remains under the ignored data/ directory.
