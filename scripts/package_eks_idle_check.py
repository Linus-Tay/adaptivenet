"""Package the actual controller for an idle-only EKS integration test."""
from pathlib import Path
import base64
import io
import zipfile

ROOT = Path(__file__).resolve().parent.parent
HARNESS = r'''import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parent / "services"))
from adaptivenet.controller.kubectl_connection import KubectlConnection
from adaptivenet.controller.kubernetes_hpa import KubernetesReadError
from adaptivenet.controller.live_hpa import LiveHpa
from adaptivenet.controller.journal import ActionJournal
from adaptivenet.controller.safety import SafetyController

parser = argparse.ArgumentParser(description="Fixture proposal / real EKS idle controller test; writes forbidden")
parser.add_argument("--site", choices=("sg", "us"), required=True)
parser.add_argument("--kubeconfig", required=True)
args = parser.parse_args()

class IdleTestConnection(KubectlConnection):
    write_attempts = 0
    def patch_hpa(self, patch):
        self.write_attempts += 1
        raise KubernetesReadError("idle_test_forbids_writes")

try:
    connection = IdleTestConnection(args.site, args.kubeconfig)
    with tempfile.TemporaryDirectory(prefix="adaptivenet-idle-") as directory:
        journal = ActionJournal(Path(directory) / "actions.db")
        adapter = LiveHpa(connection, journal)
        now = time.time()
        ident = f"features-eks-idle-fixture-{int(now)}"
        snapshot = {"contract_version": "adaptivenet.features.v1", "snapshot_id": ident,
                    "computed_at_unix": now, "ready_for_model": True}
        proposal = {"contract_version": "adaptivenet.proposal.v1", "snapshot_id": ident,
                    "action": "scale_out", "affected_region": connection.region,
                    "confidence": .95, "model_id": "fixture-not-trained-ml",
                    "evidence": ["idle_integration_test_fixture"]}
        controller = SafetyController(journal, None, mode="eks", live_hpa=adapter)
        result = controller.step_scale(snapshot, proposal, compute_overloaded=False)
        observation = adapter.last_observation
        if observation is None:
            raise ValueError("controller_did_not_observe_eks")
        cpu, target = observation.cpu_current_pct, observation.cpu_target_pct
        passed = (type(cpu) is int and type(target) is int and 0 <= cpu < target
                  and result.state == "observing"
                  and result.reasons == ("compute_evidence_missing_or_stale",)
                  and result.action_id is None and connection.write_attempts == 0
                  and journal.pending() is None)
        print(json.dumps({"test": "eks_idle_controller_integration", "passed": passed,
                          "input_source": "fixture_proposal_and_snapshot; live_kubernetes_capacity",
                          "executed": False, "write_attempts": connection.write_attempts,
                          "controller": asdict(result), "observation": asdict(observation)}, indent=2))
        raise SystemExit(0 if passed else 1)
except (KubernetesReadError, ValueError, OSError) as exc:
    print(json.dumps({"test": "eks_idle_controller_integration", "passed": False,
                      "executed": False, "error": str(exc)}))
    raise SystemExit(1)
'''

def main():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("services/adaptivenet/__init__.py", "services/adaptivenet/ingestion/__init__.py",
                     "services/adaptivenet/ingestion/decisions.py", "services/adaptivenet/ingestion/fast_signals.py",
                     "services/adaptivenet/ingestion/witness.py"):
            archive.write(ROOT / name, name)
        for name in ("safety", "kubectl_connection", "kubernetes_hpa", "live_hpa", "emulated_hpa",
                     "evidence", "journal", "recovery", "reconcile", "scaling"):
            path = f"services/adaptivenet/controller/{name}.py"
            archive.write(ROOT / path, path)
        archive.writestr("services/adaptivenet/controller/__init__.py", "")
        archive.writestr("__main__.py", HARNESS)
    out = ROOT / "data/eks-controller-idle-check"
    out.mkdir(parents=True, exist_ok=True)
    (out / "controller-idle-check.pyz").write_bytes(buffer.getvalue())
    encoded = base64.b64encode(buffer.getvalue()).decode()
    for site in ("sg", "us"):
        command = (
            "(\n  runner=$(mktemp /tmp/adaptivenet-idle.XXXXXX.pyz) || exit 1\n"
            "  trap 'rm -f \"$runner\"' EXIT\n"
            "  base64 -d > \"$runner\" <<'CONTROLLER_BUNDLE'\n" + encoded +
            "\nCONTROLLER_BUNDLE\n  python3 -S \"$runner\" --site " + site +
            " --kubeconfig ~/adaptivenet-" + site + ".kubeconfig\n)\n")
        (out / f"check-{site}.txt").write_text(command.replace(chr(92) + chr(34), chr(34)), encoding="utf-8")
    print(out)

if __name__ == "__main__":
    main()
