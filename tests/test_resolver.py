import pytest
from unittest.mock import MagicMock

from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.resolver import InstanceResolver


def _make_client(url: str) -> ZabbixClient:
    cfg = ZabbixConfig(url=url, token="test")
    client = ZabbixClient(cfg)
    return client


class TestInstanceResolver:
    def test_single_instance(self):
        clients = {"default": _make_client("https://zabbix.example.com")}
        resolver = InstanceResolver(clients)
        assert resolver.default_name == "default"
        assert resolver.is_multi is False
        assert resolver.resolve() is clients["default"]

    def test_explicit_instance(self):
        clients = {
            "prod": _make_client("https://zabbix.prod.com"),
            "staging": _make_client("https://zabbix.staging.com"),
        }
        resolver = InstanceResolver(clients)
        assert resolver.resolve("prod") is clients["prod"]
        assert resolver.resolve("staging") is clients["staging"]

    def test_unknown_instance_raises(self):
        clients = {"prod": _make_client("https://zabbix.prod.com")}
        resolver = InstanceResolver(clients)
        with pytest.raises(ValueError, match="Unknown Zabbix instance"):
            resolver.resolve("nonexistent")

    def test_empty_clients_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            InstanceResolver({})

    def test_multi_instance(self):
        clients = {
            "a": _make_client("https://a.com"),
            "b": _make_client("https://b.com"),
        }
        resolver = InstanceResolver(clients)
        assert resolver.is_multi is True
        assert set(resolver.instance_names) == {"a", "b"}

    def test_default_is_first(self):
        clients = {
            "first": _make_client("https://first.com"),
            "second": _make_client("https://second.com"),
        }
        resolver = InstanceResolver(clients)
        assert resolver.default_name == "first"
        assert resolver.resolve() is clients["first"]
