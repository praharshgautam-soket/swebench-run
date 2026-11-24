"""Controller-side view of the metering proxy (see assets/meter_server.py).

The proxy counts what the model API reports. This module reads those counters back
through the Kubernetes API's service proxy, so the controller needs no network path into
the cluster beyond what `kubectl` already has.
"""
from __future__ import annotations

import inspect
import json
from typing import Protocol


class MeterClient(Protocol):
    def reset(self, key: str) -> None: ...
    def usage(self, key: str) -> dict | None: ...
    def healthy(self) -> bool: ...


def stats_to_usage(stats: dict | None) -> dict:
    """Map the proxy's counters to the usage fields the runner records.

    `steps` is the number of successful model calls: each agent turn is one call.
    Returns {} when nothing was measured, so unknown stays unknown (shown as n/a).
    """
    if not stats or not stats.get("calls"):
        return {}
    return {
        "input_tokens": stats.get("prompt_tokens", 0),
        "output_tokens": stats.get("completion_tokens", 0),
        "steps": stats.get("ok_calls", 0),
    }


class KubeMeterClient:
    """Talks to the proxy through /api/v1/namespaces/<ns>/services/<name>:<port>/proxy/..."""

    def __init__(self, api_client, namespace: str, name: str, port: int, token: str):
        self.api, self.ns, self.name, self.port, self.token = api_client, namespace, name, port, token

    def _call(self, method: str, tail: str) -> bytes:
        return self._request(method, f"admin/{self.token}/{tail}")

    def _request(self, method: str, tail: str) -> bytes:
        path = f"/api/v1/namespaces/{self.ns}/services/{self.name}:{self.port}/proxy/{tail}"
        if "url" in inspect.signature(self.api.call_api).parameters:
            # kubernetes client >= 30: call_api takes the full URL and applies no auth itself.
            conf = self.api.configuration
            headers = {}
            for setting in conf.auth_settings().values():
                if setting.get("in") == "header" and setting.get("value"):
                    headers[setting["key"]] = setting["value"]
            resp = self.api.call_api(
                method, conf.host.rstrip("/") + path, header_params=headers,
                body=None, post_params=None, _request_timeout=30,
            )
            if getattr(resp, "data", None) is None and hasattr(resp, "read"):
                resp.read()
            return resp.data
        # Older clients: resource path first, auth applied by the client.
        resp = self.api.call_api(
            path, method, auth_settings=["BearerToken"], response_type=None,
            _preload_content=False, _return_http_data_only=True, _request_timeout=30,
        )
        return resp.data

    def reset(self, key: str) -> None:
        self._call("POST", f"reset/{key}")

    def usage(self, key: str) -> dict | None:
        return json.loads(self._call("GET", f"usage/{key}"))

    def healthy(self) -> bool:
        """True if the proxy answers its health check right now."""
        try:
            return json.loads(self._request("GET", "healthz")).get("ok") is True
        except Exception:  # noqa: BLE001
            return False
