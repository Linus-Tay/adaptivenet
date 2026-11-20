# Documents

**Start here:** [GROUP03_INSTRUCTIONS.pdf](GROUP03_INSTRUCTIONS.pdf), the step-by-step testing guide.

## Design

| Document | What it covers |
|---|---|
| [ARCHITECTURE_AND_ML_2026.md](ARCHITECTURE_AND_ML_2026.md) | architecture, data rates, Docker vs native runtime |
| [SQS_INGESTION.md](SQS_INGESTION.md) | collector, outbox, SQS queue, dead-letter queue, alarms |
| [END_TO_END.md](END_TO_END.md) | proposal → safety reviewer → decision, confidence rules |
| [SAFETY_CONTROLLER_TASK4.md](SAFETY_CONTROLLER_TASK4.md) | safety controller and EKS checks (task 4 of the project plan) |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | feature fields |
| [TEAM_HANDOFF.md](TEAM_HANDOFF.md) | model input and output contracts |
| [ENV_SETUP.txt](ENV_SETUP.txt) | AWS sign-in and RIPE Atlas key, for group members |

## Results

| Document | What it covers |
|---|---|
| [EXPERIMENT_SUMMARY.md](EXPERIMENT_SUMMARY.md) | one page on every experiment and its limits |
| [ml/RIPE_CAMPAIGN_RESULTS.md](ml/RIPE_CAMPAIGN_RESULTS.md) | the 20-trial campaign: AWS-only 0/20, AWS + RIPE 13/20 |
| [ml/live/WEDNESDAY_RESULTS.md](ml/live/WEDNESDAY_RESULTS.md) | the 14–18 Nov live run and the ML models on unseen faults |
| [LIVE_TESTS_20261113.md](LIVE_TESTS_20261113.md) | live CloudFront failover and EKS scale-up tests |
| [ACTION_REPLAY_CAMPAIGN.md](ACTION_REPLAY_CAMPAIGN.md) | the campaign replayed through the safety controller |

## ML protocols and lab notebook

The files in [ml/](ml/) are our lab notebook: each study's protocol, written before its data, and its report. The dated file names (for example `LIVE_TESTS_20261113`, `WEDNESDAY_RESULTS`) are the day each test ran. They are kept unchanged as evidence for the numbers in the article.
