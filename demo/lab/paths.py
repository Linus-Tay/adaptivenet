"""Where external observers send their requests: through the lab gateway.

Faults injected on the gateway (latency, HTTP errors, packet loss) are what the
external observers see. Per-region requests use the token-protected
``/admin/probe/{region}``, which applies the same faults. The RIPE stand-in
therefore measures gateway-path reachability: an HTTP error counts as a lost
probe, which real ICMP loss would not (see DEMO.md).
"""
from __future__ import annotations

import os


class ObserverPaths:
    def __init__(self):
        self.base = os.environ.get("OBSERVER_GATEWAY_URL", os.environ.get("GATEWAY_URL", "")).rstrip("/")
        self.token = os.environ.get("LAB_TOKEN", "")

    def http(self, scope: str) -> tuple[str, dict]:
        if scope == "cloudfront":
            return f"{self.base}/api/catalog", {}
        return f"{self.base}/admin/probe/{scope}", {"X-Lab-Token": self.token}

    def ping(self, region: str) -> tuple[str, dict]:
        return f"{self.base}/admin/probe/{region}", {"X-Lab-Token": self.token}
