"""ZabbixClient beyond the 7.x compatibility shim (test_client.py): the
JSON-RPC envelope, error mapping, the TTL cache, the frontend URL, and the
rollback journal — all through an httpx.MockTransport, never the network.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from zbbx_mcp.client import ZabbixClient
from zbbx_mcp.config import ZabbixConfig
from zbbx_mcp.rollback import Action

URL = "https://zabbix.example.com"


def _client(handler, url: str = URL) -> ZabbixClient:
    cfg = ZabbixConfig(url=url, token="test")
    client = ZabbixClient(cfg)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=cfg.url,
        headers={"Content-Type": "application/json"},
    )
    return client


def _rpc(result=None, error=None, status: int = 200):
    """A handler answering every request with one canned envelope.

    ``result`` may be a callable taking the parsed request body. Returns the
    handler and the list of request bodies it saw, in order.
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        envelope: dict = {"jsonrpc": "2.0", "id": body.get("id")}
        if error is not None:
            envelope["error"] = error
        else:
            envelope["result"] = result(body) if callable(result) else result
        return httpx.Response(status, json=envelope)

    return handler, seen


class TestEnvelope:
    async def test_request_ids_increment_and_params_default_to_empty(self):
        handler, seen = _rpc([])
        client = _client(handler)
        await client.call("host.get")
        await client.call("item.get", {"output": ["itemid"]})
        assert [b["id"] for b in seen] == [1, 2]
        assert seen[0]["params"] == {}
        assert seen[1]["params"] == {"output": ["itemid"]}
        assert all(b["jsonrpc"] == "2.0" for b in seen)
        assert [b["method"] for b in seen] == ["host.get", "item.get"]
        await client.close()

    async def test_posts_to_the_api_endpoint_under_the_base_url(self):
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

        client = _client(handler)
        await client.call("host.get")
        assert urls == [f"{URL}/api_jsonrpc.php"]
        await client.close()

    async def test_missing_result_is_an_empty_dict(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})

        client = _client(handler)
        assert await client.call("host.get") == {}
        await client.close()

    async def test_http_error_status_raises(self):
        handler, _ = _rpc([], status=500)
        client = _client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await client.call("host.get")
        await client.close()

    async def test_call_many_keeps_input_order(self):
        handler, seen = _rpc(lambda body: body["method"])
        client = _client(handler)
        out = await client.call_many([("host.get", None), ("item.get", {"x": 1}), ("trend.get", {})])
        assert out == ["host.get", "item.get", "trend.get"]
        assert sorted(b["id"] for b in seen) == [1, 2, 3]
        await client.close()


class TestErrorMapping:
    async def test_api_error_reports_code_and_data(self):
        handler, _ = _rpc(error={"code": -32602, "message": "Invalid params.",
                                 "data": 'Invalid parameter "/output".'})
        client = _client(handler)
        with pytest.raises(ValueError, match=r'Zabbix API error \(-32602\): Invalid parameter "/output"\.'):
            await client.call("host.get")
        await client.close()

    async def test_api_error_falls_back_to_message_then_unknown(self):
        handler, _ = _rpc(error={"code": -32600, "message": "Invalid request."})
        client = _client(handler)
        with pytest.raises(ValueError, match=r"\(-32600\): Invalid request\."):
            await client.call("host.get")
        await client.close()

        handler, _ = _rpc(error={})
        client = _client(handler)
        with pytest.raises(ValueError, match=r"\(\?\): Unknown error"):
            await client.call("host.get")
        await client.close()

    async def test_long_error_text_is_truncated(self):
        handler, _ = _rpc(error={"code": 1, "data": "x" * 300})
        client = _client(handler)
        with pytest.raises(ValueError) as exc:
            await client.call("host.get")
        text = str(exc.value)
        assert text.endswith("x" * 200 + "...")
        assert "x" * 201 not in text
        await client.close()

    async def test_no_permissions_names_the_method_and_the_token_role(self):
        handler, _ = _rpc(error={"code": -32602,
                                 "data": "No permissions to referred object or it does not exist!"})
        client = _client(handler)
        with pytest.raises(ValueError) as exc:
            await client.call("maintenance.create")
        text = str(exc.value)
        assert "No permissions for maintenance.create" in text
        assert "API token" in text
        await client.close()


class TestCache:
    def test_fresh_entry_is_returned(self):
        client = ZabbixClient(ZabbixConfig(url=URL, token="test"))
        client._set_cache("k", [{"hostid": "9001"}])
        assert client._get_cached("k", ttl=60) == [{"hostid": "9001"}]

    def test_expired_entry_is_dropped(self):
        client = ZabbixClient(ZabbixConfig(url=URL, token="test"))
        client._cache["k"] = (time.monotonic() - 61, [1])
        assert client._get_cached("k", ttl=60) is None
        assert client._get_cached("k", ttl=120) == [1]

    def test_unknown_key_is_none(self):
        client = ZabbixClient(ZabbixConfig(url=URL, token="test"))
        assert client._get_cached("missing") is None


class TestFrontendUrl:
    @pytest.mark.parametrize(("url", "expected"), [
        (f"{URL}/api_jsonrpc.php", URL),
        (f"{URL}/api", URL),
        (f"{URL}/", URL),
        (URL, URL),
        (f"{URL}/zabbix/api_jsonrpc.php", f"{URL}/zabbix"),
    ])
    def test_api_suffix_is_stripped(self, url, expected):
        client = ZabbixClient(ZabbixConfig(url=url, token="test"))
        assert client.frontend_url == expected


class TestSnapshot:
    async def test_unknown_object_type_makes_no_call(self):
        handler, seen = _rpc([])
        client = _client(handler)
        assert await client.snapshot("widget", "1") == {}
        assert seen == []
        await client.close()

    async def test_host_snapshot_sends_id_output_and_extra_selects(self):
        handler, seen = _rpc([{"hostid": "9001", "host": "srv-aq9001",
                               "hostgroups": [{"groupid": "5"}]}])
        client = _client(handler)
        snap = await client.snapshot("host", "9001")
        body = seen[-1]
        assert body["method"] == "host.get"
        params = body["params"]
        assert params["hostids"] == ["9001"]
        assert params["output"] == "extend"
        assert params["selectInterfaces"] == "extend"
        assert params["selectMacros"] == "extend"
        # The extra selectGroups rides through the 7.2 selector shim.
        assert params["selectHostGroups"] == ["groupid"]
        assert "selectGroups" not in params
        assert snap["host"] == "srv-aq9001"
        assert snap["groups"] == [{"groupid": "5"}]
        await client.close()

    async def test_item_snapshot_has_no_extra_selects(self):
        handler, seen = _rpc([{"itemid": "77", "name": "x"}])
        client = _client(handler)
        snap = await client.snapshot("item", "77")
        assert seen[-1]["params"] == {"itemids": ["77"], "output": "extend"}
        assert snap == {"itemid": "77", "name": "x"}
        await client.close()

    async def test_secret_macro_value_is_redacted(self):
        secret = "s" * 12
        handler, _ = _rpc([{"hostmacroid": "5", "macro": "{$A}", "type": "1", "value": secret}])
        client = _client(handler)
        snap = await client.snapshot("usermacro", "5")
        assert snap["value"] == "[REDACTED]"
        assert secret not in json.dumps(snap)
        await client.close()

    async def test_plain_macro_value_is_kept(self):
        handler, _ = _rpc([{"hostmacroid": "6", "macro": "{$B}", "type": "0", "value": "42"}])
        client = _client(handler)
        assert (await client.snapshot("usermacro", "6"))["value"] == "42"
        await client.close()

    async def test_empty_result_is_an_empty_dict(self):
        handler, _ = _rpc([])
        client = _client(handler)
        assert await client.snapshot("trigger", "404") == {}
        await client.close()


class TestRollbackJournal:
    async def test_update_snapshots_before_recording(self):
        handler, seen = _rpc([{"itemid": "77", "name": "before"}])
        client = _client(handler)
        await client.snapshot_and_record("update", "item", "77", "rename")
        assert seen[-1]["method"] == "item.get"
        assert seen[-1]["params"]["itemids"] == ["77"]
        entry = client.rollback_log.last
        assert entry is not None
        assert entry.action is Action.UPDATE
        assert entry.object_type == "item"
        assert entry.object_id == "77"
        assert entry.snapshot == {"itemid": "77", "name": "before"}
        assert entry.description == "rename"
        await client.close()

    async def test_delete_also_snapshots(self):
        handler, seen = _rpc([{"triggerid": "9", "description": "d"}])
        client = _client(handler)
        await client.snapshot_and_record(Action.DELETE, "trigger", "9")
        assert seen[-1]["method"] == "trigger.get"
        assert client.rollback_log.last.snapshot == {"triggerid": "9", "description": "d"}
        await client.close()

    async def test_create_records_without_a_snapshot_or_a_call(self):
        handler, seen = _rpc([{"hostid": "9001"}])
        client = _client(handler)
        await client.snapshot_and_record(Action.CREATE, "host", "9001", "new")
        assert seen == []
        entry = client.rollback_log.last
        assert entry.action is Action.CREATE
        assert entry.snapshot == {}
        await client.close()

    async def test_api_error_during_snapshot_still_records(self):
        handler, _ = _rpc(error={"code": -32602, "data": "No permissions to referred object"})
        client = _client(handler)
        await client.snapshot_and_record("delete", "host", "9001")
        entry = client.rollback_log.last
        assert entry.action is Action.DELETE
        assert entry.snapshot == {}
        await client.close()

    async def test_http_error_during_snapshot_still_records(self):
        handler, _ = _rpc([], status=503)
        client = _client(handler)
        await client.snapshot_and_record("update", "hostgroup", "12")
        assert client.rollback_log.last.object_id == "12"
        assert client.rollback_log.last.snapshot == {}
        await client.close()

    def test_record_create_appends_an_entry(self):
        client = ZabbixClient(ZabbixConfig(url=URL, token="test"))
        client.record_create("hostgroup", "12", "made")
        assert len(client.rollback_log) == 1
        entry = client.rollback_log.last
        assert (entry.action, entry.object_type, entry.object_id, entry.description) == (
            Action.CREATE, "hostgroup", "12", "made")
        assert entry.snapshot == {}
