"""Wire tests for the Slack tools. The HTTP post is replaced by a recorder:
these tests never reach the network, they assert the payload that would be
sent. Fixtures are synthetic."""

import json
import time

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import slack

HOOK = "https://example.com/hook"
FRESH = str(int(time.time()) - 60)


class FakePost:
    """Stands in for httpx.AsyncClient; records every post."""

    def __init__(self, status=200, text="ok", exc=None):
        self.posts = []
        self.status, self.text, self.exc = status, text, exc

    def __call__(self, timeout=None):
        self.timeout = timeout
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if self.exc:
            raise self.exc
        self.posts.append((url, json))
        return httpx.Response(self.status, text=self.text)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(slack.SLACK_WEBHOOK_ENV, HOOK)
    monkeypatch.setenv("ZABBIX_PRODUCT_MAP", "")
    monkeypatch.setattr(classify, "_PRODUCT_MAP", None)

    def install(**kw):
        fake = FakePost(**kw)
        monkeypatch.setattr(slack.httpx, "AsyncClient", fake)
        return fake
    return install


# --- send_slack_message ---------------------------------------------------

def test_message_payload_with_channel_and_blocks(env):
    fake = env()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    out = run_tool(slack, "send_slack_message", RecordingClient(),
                   text="*hello*", channel="#ops", blocks=json.dumps(blocks))
    assert out == "Message sent to Slack."
    assert fake.posts == [(HOOK, {"text": "*hello*", "channel": "#ops", "blocks": blocks})]
    assert fake.timeout == 10


def test_message_plain_text_only(env):
    fake = env()
    run_tool(slack, "send_slack_message", RecordingClient(), text="hi")
    assert fake.posts == [(HOOK, {"text": "hi"})]


def test_message_without_webhook_sends_nothing(env, monkeypatch):
    fake = env()
    monkeypatch.delenv(slack.SLACK_WEBHOOK_ENV)
    out = run_tool(slack, "send_slack_message", RecordingClient(), text="hi")
    assert out == "No Slack webhook URL. Set SLACK_WEBHOOK_URL environment variable."
    assert fake.posts == []


def test_message_invalid_blocks_sends_nothing(env):
    fake = env()
    out = run_tool(slack, "send_slack_message", RecordingClient(), text="hi", blocks="{not json")
    assert out == "Invalid blocks JSON."
    assert fake.posts == []


def test_message_slack_rejects(env):
    env(status=400, text="invalid_payload")
    out = run_tool(slack, "send_slack_message", RecordingClient(), text="hi")
    assert out == "Slack returned: 400 invalid_payload"


def test_message_transport_error(env):
    env(exc=httpx.ConnectError("connection refused"))
    out = run_tool(slack, "send_slack_message", RecordingClient(), text="hi")
    assert out == "Error sending to Slack: connection refused"


# --- send_slack_report ----------------------------------------------------

def _host(hid, name, group, ip):
    return {"hostid": hid, "host": name, "status": "0",
            "groups": [{"name": group}], "interfaces": [{"ip": ip}]}


HOSTS = [
    _host("1", "srv-aq9001", "app_free", "192.0.2.1"),
    _host("2", "srv-aq9002", "app_free", "192.0.2.2"),
    _host("3", "srv-bv9001", "app_paid", "198.51.100.3"),
    _host("4", "srv-hm9001", "app_paid", "198.51.100.4"),
    {"hostid": "5", "host": "srv-tf9001", "status": "0", "groups": [], "interfaces": []},
]
CPU = [
    {"hostid": "1", "lastvalue": "5", "lastclock": FRESH},     # 95% busy
    {"hostid": "2", "lastvalue": "50", "lastclock": FRESH},    # 50% — below threshold
    {"hostid": "3", "lastvalue": "10", "lastclock": FRESH},    # 90% busy
    {"hostid": "4", "lastvalue": "0", "lastclock": "0"},       # never collected: not 100%
]
PROBLEMS = [
    {"eventid": "9", "name": "a", "severity": "4", "suppressed": "0"},
    {"eventid": "8", "name": "b", "severity": "4", "suppressed": "0"},
    {"eventid": "7", "name": "c", "severity": "2", "suppressed": "0"},
    {"eventid": "6", "name": "d", "severity": "5", "suppressed": "1"},  # in maintenance
]


def _client(hosts=HOSTS, problems=PROBLEMS, cpu=CPU):
    return RecordingClient({"host.get": hosts, "problem.get": problems, "item.get": cpu})


def _texts(payload):
    return [b["text"]["text"] for b in payload["blocks"] if b["type"] in ("header", "section")]


def test_report_full_payload(env):
    fake = env()
    c = _client()
    out = run_tool(slack, "send_slack_report", c, title="Daily")
    assert out == "Report sent to Slack: 4 servers, 3 problems, 2 high CPU"

    assert c.sent("host.get")["filter"] == {"status": "0"}
    assert c.sent("problem.get")["limit"] == 100
    item = c.sent("item.get")
    assert sorted(item["hostids"]) == ["1", "2", "3", "4"]
    assert item["filter"] == {"key_": "system.cpu.util[,idle]"}

    [(url, payload)] = fake.posts
    assert url == HOOK and payload["text"] == "Daily"
    texts = _texts(payload)
    assert texts[0] == "Daily"
    assert texts[1] == "*4 servers* across 2 product tiers"
    assert "• app_free / Default: *2*" in texts[2] and "• app_paid / Default: *2*" in texts[2]
    assert texts[3] == ":warning: *Active Problems (3):*\n*High*: 2 | *Warn*: 1"
    assert texts[4] == (":fire: *High CPU (>80.0%):*\n"
                        "• `srv-aq9001` (app_free/Default) — *95.0%* (192.0.2.1)\n"
                        "• `srv-bv9001` (app_paid/Default) — *90.0%* (198.51.100.3)")
    assert "srv-hm9001" not in json.dumps(payload)  # stale idle 0 is not CPU 100%
    assert payload["blocks"][-1]["type"] == "context"
    assert payload["blocks"][-1]["elements"][0]["text"].startswith("Generated by zbbx-mcp at ")


def test_report_product_filter_and_suppressed(env):
    fake = env()
    c = _client()
    out = run_tool(slack, "send_slack_report", c, product="app_paid", include_suppressed=True)
    assert out == "Report sent to Slack: 2 servers, 4 problems, 1 high CPU"
    assert sorted(c.sent("item.get")["hostids"]) == ["3", "4"]
    texts = _texts(fake.posts[0][1])
    assert "*Disaster*: 1" in texts[3]


def test_report_sections_disabled(env):
    fake = env()
    c = _client()
    out = run_tool(slack, "send_slack_report", c, include_problems=False, include_high_cpu=False)
    assert out == "Report sent to Slack: 4 servers"
    assert [m for m, _ in c.calls] == ["host.get"]
    types = [b["type"] for b in fake.posts[0][1]["blocks"]]
    assert "divider" not in types


def test_report_empty_fleet(env):
    fake = env()
    c = _client(hosts=[], problems=[])
    out = run_tool(slack, "send_slack_report", c)
    assert out == "Report sent to Slack: 0 servers"
    assert "item.get" not in [m for m, _ in c.calls]
    assert _texts(fake.posts[0][1])[1] == "*0 servers* across 0 product tiers"


def test_report_truncates_high_cpu_list(env):
    fake = env()
    hosts = [_host(str(i), f"srv-aq{9000 + i}", "app_free", f"192.0.2.{i}") for i in range(1, 13)]
    cpu = [{"hostid": str(i), "lastvalue": "1", "lastclock": FRESH} for i in range(1, 13)]
    out = run_tool(slack, "send_slack_report", _client(hosts=hosts, problems=[], cpu=cpu))
    assert out == "Report sent to Slack: 12 servers, 12 high CPU"
    cpu_text = _texts(fake.posts[0][1])[-1]
    assert cpu_text.count("• `srv-aq") == 10
    assert cpu_text.endswith("_...and 2 more_")


def test_report_without_webhook_queries_nothing(env, monkeypatch):
    fake = env()
    monkeypatch.delenv(slack.SLACK_WEBHOOK_ENV)
    c = _client()
    out = run_tool(slack, "send_slack_report", c)
    assert out.startswith("No Slack webhook URL.")
    assert c.calls == [] and fake.posts == []


def test_report_slack_rejects_and_api_error(env):
    env(status=404, text="no_service")
    assert run_tool(slack, "send_slack_report", _client()) == "Slack returned: 404 no_service"

    fake = env()

    def boom(_):
        raise ValueError("Zabbix API error: denied")
    out = run_tool(slack, "send_slack_report", RecordingClient({"host.get": boom}))
    assert out == "Error: Zabbix API error: denied"
    assert fake.posts == []
