"""Offline checks for the queue-only remote-state Terraform bootstrap."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_sqs_terraform.sh"

def _bash_works() -> bool:
    # Windows ships a bash.exe WSL launcher that fails when no Linux distro is installed.
    if shutil.which("bash") is None:
        return False
    try:
        return subprocess.run(["bash", "-c", "exit 0"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _bash_works(), reason="no working bash (plain Windows)")
def test_sqs_bootstrap_bash_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

def test_sqs_bootstrap_isolated_target_and_remote_state_guards():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'dev/terraform.tfstate' in text
    assert 'use_lockfile=true' in text
    assert 's3api head-bucket' in text
    assert 'get-caller-identity' in text
    assert 'aws_sqs_queue.dead_letter' in text
    assert 'aws_sqs_queue.telemetry' in text
    assert 'aws_sqs_queue_redrive_allow_policy.dead_letter' in text
    assert 'show -json' in text
    assert 'REFUSING unexpected Terraform change' in text
    assert 'apply -input=false' in text
    assert 'terraform -chdir="$TF_DIR" destroy' not in text
    assert 'terraform -chdir="$TF_DIR" state rm' not in text
    assert 'terraform -chdir="$TF_DIR" import' not in text

def test_backend_is_shared_and_terraform_lockfile_retained():
    backend=(ROOT/"infra/terraform/environments/dev/backend.tf").read_text(encoding="utf-8")
    example=(ROOT/"infra/terraform/environments/dev/backend.hcl.example").read_text(encoding="utf-8")
    assert 'backend "s3"' in backend
    assert 'dev/terraform.tfstate' in example
    assert 'use_lockfile = true' in example


def test_cross_platform_provider_lock_is_generated_from_official_registry_before_readonly_init():
    script=SCRIPT.read_text(encoding="utf-8")
    assert "providers lock" in script
    assert "-platform=darwin_arm64" in script
    assert "-platform=linux_amd64" in script
    assert "registry.terraform.io/hashicorp/aws" in script
    assert script.index("providers lock") < script.index("init -reconfigure")
    assert "6\\.12\\.0" in script
    assert "-lockfile=readonly" in script
    assert "-upgrade" not in script
    assert "-lockfile=force" not in script
