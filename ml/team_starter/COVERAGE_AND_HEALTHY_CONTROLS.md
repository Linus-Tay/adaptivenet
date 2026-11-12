# Six-class coverage and healthy-control false-alarm rate

The schema (`verified_labels.LABELS`), dataset builder, splitter and metrics
already support all six classes and a false-alarms-per-day figure. What limits
the native study is **verified evidence**, not code. `coverage_audit.py` makes
that limit explicit and reproducible instead of leaving it to prose.

## Run the audit

```bash
python -m ml.team_starter.coverage_audit \
  --labels data/ml-results/native/<study>/verified_episodes.json \
  --dataset data/ml-results/native/<study>/dataset.json \
  --predictions data/ml-results/native/<study>/<arm>/test-predictions.json \
  --out data/ml-results/native/<study>/coverage-audit.json
```

`--dataset` and `--predictions` are optional. Exit code 0 means no blockers;
2 means at least one claim is not supported yet, and every blocker is printed.

The audit reports, for each class and each split (train / validation / test /
embargo-excluded): reviewed episodes, independent experiment runs, dataset rows
and verified hours. A class counts as *trainable and scorable* only with at
least 2 independent runs in train and 1 in test.

## False alarms per day

* Exposure is the **verified healthy interval duration** of held-out test
  episodes. It is not the first-to-last row span, which gives a one-row healthy
  episode zero exposure while still counting its alarms.
* An alarm is a transition into a non-healthy prediction inside a healthy
  episode. Abstentions are not alarms, and rows with no prediction are reported
  as `incomplete_predictions`. They are never treated as healthy.
* The rate comes with an exact (Garwood) Poisson 95% interval. Below 6 h of
  held-out healthy exposure, the status is `indicative_only`.
* With no healthy episode in the test block, the status is `not_estimable`. No
  number is produced.

## Native results

See [docs/ml/SIX_CLASS_AND_FALSE_ALARM_STUDY.md](../../docs/ml/SIX_CLASS_AND_FALSE_ALARM_STUDY.md) for the three-class study, the class-stratified split and the date confound.

## Evidence still required (as of 2026-11-12)

The current native study (10 episodes: 6 network degradation, 4 healthy) has no
healthy control in its held-out test block. So:

| Need | Why | How to collect it honestly |
|---|---|---|
| ≥1 healthy control in the **latest** chronological block, ideally ≥6 h in total | The test block is chronologically last, so the controls must be recorded after the latest incident | Passive capture only: run the collector/archiver with no fault actions scheduled. The reviewer confirms no SSM/fault commands ran in the window, the HTTP witness is all 2xx, and AMP `app_up=1`. Record the evidence refs. |
| `application_failure` in ≥3 runs (2 train + 1 test) | Earlier app-failure evidence is outside the unified dataset | Re-run service-stop experiments with a stable `experiment_run_id`. The label starts at the first **observed** failure, not at the button click. |
| `transient` and `recovery` phases | They only make sense inside an incident run | Label them in the same `experiment_run_id` as the incident they follow, using observed end/recovery times. |
| `partial_outage` | Needs ≥2 independent vantage points (validator-enforced) | Multi-probe RIPE Atlas evidence. The single HTTP witness cannot show this. |

Until these exist, report the model as a **binary healthy / network-degradation
detector** and state that its false-alarm rate is not yet estimable.
