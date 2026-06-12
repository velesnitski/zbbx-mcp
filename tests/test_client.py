"""Wire-format tests for ZabbixClient's 6.4 -> 7.2+ compatibility (ADR 055).

These drive ZabbixClient.call() through an httpx.MockTransport so we can
assert the exact JSON-RPC request it puts on the wire — auth moved from the
request body to the Authorization header, and the host-group selector /
returned property renamed — without needing a live Zabbix server.
"""

import json

import httpx

from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig


def _client_with_handler(handler):
    cfg = ZabbixConfig(url="https://zbx.example.com", token="test")
    client = ZabbixClient(cfg)
    # Swap the real pooled AsyncClient for one backed by a mock transport.
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=cfg.url,
        headers={"Content-Type": "application/json"},
    )
    return client


def _make_handler(captured, result):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        res = result(body.get("method")) if callable(result) else result
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": res}
        )

    return handler


class TestClientZabbix7Compat:
    """ADR 055 — auth-header + host-group selector translation."""

    async def test_authenticated_call_uses_bearer_header_not_body(self):
        captured: list = []
        client = _client_with_handler(_make_handler(captured, []))
        await client.call("host.get", {"output": ["hostid"]})
        req = captured[-1]
        body = json.loads(req.content)
        # Zabbix 7.2 rejects an `auth` body property entirely.
        assert "auth" not in body
        assert req.headers.get("Authorization") == "Bearer test"
        await client.close()

    async def test_apiinfo_version_sent_unauthenticated(self):
        # apiinfo.version must carry neither a body auth nor an auth header.
        captured: list = []
        client = _client_with_handler(_make_handler(captured, "7.4.9"))
        await client.call("apiinfo.version")
        req = captured[-1]
        assert "auth" not in json.loads(req.content)
        assert "Authorization" not in req.headers
        await client.close()

    async def test_selectgroups_translated_and_response_aliased(self):
        captured: list = []

        def result(method):
            return [{"hostid": "1", "host": "h", "hostgroups": [{"name": "G"}]}]

        client = _client_with_handler(_make_handler(captured, result))
        out = await client.call(
            "host.get", {"output": ["hostid"], "selectGroups": ["name"]}
        )
        sent = json.loads(captured[-1].content)["params"]
        # Request: 6.x selectGroups -> 7.2 selectHostGroups.
        assert sent.get("selectHostGroups") == ["name"]
        assert "selectGroups" not in sent
        # Response: 7.2 hostgroups aliased back to groups for the tool layer.
        assert out[0]["groups"] == [{"name": "G"}]
        await client.close()

    async def test_trigger_get_also_translated(self):
        captured: list = []
        client = _client_with_handler(
            _make_handler(captured, [{"triggerid": "9", "hostgroups": [{"name": "T"}]}])
        )
        out = await client.call("trigger.get", {"selectGroups": "extend"})
        sent = json.loads(captured[-1].content)["params"]
        assert sent.get("selectHostGroups") == "extend"
        assert out[0]["groups"] == [{"name": "T"}]
        await client.close()

    async def test_caller_params_dict_not_mutated(self):
        captured: list = []
        client = _client_with_handler(_make_handler(captured, []))
        params = {"output": ["hostid"], "selectGroups": ["name"]}
        await client.call("host.get", params)
        # The translation must copy, never mutate the caller's dict.
        assert params["selectGroups"] == ["name"]
        assert "selectHostGroups" not in params
        await client.close()
