import json
import subprocess
from types import SimpleNamespace
import pytest
from adaptivenet.controller.kubectl_connection import KubectlConnection
from adaptivenet.controller.kubernetes_hpa import KubernetesReadError

CONTEXT = "arn:aws:eks:ap-southeast-1:468354413374:cluster/adaptivenet-sg"

def config():
    return {"current-context": CONTEXT, "clusters": [{"cluster": {
        "server": "https://fixture.ap-southeast-1.eks.amazonaws.com"}}]}

def test_connection_pins_context_and_only_reads_allowlisted_paths():
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 15
        return SimpleNamespace(returncode=0, stdout=json.dumps(config() if "config" in args else {"items": []}))
    reader = KubectlConnection("sg", "fixture.kubeconfig", run=run)
    assert reader("/api/v1/namespaces/adaptivenet/pods", selector="app=demo-sg") == {"items": []}
    assert calls[-1][-1].endswith("?labelSelector=app%3Ddemo-sg")
    assert all(args[args.index("--context")+1] == CONTEXT for args in calls)
    with pytest.raises(KubernetesReadError, match="not_allowlisted"):
        reader("/api/v1/secrets")
    assert len(calls) == 2

@pytest.mark.parametrize("change", ["context", "insecure", "endpoint"])
def test_wrong_cluster_or_insecure_endpoint_is_rejected(change):
    data = config()
    if change == "context":
        data["current-context"] = "wrong"
    elif change == "insecure":
        data["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    else:
        data["clusters"][0]["cluster"]["server"] = "https://other.invalid"
    with pytest.raises(KubernetesReadError):
        KubectlConnection("sg", "fixture", run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(data)))

def test_permissions_error_does_not_expose_stderr():
    with pytest.raises(KubernetesReadError, match="connection_or_permission_failed") as exc:
        KubectlConnection("sg", "fixture", run=lambda *a, **k: SimpleNamespace(returncode=1, stderr="private", stdout=""))
    assert "private" not in str(exc.value)

def test_timeout_is_reported_as_connection_failure():
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 15)
    with pytest.raises(KubernetesReadError, match="timed_out"):
        KubectlConnection("us", "fixture", run=run)
