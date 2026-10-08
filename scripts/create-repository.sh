#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v gh >/dev/null || { echo "Install GitHub CLI first: brew install gh"; exit 1; }
gh auth status >/dev/null
owner="$(gh api user --jq .login)"
repository="${1:-adaptivenet}"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Pass a repository name, not a URL."; exit 1; }
if [[ ! -d .git ]]; then git init -b main; fi
if git remote get-url origin >/dev/null 2>&1; then
  echo "An origin already exists; no repository was created or pushed."
  exit 1
fi
if ! git config user.name >/dev/null || ! git config user.email >/dev/null; then
  echo 'Set your Git commit name/email first; see docs/AWS_GITHUB_SETUP.md.'
  exit 1
fi
if [[ -n "$(git ls-files --cached -- '*.tfstate' '*.tfstate.backup' '.env' '*.tfvars' 'backend.hcl')" ]]; then
  echo 'Sensitive local configuration is already tracked. Untrack it before continuing.'
  exit 1
fi
git add .
if ! git diff --cached --quiet; then git commit -m "Prepare AdaptiveNet application registry and AWS delivery foundation"; fi
gh repo create "$owner/$repository" --private --source=. --remote=origin --push
printf 'Created private repository: https://github.com/%s/%s\n' "$owner" "$repository"
