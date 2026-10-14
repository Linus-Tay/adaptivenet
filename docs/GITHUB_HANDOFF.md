# GitHub team workflow (Mac)

These steps update a **code repository only**. They do not deploy AWS infrastructure or alter Terraform state.

## 1. Inspect the existing repository

```bash
cd "$HOME/Downloads/adaptivenet 5"
git status --short
git branch --show-current
git remote -v
```

If `git remote -v` shows a valid `origin`, **reuse it**. Do not run `git init` or create a second unrelated repository.

If there is **no origin**, create a private, **empty** GitHub repository (no generated README) and attach it:

```bash
git remote add origin git@github.com:YOUR_ORG_OR_USER/adaptivenet.git
# Alternatively: https://github.com/YOUR_ORG_OR_USER/adaptivenet.git
```

If the folder has no `.git` directory yet, run `git init -b main` *only then*, and add the new remote. GitHub authentication uses Git Credential Manager / `gh auth login` for HTTPS, or a registered SSH key. **Never place passwords or tokens inside remote URLs.**

## 2. Branch, test, and stage only your changes

```bash
git switch -c feature/ingestion-ml-handoff  # or use another unused branch name
source .venv/bin/activate
python -m pytest -q

# Include the *existing working ingestion code* so teammates can reproduce it.
git add README.md docs/TEAM_HANDOFF.md docs/DATA_DICTIONARY.md docs/GITHUB_HANDOFF.md \
  ml/README.md services/adaptivenet/ingestion \
  scripts/ingestion.py scripts/ingestion_doctor.py scripts/amp_metric_doctor.py \
  scripts/catalog_probe.py scripts/model_handoff.py scripts/check_push_safety.py \
  packages/contracts/src/ingestion.ts packages/contracts/src/model_handoff.ts \
  tests/test_ingestion*.py tests/test_model_handoff.py tests/test_check_push_safety.py

git diff --cached --check
python scripts/check_push_safety.py --staged
git diff --cached --stat
git status --short
```

**Check all staged filenames**. If the installer created `README.proposed.md` (because you customized an existing README), review and merge it first; then stage the final root `README.md`. Do not add `.env`, `.aws/`, local `.db`/`.sqlite`, `.terraform/`, Terraform state, downloaded ZIPs, credentials, raw customer data or private logs.

## 3. Commit and push

```bash
git commit -m "Document ingestion pipeline and add safe ML controller handoff"
git push -u origin feature/ingestion-ml-handoff
```

Open the GitHub repo, switch to `feature/ingestion-ml-handoff`, and use **Compare & pull request**. Ask a teammate to review the interface and tests; merge after CI passes.

If `git push` says `repository not found`, verify `git remote -v` and repository access. If it says `permission denied (publickey)`, configure your GitHub SSH key or use HTTPS authentication. If it says `non-fast-forward`, **fetch and reconcile**; don't force-push shared work.

If the project already uses `main` as the collaboration branch, still prefer a PR rather than committing fixes directly to `main`.

## 4. Rules for classmates

- Branch per feature; small PRs; include tests and an updated data dictionary for contract changes.
- Keep schema versions explicit; reject unrecognized major versions.
- Never commit trained model artifacts larger than necessary; store reproducible training outputs externally with checksums.
- Do not make unreviewed `terraform apply`, Route 53 changes or AWS permission changes from CI.
- Use `git pull --ff-only` when staying up to date; resolve divergence in a separate branch or PR.
