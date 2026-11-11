# Step 5.3 — Evidence-grounded ML evaluation report

The report compiler **does not collect data, train models, infer labels or execute AWS actions**. It reads local JSON reports, writes one Markdown summary, and optionally renders SVG bars showing **real counts** from the audit. The performance chart is deliberately **not generated** until all three native classifiers have trained and have valid held-out metrics **and** the paired reporter records successful evaluation.

## Install and run (Windows PowerShell)

```powershell
Expand-Archive -Path "$env:USERPROFILE\Downloads\AdaptiveNet_Step5_3_Report_Compiler.zip" -DestinationPath . -Force
python -m pytest -q ml/team_starter/tests/test_step5_3_report_compiler.py
python -m pytest -q ml/team_starter/tests
python -m pytest -q
python -m ml.team_starter.report_compiler --audit data/ml-results/native/availability-audit.json --evaluation data/ml-results/native/evaluation-report.json --paired data/ml-results/native/paired-incident-report.json --shadow data/ml-results/native-baseline-shadow-review.json --out data/ml-results/native/ML_EVALUATION_DRAFT.md
Get-Content data/ml-results/native/ML_EVALUATION_DRAFT.md
```

The command reads from the existing files. Missing files render as unavailable rather than zero. It refuses to overwrite an evidence input file. No audit, model file, or incident label is modified.

## What is and is not ready

- **Ready:** count summary, source limitation audit, three-arm training status, independently reviewed policy-result summary, reproducibility commands, clearly scoped unfinished work.
- **Conditional:** three-arm held-out macro-F1 plot and paired delay comparison once genuinely reviewed native incidents, trained models, and a valid matched test cohort exist.
- **Not acceptable:** copying an Edge Fault or SOFI benchmark result into a native AdaptiveNet score, using synthetic fixture labels for ML performance, treating a reviewed SSM action alone as an outage, treating repeated RIPE polls as independent observations, or representing a shadow hold as actual mitigation.

The generated status chart uses reported `journal_status_counts` from the audit, which may include partial polls due to a source issue or authentication expiration. It is **not** a fault-detection performance graphic.

## File safety

No `ml/predictor.py`, controller, reader, collector, production reviewer, or any infrastructure files are changed. Generated reports should stay within `data/ml-results/` (gitignored) during development. All new tests use synthetic fixtures and are separate from scientific results.
