"""Authenticated, read-only Kubernetes transport through an explicit kubeconfig."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode

from .kubernetes_hpa import KubernetesReadError
from .scaling import WorkloadTarget

TARGETS = {
    "sg": ("region-a", "ap-southeast-1", WorkloadTarget("adaptivenet-sg", "adaptivenet", "demo-sg", "demo-sg")),
    "us": ("region-b", "us-east-1", WorkloadTarget("adaptivenet-us", "adaptivenet", "demo-us", "demo-us")),
}
ACCOUNT = "468354413374"

class KubectlConnection:
    def __init__(self, site: str, kubeconfig: str, *, run=subprocess.run):
        if site not in TARGETS:
            raise ValueError("site must be sg or us")
        self.region, aws_region, self.target = TARGETS[site]
        self.context = f"arn:aws:eks:{aws_region}:{ACCOUNT}:cluster/{self.target.cluster}"
        self._base = ["kubectl", "--kubeconfig", str(Path(kubeconfig).expanduser()),
                      "--context", self.context, "--request-timeout=10s"]
        self._run = run
        # Check the named context without printing certificates or credentials.
        config = self._call(["config", "view", "--minify", "-o", "json"])
        if config.get("current-context") != self.context:
            raise KubernetesReadError("unexpected_kubernetes_context")
        clusters = config.get("clusters", [])
        if len(clusters) != 1:
            raise KubernetesReadError("invalid_kubernetes_cluster_configuration")
        from urllib.parse import urlsplit
        cluster = clusters[0].get("cluster", {})
        endpoint = urlsplit(cluster.get("server", ""))
        if (endpoint.scheme != "https" or not endpoint.hostname
                or not endpoint.hostname.endswith(f".{aws_region}.eks.amazonaws.com")
                or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
                or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
                or cluster.get("insecure-skip-tls-verify") is True):
            raise KubernetesReadError("invalid_or_insecure_eks_endpoint")

    def _call(self, args: list[str]) -> dict:
        try:
            result = self._run(self._base + args, capture_output=True, text=True,
                               timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KubernetesReadError("kubectl_unavailable_or_timed_out") from exc
        if result.returncode:
            # kubectl stderr can include private paths/addresses; do not echo it.
            raise KubernetesReadError("kubectl_connection_or_permission_failed")
        try:
            obj = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise KubernetesReadError("invalid_kubectl_response") from exc
        if not isinstance(obj, dict):
            raise KubernetesReadError("invalid_kubectl_response")
        return obj

    def __call__(self, path: str, *, selector: str | None = None) -> dict:
        t = self.target
        allowed = {
            f"/apis/autoscaling/v2/namespaces/{t.namespace}/horizontalpodautoscalers/{t.hpa}",
            f"/apis/apps/v1/namespaces/{t.namespace}/deployments/{t.deployment}",
            f"/api/v1/namespaces/{t.namespace}/pods",
            f"/apis/metrics.k8s.io/v1beta1/namespaces/{t.namespace}/pods",
        }
        if path not in allowed:
            raise KubernetesReadError("resource_not_allowlisted")
        query = "?" + urlencode({"labelSelector": selector}) if selector is not None else ""
        return self._call(["get", "--raw", path + query])

    def patch_hpa(self, patch: list[dict]) -> dict:
        """Write only this connection's allowlisted HPA using an atomic JSON patch."""
        return self._call(["patch", "hpa", self.target.hpa, "--namespace", self.target.namespace,
                           "--type=json", "-p", json.dumps(patch, allow_nan=False), "-o", "json"])
