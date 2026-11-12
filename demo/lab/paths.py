"""Where external observers send their requests.

``edge`` (default, demo/compose.yaml): the demo's emulated internet path.
``gateway`` (demo/compose.lab-overlay.yaml): the group's existing local lab
gateway, so faults injected from its dashboard (gateway latency / HTTP errors)
are what the external observers see. Per-region requests use the gateway's
token-protected ``/admin/probe/{region}``, which applies the same faults.
In gateway mode the RIPE stand-in measures gateway-path reachability: an HTTP
error counts as a lost probe, which real ICMP loss would not (see DEMO.md).
"""
from __future__ import annotations

import os


class ObserverPaths:
    def __init__(self, mode: str | None = None):
        self.mode = mode or os.environ.get("PATH_MODE", "edge")
        if self.mode not in ("edge", "gateway"):
            raise ValueError("PATH_MODE must be edge or gateway")
        base = os.environ.get("EDGE_URL" if self.mode == "edge" else "OBSERVER_GATEWAY_URL",
                              os.environ.get("GATEWAY_URL", ""))
        self.base = base.rstrip("/")
        self.token = os.environ.get("LAB_TOKEN", "")

    def http(self, scope: str) -> tuple[str, dict]:
        if self.mode == "edge":
            return f"{self.base}/http/{scope}", {}
        if scope == "cloudfront":
            return f"{self.base}/api/catalog", {}
        return f"{self.base}/admin/probe/{scope}", {"X-Lab-Token": self.token}

    def ping(self, region: str) -> tuple[str, dict]:
        if self.mode == "edge":
            return f"{self.base}/ping/{region}", {}
        return f"{self.base}/admin/probe/{region}", {"X-Lab-Token": self.token}
