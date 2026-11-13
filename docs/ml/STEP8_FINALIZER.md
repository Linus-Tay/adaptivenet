# AdaptiveNet — Step 8 minimal finalizer (Windows)

## What it does

- Verifies frozen original network dataset and all 3 model SHA256 values against Step 6.
- Verifies prospective shadow safety and Step 7 authenticated inference evidence.
- Corrects the **Step 7 source Dockerfile** `shadow` group collision idempotently. It does not rebuild the Docker image.
- Checks the local Docker image exists, starts a localhost-only ephemeral container, probes `/readyz` and confirms unauthenticated `/v1/infer` is rejected, then stops the container.
- Runs the **full repository pytest once**.
- Generates `docs/ml/NETWORK_DEGRADATION_FINAL_EVALUATION.md` and `docs/ml/STEP8_ML_HANDOFF.md` without overwriting the previous application-failure/public-benchmark report.
- Inventories changed Git paths and scans source candidates for common credential patterns; does NOT stage, commit, push or deploy anything.

## One paste (PowerShell, from repository root)

```powershell
Expand-Archive "$env:USERPROFILE\Downloads\AdaptiveNet_Step8_Final_Acceptance.zip" -DestinationPath . -Force
python -m unittest discover -s step8_finish -p test_step8.py -v
if ($LASTEXITCODE -ne 0) { throw 'Step 8 regression tests failed' }
python .\step8_finish\patch_docker_source.py
if ($LASTEXITCODE -ne 0) { throw 'Step 7 source Dockerfile patch failed' }
python .\step8_finish\finalize_step8.py
if ($LASTEXITCODE -ne 0) { throw 'Step 8 audit requires review; inspect printed output' }
```

Expected local acceptance `STEP8_LOCAL_ACCEPTANCE_READY_FOR_MANUAL_GIT_REVIEW` if every local gate passes, including Docker startup and full tests. This is **NOT** a GitHub submission or proof of EKS deployment.

### Final review before Git push

`data/ml-results/native/step8-finalization/STEP8_CANDIDATES.json` lists source/document changed files and blocked/unrelated files. Inspect it and review the complete Git diff. **Do not use `git add .` or stage `data/`.** When the reviewed changed-file list is accepted, explicitly stage individual ML code and report files, confirm `git diff --cached --check`, run a staged-content secret scan, commit and push the feature branch, then open a PR via team process. A human teammate must review the code and scientific limitations. The tool deliberately does not do remote writes or assume PR authority.

### Non-negotiable unresolved research scope

A successful Step 8 engineering handoff does **not** establish six-condition verified ground truth, global-healthy false alarms/day, statistically significant external-signal improvements, recovery-class accuracy, five-second original witness evidence, or EKS rollout. Do not change these facts in a submission to make the checker pass.
