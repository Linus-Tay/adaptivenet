# AdaptiveNet: AWS account, GitHub and delivery setup

Prepared 7 October 2026. Work from the `adaptivenet/` repository root. The AWS region for this foundation is Singapore (`ap-southeast-1`). The later second workload region is N. Virginia (`us-east-1`), matching the proposal diagram. Live external monitoring is deferred.

## What this milestone creates

| Stage | Resources / result |
|---|---|
| Repository | Private GitHub repo, source history, pull-request CI |
| Human access | Your SSO setup identity for the project AWS account |
| Human-run bootstrap | Private/versioned Terraform state bucket, 3 ECR repositories, GitHub OIDC provider (or reuse existing), 3 scoped GitHub IAM roles |
| Image workflow | Services, dashboard, demo-web images with immutable full-commit tags |
| Development infrastructure workflow | Encrypted telemetry SQS + DLQ, private/versioned S3 artifact bucket |
| Later milestone | VPC/EKS, ALBs, Route53, PostgreSQL, telemetry integrations and controller |

Publishing an image does not deploy or host the application. Creating a queue does not connect the collector. No AWS resources or remote GitHub repository have been created by generating this package.

## 1. Create or choose the AWS project account

Use an AWS account you own/administer, ideally dedicated to this project. If you use an existing personal account, inspect existing resources and IAM Identity Center before creating duplicates. If this is an AWS Academy/Learner Lab account, use the lab's assigned identity and ask the administrator about IAM/OIDC/EKS restrictions; the bootstrap assumes you can manage IAM roles/providers.

For a new account, sign up at https://aws.amazon.com/ and complete email, contact/payment and account verification. These personal steps stay with you. Add MFA to root. Do not create root access keys. Create a monthly cost budget and alerts at amounts you are comfortable spending. An alert does not stop usage. ECR/S3 storage and SQS usage can cost money even though this foundation avoids EKS/NAT/ALB costs.

## 2. Create your human setup identity

Recommended: IAM Identity Center with an **organization instance and multi-account permissions enabled**. An account instance alone cannot grant AWS account access through permission sets.

1. Open IAM Identity Center in your chosen home region (use Singapore if creating it now).
2. Enable an organization instance. For a standalone account, follow AWS's organization-creation steps if offered. If already in an organization, use the existing organization/admin; do not create a second identity setup blindly.
3. Use the Identity Center directory, create your user (for example `linus`), and accept its invitation yourself.
4. Configure MFA for access.
5. Create a permission set using AWS managed `AdministratorAccess`. Name it `AdaptiveNetSetupAdmin` and use a short session such as one hour.
6. Under AWS accounts, select only the project account and assign your user that permission set.
7. Sign in through the AWS access portal as that user. Root is no longer your development login.

This identity is intentionally powerful enough to create the initial project IAM, state and registry resources. It is your human setup identity; it is not stored in the application and is not given to GitHub. For an AWS organization, prefer project workloads in a member account rather than the organization management account.

GitHub receives these distinct, restricted roles automatically from bootstrap:

| Role | Allowed work |
|---|---|
| `adaptivenet-github-images` | Push/read the three `adaptivenet/*` ECR repositories |
| `adaptivenet-github-plan` | Read the dev state/resources; acquire/release its state lock |
| `adaptivenet-github-apply` | Update the dev state, named telemetry queues and project artifact bucket |

All three roles trust only your exact GitHub `OWNER/REPO`, the `main` branch and AWS's OIDC audience. CI on pull requests has no AWS identity. These roles cannot create EKS or IAM resources. Their policies must be deliberately expanded when those deployment modules are added.

## 3. Install your Mac tools and sign in

Install Homebrew if you do not already have it, using https://brew.sh/. Then:

```sh
brew install git gh awscli node@24 python@3.12 helm
brew tap hashicorp/tap
brew install hashicorp/tap/terraform
```

If Homebrew's `node@24` is keg-only, follow its printed PATH instructions. Terraform must be at least 1.13.5 and below 2; CI uses 1.13.5. The included provider locks include provider checksums; do not delete them just because your Mac uses ARM. Docker Desktop is optional for AWS setup and needed for local Docker Compose: https://www.docker.com/products/docker-desktop/.

Check:

```sh
git --version
gh --version
aws --version
terraform version
node --version
```

Authenticate:

```sh
aws configure sso --profile adaptivenet-admin
aws sso login --profile adaptivenet-admin
aws sts get-caller-identity --profile adaptivenet-admin
```

During configuration: choose an SSO session name, paste your access-portal start URL, enter the Identity Center region, keep the normal `sso:account:access` registration scope, select the project account and `AdaptiveNetSetupAdmin`, and default the CLI service region to `ap-southeast-1`. Verify the returned account ID before any apply.

```sh
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
```

Do not paste AWS keys, SSO tokens or GitHub access tokens into chat, `.env`, Terraform files or GitHub secrets. GitHub-to-AWS uses short-lived OIDC credentials.

## 4. Create the repository and push this source

Extract the archive and enter its `adaptivenet/` directory. Set your own commit identity using your real or GitHub-provided noreply email:

```sh
git config --global user.name "Linus Tay"
git config --global user.email "YOUR_GITHUB_COMMIT_EMAIL"
bash scripts/create-repository.sh adaptivenet
```

The script uses the authenticated GitHub username, creates a **private** repository, commits the source and pushes `main`. It refuses to overwrite an existing origin. It does not store credentials. Use a different final name argument if `adaptivenet` already exists.

Manual website alternative: create an empty private repository at https://github.com/new. Do not initialize its README, .gitignore or license since this source already exists. Then initialize/commit this directory and add the exact clone URL GitHub shows as `origin`, followed by `git push -u origin main`. Do not run the creation script against a repo already created on the website.

First push runs tests/builds. Image publication and infrastructure execution stay disabled until `AWS_READY=true` is configured later. No AWS secrets are needed for source CI.

Use feature branches and PRs. Configure a main-branch rule requiring CI where your GitHub plan supports it. Limit who can push to main: trusted main workflows can request the project OIDC roles. Full action-SHA pinning and additional deployment protection can be added later; current workflows pin action major versions and exact Terraform/provider versions.

## 5. Bootstrap AWS once from your SSO session

From the repository root:

```sh
export AWS_PROFILE=adaptivenet-admin
aws sts get-caller-identity
cp infra/terraform/bootstrap/terraform.tfvars.example infra/terraform/bootstrap/terraform.tfvars
```

Edit that local file with your actual account ID and exact `OWNER/adaptivenet`. Keep `aws_region = "ap-southeast-1"`. If a GitHub OIDC provider already exists in IAM → Identity providers, put its ARN in `existing_github_oidc_provider_arn`; otherwise leave the optional line commented. IAM has one provider per URL per account, so avoid duplicate creation.

```sh
terraform -chdir=infra/terraform/bootstrap init
terraform -chdir=infra/terraform/bootstrap plan -out=bootstrap.tfplan
terraform -chdir=infra/terraform/bootstrap show bootstrap.tfplan
```

Review: one state bucket + supporting settings, three ECR repositories/lifecycle policies, three scoped roles/policies, and a GitHub provider only if not reused. There should be **no EKS cluster, VPC, NAT gateway, ALB or DNS change**.

Apply the exact reviewed plan:

```sh
terraform -chdir=infra/terraform/bootstrap apply bootstrap.tfplan
terraform -chdir=infra/terraform/bootstrap output
```

Applying a saved plan performs its actions without a second approval prompt. If it fails, inspect the error and current state before re-planning; do not assume nothing was created. Terraform formatting and workflow checks pass, but full provider validation could not complete in this environment and policies have not been exercised in your account; account policies/quotas can still block them.

## 6. Move bootstrap state into the bucket it created

The first bootstrap used local state because the bucket did not exist yet. Migrate it before team use. Use the actual `state_bucket` output for the uppercase placeholder below:

```sh
cat > infra/terraform/bootstrap/backend-bootstrap.tf <<'HCL'
terraform {
  backend "s3" {
    bucket       = "YOUR_STATE_BUCKET_OUTPUT"
    key          = "bootstrap/terraform.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
HCL
```

Replace `YOUR_STATE_BUCKET_OUTPUT` in the generated file, then:

```sh
terraform -chdir=infra/terraform/bootstrap init -migrate-state
terraform -chdir=infra/terraform/bootstrap state list
```

Confirm copying the local state when Terraform asks. `backend-bootstrap.tf`, local plans and state files are ignored by Git. Keep a private backup until migration succeeds; never commit state. On a new laptop, recreate the backend configuration and use `init` with SSO to read existing state—do not create duplicate resources. CI only accesses `dev/terraform.tfstate`, never the bootstrap state. Bootstrap's state bucket has `prevent_destroy`.

## 7. Configure GitHub's AWS variables

```sh
python3 scripts/configure-github.py
```

This reads non-secret Terraform outputs and sets:

- `AWS_ACCOUNT_ID`, `AWS_REGION`, `TF_STATE_BUCKET`
- `AWS_IMAGES_ROLE_ARN`
- `AWS_TERRAFORM_PLAN_ROLE_ARN`, `AWS_TERRAFORM_APPLY_ROLE_ARN`
- `AWS_READY=true` only after variable setup succeeds

This version works without paid GitHub environment features: trust is bound to `main`, and infrastructure changes are explicitly started with workflow_dispatch. The apply role is not an account administrator. GitHub environments/reviewers are an optional later enhancement; if added, the OIDC trust subject must change to the environment name and branch restrictions must be configured on that environment.

Check Settings → Secrets and variables → Actions → **Variables**. There should be no AWS access-key secret.

## 8. Publish images

Open GitHub → Actions → Publish images → Run workflow → main. Subsequent successful CI runs from pushes to main also trigger publication once AWS_READY is true.

Three repositories are created in ECR:

- `adaptivenet/services` (same image, selected Python module at runtime)
- `adaptivenet/dashboard`
- `adaptivenet/demo-web`

Images are built for `linux/amd64`; choose matching EKS nodes later. Tags are full Git commit SHAs and cannot be overwritten. Rerunning a publish skips existing tags. The digest is recorded in the job summary. No mutable `latest` tag is used. The current scan-on-push reports findings but is not yet a vulnerability-based release gate. Regional ECR replication and workload deployment are deferred.

## 9. Plan and apply the development foundation

GitHub → Actions → Infrastructure → Run workflow → main → operation **plan**. Inspect the plan summary. It should create two SQS queues, the redrive-allow policy, and the project S3 artifact bucket/settings.

When ready, run the workflow again with operation **apply**. That run generates a fresh plan and its apply job consumes that exact saved plan. No changes run on PRs or normal pushes. There is no separate human review pause between those two jobs in this baseline; choosing apply authorizes the displayed foundation scope. If you need to approve the exact newly generated plan before execution, use the local commands below or add a supported protected environment.

Output: queue URLs and artifact bucket. This is delivery/telemetry infrastructure only, not a public hosted AdaptiveNet instance.

Equivalent local execution:

```sh
cp infra/terraform/environments/dev/backend.hcl.example infra/terraform/environments/dev/backend.hcl
# Edit backend.hcl to use your real state_bucket output.
export TF_VAR_aws_account_id=YOUR_12_DIGIT_ACCOUNT_ID
export TF_VAR_aws_region=ap-southeast-1
terraform -chdir=infra/terraform/environments/dev init -backend-config=backend.hcl
terraform -chdir=infra/terraform/environments/dev plan -out=dev.tfplan
terraform -chdir=infra/terraform/environments/dev show dev.tfplan
terraform -chdir=infra/terraform/environments/dev apply dev.tfplan
```

The example backend uses S3 native locking (`use_lockfile=true`); no DynamoDB lock table is needed. Use the same state key from local and CI, and avoid simultaneous apply operations.

## 10. Run and register your demo

Use `docs/SETUP.md` for local startup. Open http://localhost:3000/applications and register the two local endpoints. Registration persists across control-API restarts (SQLite; Compose named volume). The UI shows internal telemetry, external telemetry, routing and scaling as disconnected. The experiment controls remain a separate local simulator and are not yet bound to newly registered apps.

Next milestone: implement PostgreSQL + ingestion/SQS consumption and connect internal metrics. Then provision actual regional EKS workloads, configure Route53, and bind the registered endpoints to the real controller. RIPE/external measurement setup comes after that, as requested.

## Troubleshooting boundaries

- Wrong AWS account: stop and select the correct SSO profile; `allowed_account_ids` rejects mismatches.
- OIDC AccessDenied: check OWNER/REPO, `main` branch, audience, provider ARN and role ARN. Never fix it with wildcard trust or AdministratorAccess on CI.
- Terraform AccessDenied: identify the denied API and its resource, then update the scoped policy using human bootstrap. Do not attach account-wide policies to get around it.
- State lock busy: check for a real running apply before force-unlocking.
- Repo already exists: clone/use that repository or choose another name; no force-push is needed.
- Domain/custom TLS, EKS or RIPE errors: those services have not been provisioned by this milestone.

References: [AWS Identity Center setup](https://docs.aws.amazon.com/singlesignon/latest/userguide/getting-started.html), [IAM best practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html), [GitHub OIDC for AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [S3 Terraform backend](https://developer.hashicorp.com/terraform/language/backend/s3), [GitHub repository creation](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github).
