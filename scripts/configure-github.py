"""Read bootstrap outputs and configure the existing GitHub repository; no secrets."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
def run(*args):
    return subprocess.check_output(args, cwd=root, text=True)
outputs = json.loads(run("terraform", "-chdir=infra/terraform/bootstrap", "output", "-json"))
repo = json.loads(run("gh", "repo", "view", "--json", "nameWithOwner"))["nameWithOwner"]
if outputs["github_repository"]["value"] != repo:
    raise SystemExit("Bootstrap's GitHub repository does not match this origin. Correct the configuration before enabling CI.")
subprocess.run(["gh", "variable", "set", "AWS_READY", "--body", "false", "--repo", repo], check=True, cwd=root)
roles = outputs["role_arns"]["value"]
values = {
    "AWS_ACCOUNT_ID": outputs["aws_account_id"]["value"],
    "AWS_REGION": outputs["aws_region"]["value"],
    "TF_STATE_BUCKET": outputs["state_bucket"]["value"],
    "AWS_IMAGES_ROLE_ARN": roles["images"],
    "AWS_TERRAFORM_PLAN_ROLE_ARN": roles["plan"],
    "AWS_TERRAFORM_APPLY_ROLE_ARN": roles["apply"],
}
for key, value in values.items():
    subprocess.run(["gh", "variable", "set", key, "--body", value, "--repo", repo], check=True, cwd=root)
subprocess.run(["gh", "variable", "set", "AWS_READY", "--body", "true", "--repo", repo], check=True, cwd=root)
print("Repository variables configured. Only main can assume the AWS roles. Run Infrastructure/plan before selecting apply.")
