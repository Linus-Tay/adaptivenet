"""Simple Git staging safety tests; no network or GitHub credentials."""
import importlib.util
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "check_push_safety.py"
spec = importlib.util.spec_from_file_location("check_push_safety", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_sensitive_paths():
    for path in (".env", ".env.production", "data/adaptivenet-ingestion.db",
                 "infra/terraform/terraform.tfstate", "infra/terraform/.terraform/foo",
                 "private.pem", "terraform.tfvars", ".aws/credentials"):
        assert module.is_sensitive_path(path), path


def test_safe_paths():
    for path in (".env.example", "README.md", "docs/TEAM_HANDOFF.md",
                 "tests/test_model_handoff.py", "infra/terraform/main.tf"):
        assert not module.is_sensitive_path(path), path
