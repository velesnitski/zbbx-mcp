from zbbx_mcp.client import ZabbixClient


class InstanceResolver:
    """Resolves which Zabbix client to use for a given request.

    Priority:
    1. Explicit instance name parameter
    2. Default (first configured) instance
    """

    def __init__(self, clients: dict[str, ZabbixClient]):
        if not clients:
            raise ValueError("At least one Zabbix instance must be configured.")
        self._clients = clients
        self._default = next(iter(clients))

    def resolve(self, instance: str = "") -> ZabbixClient:
        """Pick the right client based on instance name."""
        if instance:
            if instance not in self._clients:
                available = ", ".join(self._clients.keys())
                raise ValueError(
                    f"Unknown Zabbix instance '{instance}'. "
                    f"Available: {available}"
                )
            return self._clients[instance]

        return self._clients[self._default]

    @property
    def default_name(self) -> str:
        return self._default

    @property
    def instance_names(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def is_multi(self) -> bool:
        return len(self._clients) > 1
