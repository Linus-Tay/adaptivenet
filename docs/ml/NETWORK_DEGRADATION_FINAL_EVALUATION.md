# AdaptiveNet — Final network-degradation ML evaluation (Step 8)

**Scope:** two-class current-condition network-degradation study; November 2026.
**Status:** completed offline ML engineering evaluation with explicitly limited scientific acceptance.
**Safety:** shadow-only; no model-triggered AWS writes, traffic shifting, scaling or automatic mitigation.

## Relationship to earlier application-failure and public-dataset reports

This is a **new native network-degradation experiment**, distinct from the earlier 118-row
healthy-versus-application-failure experiment (9 episodes) in `docs/ml/FINAL_ML_EVALUATION.md`.
Do **not** merge their confusion matrices or claim they are the same native holdout.
Earlier public Edge Fault and SOFI results remain separate benchmark evidence, not equivalent live-network validation.

## Verified original observations and experimental design

- Binary dataset: **158 original labeled snapshot rows**, **10 reviewed episodes**: 4 healthy and 6 network-degradation.
- Episode split: **{'train': 6, 'validation': 1, 'test': 2, 'excluded_for_embargo': 1}**. Test: **34 fault-only rows**; held-out healthy test rows: **0**.
- Identical held-out snapshot IDs across all three models: **verified**.
- Original reviewed dataset SHA-256: `4dbb959071d100d8520ad6aa24bdaa373997fe498cc3858d2c1e7b6b2c248297`.
- Frozen models: SHA-256 reverified from saved training artifacts; preprocessing preserves missingness/freshness.
- Detection horizon: **0 seconds** (present-condition classification, not forecasting).

## Three-arm paired network-degradation evaluation

| Model | Fault recall | Fault-class F1* | Fault-only accuracy† | Abstentions | Verified healthy held-out |
|---|---:|---:|---:|---:|---:|
| `aws_only` | 50.0% | 66.7% | 50.0% | 17/34 | 0 |
| `aws_ripe` | 50.0% | 66.7% | 50.0% | 17/34 | 0 |
| `aws_ripe_http` | 50.0% | 66.7% | 50.0% | 17/34 | 0 |

*Fault F1 is conditional on the **fault-only** holdout, not a population-wide F1 or healthy-condition quality measure.
†Abstentions are counted as wrong in fault-only accuracy.

### Incident detection delay and paired interpretation

| Test episode | AWS-only | AWS+RIPE | AWS+RIPE+HTTP |
|---|---:|---:|---:|
| `network-trial-31b8c3c6ebf6` | 27.880s | 27.880s | 27.880s |
| `network-trial-a7e5356532f8` | miss / abstain | miss / abstain | miss / abstain |

**Scientific conclusion:** external RIPE/HTTP signals did **not** demonstrate earlier or more reliable detection than AWS-only on this eligible held-out study. This is a negative or inconclusive comparison, **not** proof that external signals are useless.

## Prospective shadow integration and safety

- **7 new observed snapshots** spanning **332.6 s**; **21 trained predictions**, **0 abstentions**.
- Every prospective response passed independent policy review; `approved_for_execution=false`, `executed=false`, `effective_action=hold`.
- Local authenticated HTTP smoke: `STEP7_LOCAL_AUTHENTICATED_SERVICE_VERIFIED_SHADOW_ONLY`. Unauthorized inference rejected and 3 model groups returned.
- Local Docker image: `sha256:ddb51268aed9ecde827846aea3f0e5a1b291c34473a90e5476b66168a34e1234`.
- Local container startup/health/auth check: **EPHEMERAL_DOCKER_SERVICE_VERIFIED**.
- ECR push, actual EKS deployment and authenticated in-cluster telemetry: **NOT VERIFIED**.

## Required scientific limitations

1. **No globally verified healthy held-out test**; false alarms/day, operational specificity and healthy-class precision are **not estimable** from these predictions.
2. **No separately verified recovery class**; recovery classification and mitigation-to-recovery delays are not estimable.
3. The combined six-condition native dataset (`healthy`, `application_failure`, `network_degradation`, `partial_outage`, `transient`, `recovery`) has not been validated as one study.
4. The local HTTP journal in earlier audits was predominantly 15-second cadence; independent original 5-second HTTP evidence must not be inferred from it.
5. Two held-out fault episodes and a short prospective run are insufficient to demonstrate robust field accuracy, calibrated confidence or long-term reliability.
6. The Virginia-only normal-window approval is **not** global healthy ground truth. Single-probe RIPE loss is not proof of a new regional outage.
7. A previous Step 4 AWS/CloudTrail management-event audit had incomplete coverage; do not describe it as comprehensive.

## Verification and reproducibility

- Step 8 full repository pytest: **PASS** (131 passed, 2 warnings in 17.06s).
- Verified Git branch during this run: `feature/ml-team-starter-20261104`.
- Existing saved reports: `data/ml-results/native/step6-network-validation/STEP6_REPORT.json` and `.md`, `data/ml-results/native/step7-shadow-handoff/STEP7_LOCAL_SMOKE.json`.
- Run: `python -m pytest -q` then `python step8_finish/finalize_step8.py --skip-tests` (requires prior passing test evidence; skip does not certify new test success).
- Raw telemetry, credentials, model binaries and private evidence remain under ignored/private `data/`; **do not commit**.

## Final assessment

**Engineering delivery:** complete for the frozen local two-class network detector and the shadow-only service. **Scientific acceptance:** conditional/incomplete for six-class coverage, global false alarms, recovery and generalization. **Deployment:** not certified in EKS. **Repository handoff:** requires reviewed Git staging/commit/push/PR acceptance.

_Generated from local evidence at 2026-11-10T18:59:11.210345+00:00._
