import asyncio
import json
import time
from itertools import count

import httpx

from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.rollback import SNAPSHOT_CONFIG, Action, RollbackLog

__all__ = ["ZabbixClient"]


class ZabbixClient:
    """Async client for Zabbix JSON-RPC API (6.0+).

    Performance notes:
    - httpx.AsyncClient keeps a connection pool with keepalive by default
    - HTTP/2 enabled for multiplexed requests over a single TCP connection
    - Connection limits tuned for typical Zabbix API usage patterns
    """

    def __init__(self, config: ZabbixConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            headers={"Content-Type": "application/json"},
            base_url=config.url,
        )
        self._request_id = count(1)
        self.rollback_log = RollbackLog()
        self._cache: dict[str, tuple[float, list]] = {}  # key -> (timestamp, data)

    def _get_cached(self, key: str, ttl: float = 60.0) -> list | None:
        """Get cached result if fresh."""
        if key in self._cache:
            ts, data = self._cache[key]
            if time.monotonic() - ts < ttl:
                return data
        return None

    def _set_cache(self, key: str, data: list) -> None:
        """Cache a result."""
        self._cache[key] = (time.monotonic(), data)

    @property
    def frontend_url(self) -> str:
        """Return the Zabbix frontend URL (strip API path)."""
        url = self._config.url.rstrip("/")
        for suffix in ("/api_jsonrpc.php", "/api"):
            if url.endswith(suffix):
                url = url[:-len(suffix)]
        return url

    async def close(self):
        """Close the underlying HTTP client and release connections."""
        await self._client.aclose()

    async def call(self, method: str, params: dict | None = None) -> dict | list:
        """Execute a Zabbix JSON-RPC API call.

        Args:
            method: Zabbix API method (e.g., 'host.get', 'problem.get')
            params: Method parameters

        Returns:
            The 'result' field from the JSON-RPC response.
        """
        # Methods that must be called without auth (Zabbix requirement)
        no_auth = method in ("apiinfo.version", "user.login")

        request_id = next(self._request_id)
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": request_id,
        }
        if not no_auth:
            payload["auth"] = self._config.token

        resp = await self._client.post("/api_jsonrpc.php", json=payload)
        resp.raise_for_status()
        body = resp.json()

        if "error" in body:
            err = body["error"]
            msg = err.get("data", err.get("message", "Unknown error"))
            if isinstance(msg, str) and len(msg) > 200:
                msg = msg[:200] + "..."
            # Friendly message for permission errors
            if isinstance(msg, str) and "no permissions" in msg.lower():
                raise ValueError(
                    f"No permissions for {method}. "
                    "Your API token may lack the required role (Admin or Super Admin). "
                    "Check token permissions in Zabbix UI → User settings → API tokens."
                )
            raise ValueError(f"Zabbix API error ({err.get('code', '?')}): {msg}")

        return body.get("result", {})

    async def snapshot(self, object_type: str, object_id: str) -> dict:
        """Fetch the current state of an object for rollback purposes."""
        cfg = SNAPSHOT_CONFIG.get(object_type)
        if not cfg:
            return {}

        id_field = cfg["id_field"]
        params = {
            f"{id_field}s": [object_id],
            "output": "extend",
        }
        # Add extra select params if configured
        extra = cfg.get("get_params_extra")
        if extra:
            params.update(json.loads(extra))

        result = await self.call(cfg["get_method"], params)
        if result and isinstance(result, list):
            snap = result[0]
            # Scrub secret macro values (type=1) from snapshots
            if object_type == "usermacro" and snap.get("type") == "1":
                snap["value"] = "[REDACTED]"
            return snap
        return {}

    async def snapshot_and_record(
        self,
        action: Action | str,
        object_type: str,
        object_id: str,
        description: str = "",
    ) -> None:
        """Take a snapshot of an object and record it in the rollback log."""
        if isinstance(action, str):
            action = Action(action)
        snap = {}
        if action in (Action.UPDATE, Action.DELETE):
            try:
                snap = await self.snapshot(object_type, object_id)
            except (ValueError, httpx.HTTPError, KeyError):
                snap = {}  # Record without snapshot rather than fail the operation
        self.rollback_log.record(action, object_type, object_id, snap, description)

    def record_create(self, object_type: str, object_id: str, description: str = "") -> None:
        """Record a create operation (no snapshot needed, just the ID for undo)."""
        self.rollback_log.record(Action.CREATE, object_type, object_id, {}, description)

    async def call_many(self, calls: list[tuple[str, dict | None]]) -> list:
        """Execute multiple Zabbix API calls in parallel.

        Args:
            calls: List of (method, params) tuples

        Returns:
            List of results in the same order as the input calls.
        """
        return await asyncio.gather(
            *(self.call(method, params) for method, params in calls)
        )
