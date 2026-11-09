"""HTTP route adapter for the local gateway simulator."""
from ipaddress import ip_address

import httpx


class GatewayRoute:
    def __init__(self, client: httpx.Client, token: str):
        if client.base_url.scheme != "http" or not ip_address(client.base_url.host).is_loopback:
            raise ValueError("gateway must use an HTTP loopback address")
        self._client = client
        self._headers = {"X-Lab-Token": token}

    def read_weight(self) -> int:
        try:
            response = self._client.get("/admin/state", headers=self._headers, follow_redirects=False)
            response.raise_for_status()
            weight = response.json()["region_a_percent"]
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise OSError("gateway route read failed") from exc
        if type(weight) is not int or not 0 <= weight <= 100:
            raise OSError("gateway returned an invalid route weight")
        return weight

    def request_weight(self, *, original: int, target: int) -> None:
        try:
            response = self._client.put(
                "/admin/routes",
                json={"expected_region_a_percent": original, "region_a_percent": target},
                headers=self._headers,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OSError("gateway route update failed") from exc
