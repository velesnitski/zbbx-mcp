"""Wire tests for the domain tools. No DNS, no sockets, no HTTP: every network
call in ``_probe_domain`` is monkeypatched. Domains are example.* only."""

import io
import ipaddress
import socket
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import httpx
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import domains

CERT = "web_cert_check.sh[{HOST.NAME}]"
WHOIS = "web_registration_check.sh[{HOST.NAME}]"
HTTPS = "web_https_check.sh[{HOST.NAME}]"


def _boom(_params):
    raise httpx.ConnectError("unreachable")


def _no_network(*_a, **_k):
    raise AssertionError("network access attempted")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # Belt and braces: any real resolver / TLS / HTTP primitive reached is a
    # test failure. (socket.socket itself stays intact here: the event loop
    # needs its self-pipe; the probe tests patch it individually.)
    for name in ("gethostbyname_ex", "getaddrinfo"):
        monkeypatch.setattr(domains.socket, name, _no_network)
    monkeypatch.setattr(domains.ssl, "create_default_context", _no_network)
    monkeypatch.setattr(domains.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(classify, "_EXTRA_PROVIDER_NETS",
                        [("Provider A", ipaddress.ip_network("192.0.2.0/24"))])


# --------------------------------------------------------------- _probe_domain

class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


class _Resp:
    def __init__(self, status, headers):
        self.status = status
        self.headers = _Headers(headers)


class _Conn:
    def __init__(self, cert):
        self._cert = cert

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        self.timeout = t

    def connect(self, addr):
        self.addr = addr

    def getpeercert(self):
        return self._cert

    def cipher(self):
        return ("TLS_AES_128_GCM_SHA256", "TLSv1.3", 128)


class _Ctx:
    def __init__(self, cert):
        self.cert = cert

    def wrap_socket(self, sock, server_hostname=""):
        assert server_hostname == "example.com"
        return _Conn(self.cert)


def _fmt(dt):
    return dt.strftime("%b %d %H:%M:%S %Y GMT")


def _patch_network(monkeypatch, *, cert, resp=None, http_exc=None, v6=True):
    monkeypatch.setattr(domains.socket, "gethostbyname_ex",
                        lambda d: (d, [], ["192.0.2.20", "192.0.2.10", "192.0.2.10"]))

    def gai(*_a):
        if not v6:
            raise socket.gaierror("no AAAA")
        return [("v6",)]
    monkeypatch.setattr(domains.socket, "getaddrinfo", gai)
    monkeypatch.setattr(domains.socket, "socket", lambda *a: object())
    monkeypatch.setattr(domains.ssl, "create_default_context", lambda: _Ctx(cert))

    def urlopen(req, timeout=0):
        assert req.full_url == "https://example.com" and req.get_method() == "HEAD"
        if http_exc is not None:
            raise http_exc
        return resp
    monkeypatch.setattr(domains.urllib.request, "urlopen", urlopen)


def test_probe_domain_collects_dns_tls_and_http(monkeypatch):
    now = datetime.now(timezone.utc)
    cert = {
        "notAfter": _fmt(now + timedelta(days=40, hours=1)),
        "notBefore": _fmt(datetime(2026, 1, 2, tzinfo=timezone.utc)),
        "issuer": ((("countryName", "AQ"),), (("organizationName", "Example CA"),)),
        "subjectAltName": (("DNS", "example.com"), ("DNS", "www.example.com")),
    }
    resp = _Resp(301, {"Server": "edge", "Strict-Transport-Security": "max-age=1",
                       "Location": "https://www.example.com/"})
    _patch_network(monkeypatch, cert=cert, resp=resp)
    r = domains._probe_domain("example.com")
    assert r["ip"] == "192.0.2.10" and r["all_ips"] == "192.0.2.10; 192.0.2.20"
    assert r["provider"] == "Provider A" and r["ipv6"] == "yes"
    assert r["ssl_days"] == 40 and r["ssl_valid_from"] == "2026-01-02"
    assert r["ssl_issuer"] == "Example CA" and r["ssl_key"] == "TLS_AES_128_GCM_SHA256"
    assert r["ssl_sans"] == 2
    assert r["http_status"] == 301 and r["http_server"] == "edge" and r["hsts"] == "yes"
    assert r["http_redirect"] == "https://www.example.com/"
    assert r["resp_ms"] >= 0


def test_probe_domain_http_error_and_issuer_fallback(monkeypatch):
    now = datetime.now(timezone.utc)
    cert = {"notAfter": _fmt(now + timedelta(days=5, hours=1)),
            "notBefore": _fmt(now - timedelta(days=1)),
            "issuer": ((("commonName", "Example Root"),),)}
    hdrs = Message()
    hdrs["Server"] = "origin"
    exc = urllib.error.HTTPError("https://example.com", 503, "down", hdrs, io.BytesIO())
    _patch_network(monkeypatch, cert=cert, http_exc=exc, v6=False)
    r = domains._probe_domain("example.com")
    assert r["ipv6"] == "no"
    assert r["ssl_issuer"] == "Example Root" and r["ssl_sans"] == 0
    assert r["http_status"] == 503 and r["http_server"] == "origin"
    assert r["hsts"] == "no" and r["resp_ms"] >= 0


def test_probe_domain_everything_fails_returns_sentinels(monkeypatch):
    # Every primitive raises (autouse fixture + socket.socket here).
    def nx(*_a):
        raise socket.gaierror("nx")
    monkeypatch.setattr(domains.socket, "gethostbyname_ex", nx)
    monkeypatch.setattr(domains.socket, "getaddrinfo", nx)
    monkeypatch.setattr(domains.socket, "socket", _no_network)
    r = domains._probe_domain("example.org")
    assert r == {
        "ip": "", "all_ips": "", "ipv6": "no", "provider": "",
        "ssl_days": -1, "ssl_issuer": "", "ssl_valid_from": "", "ssl_key": "", "ssl_sans": 0,
        "http_status": -1, "http_server": "", "http_redirect": "", "hsts": "no",
        "resp_ms": -1,
    }


# ----------------------------------------------------------- get_domain_status

def _item(hid, host, key, val):
    return {"itemid": f"{hid}{key[:4]}", "hostid": hid, "key_": key, "lastvalue": val,
            "lastclock": "0", "hosts": [{"hostid": hid, "host": host}]}


ITEMS = [
    _item("1", "example.com", CERT, "1"), _item("1", "example.com", WHOIS, "1"),
    _item("1", "example.com", HTTPS, "1"),
    _item("2", "example.net", CERT, "0"), _item("2", "example.net", WHOIS, "1"),
    _item("2", "example.net", HTTPS, "0"),
    _item("3", "example.org", CERT, "junk"),                     # unparsable → N/A
    _item("4", "srv-aq9001", CERT, "1"),                          # not a domain name
]
GROUPS = [{"hostid": "1", "groups": [{"name": "app_free"}]},
          {"hostid": "2", "groups": [{"name": "app_paid"}, {"name": "web"}]}]

PROBES = {
    "example.com": {"ip": "192.0.2.10", "all_ips": "192.0.2.10", "ipv6": "yes",
                    "provider": "Provider A", "ssl_days": 40, "ssl_issuer": "Example CA",
                    "ssl_valid_from": "2026-01-02", "ssl_key": "AES", "ssl_sans": 2,
                    "http_status": 200, "http_server": "edge", "http_redirect": "",
                    "hsts": "yes", "resp_ms": 12},
    "example.net": {"ip": "192.0.2.11", "ssl_days": -1, "http_status": -1, "resp_ms": -1},
    "example.org": {},
}


@pytest.fixture
def probes(monkeypatch):
    seen = []

    def fake(name):
        seen.append(name)
        return PROBES[name]
    monkeypatch.setattr(domains, "_probe_domain", fake)
    return seen


def _client(items=ITEMS):
    return RecordingClient({"item.get": items, "host.get": GROUPS})


def test_get_domain_status_table(probes):
    c = _client()
    out = run_tool(domains, "get_domain_status", c)
    ip = c.sent("item.get")
    assert ip["filter"] == {"key_": [CERT, WHOIS, HTTPS], "status": "0"}
    assert ip["selectHosts"] == ["hostid", "host"]
    hp = c.sent("host.get")
    assert hp["hostids"] == ["1", "2", "3", "4"] and hp["selectGroups"] == ["name"]
    assert sorted(probes) == ["example.com", "example.net", "example.org"]
    assert out.startswith("**Domain Status: 3 domains, 2 with issues**")
    assert "| example.com | OK | UP | 192.0.2.10 | Provider A | 40d | 200 | edge | yes | 12ms |" in out
    assert "| example.net | EXPIRED | DOWN | 192.0.2.11 |  | N/Ad | N/A |  | no | N/Ams |" in out
    assert "| example.org | N/A | N/A |" in out
    assert "srv-aq9001" not in out


def test_get_domain_status_only_problems_search_and_truncation(probes):
    out = run_tool(domains, "get_domain_status", _client(), only_problems=True, max_results=1)
    assert "example.com |" not in out
    assert "| example.net |" in out and "| example.org |" not in out
    assert out.endswith("*1 more omitted*")
    out = run_tool(domains, "get_domain_status", _client(), search="NET")
    assert out.startswith("**Domain Status: 1 domains, 1 with issues**")


def test_get_domain_status_csv_written_under_tmp_home(probes, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))                  # never the real home
    (tmp_path / "Downloads").mkdir()
    out = run_tool(domains, "get_domain_status", _client(), format="csv")
    files = list((tmp_path / "Downloads").glob("domains-*.csv"))
    assert len(files) == 1
    assert f"to `{files[0]}`" in out and "(19 fields)" in out and "2 with issues" in out
    rows = files[0].read_text().splitlines()
    assert rows[0].startswith("Domain,SSL Cert,WHOIS,HTTPS,IP,All IPs,IPv6,Provider")
    assert rows[1].split(",")[:9] == ["example.com", "OK", "OK", "UP", "192.0.2.10",
                                      "192.0.2.10", "yes", "Provider A", "40"]
    assert rows[1].endswith(",12,app_free")
    assert rows[2].endswith("app_paid; web")                   # commas escaped
    assert len(rows) == 4


def test_get_domain_status_csv_all_healthy(probes, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    out = run_tool(domains, "get_domain_status", _client(ITEMS[:3]), format="csv")
    assert out.endswith("All healthy") and "Exported 1 domains" in out


def test_get_domain_status_empty_paths(probes):
    c = RecordingClient({"item.get": []})
    assert run_tool(domains, "get_domain_status", c) == "No domain monitoring items found."
    out = run_tool(domains, "get_domain_status", _client(), search="example.invalid")
    assert out == "No domains match the filter."
    # Items without a host: no group lookup at all, and nothing is a domain
    bare = [{"itemid": "9", "key_": CERT, "lastvalue": "1"}]
    c = RecordingClient({"item.get": bare})
    assert run_tool(domains, "get_domain_status", c) == "No domains match the filter."
    assert [m for m, _ in c.calls] == ["item.get"]
    assert probes == []


def test_get_domain_status_error():
    out = run_tool(domains, "get_domain_status", RecordingClient({"item.get": _boom}))
    assert out == "Error: unreachable"


# -------------------------------------------------------------- get_ssl_expiry

SSL_ITEMS = [
    {"itemid": "1", "hostid": "1", "lastvalue": "1", "hosts": [{"host": "example.com"}]},
    {"itemid": "2", "hostid": "2", "lastvalue": "0", "hosts": [{"host": "example.net"}]},
    {"itemid": "3", "hostid": "3", "lastvalue": "x", "hosts": [{"host": "example.org"}]},
    {"itemid": "4", "hostid": "4", "lastvalue": "1"},
]


def test_get_ssl_expiry_problems_only():
    c = RecordingClient({"item.get": SSL_ITEMS})
    out = run_tool(domains, "get_ssl_expiry", c)
    assert c.sent("item.get")["filter"] == {"key_": CERT, "status": "0"}
    assert out.startswith("**SSL Certs: 2 OK, 2 issues**")
    assert "| example.net | EXPIRED |" in out and "| example.org | ERROR |" in out
    assert "| example.com |" not in out


def test_get_ssl_expiry_all_rows_capped():
    out = run_tool(domains, "get_ssl_expiry", RecordingClient({"item.get": SSL_ITEMS}),
                   only_problems=False, max_results=2)
    assert "| ? | OK |" in out and "| example.com | OK |" in out
    assert "example.net" not in out


def test_get_ssl_expiry_empty_and_error():
    assert run_tool(domains, "get_ssl_expiry", RecordingClient()) == "No SSL cert check items found."
    assert run_tool(domains, "get_ssl_expiry", RecordingClient({"item.get": _boom})) == "Error: unreachable"


# ------------------------------------------------------------- get_domain_list

LIST_HOSTS = [
    {"hostid": "1", "host": "example.com", "status": "0", "groups": [{"name": "Web Free"}]},
    {"hostid": "2", "host": "example.net", "status": "1", "groups": [{"name": "web paid"}]},
    {"hostid": "3", "host": "example.org", "status": "0", "groups": [{"name": "other"}]},
]


def test_get_domain_list_dedupes_hosts_and_filters_group():
    c = RecordingClient({"item.get": [{"hostid": "1"}, {"hostid": "1"}, {"hostid": "2"}, {"hostid": "3"}],
                         "host.get": LIST_HOSTS})
    out = run_tool(domains, "get_domain_list", c, group="WEB", max_results=1)
    assert sorted(c.sent("host.get")["hostids"]) == ["1", "2", "3"]
    assert out.startswith("**2 monitored domains**")
    assert "- example.com [enabled] (Web Free)" in out
    assert "example.net" not in out and out.endswith("*1 more omitted*")
    c = RecordingClient({"item.get": [{"hostid": "2"}], "host.get": LIST_HOSTS[1:2]})
    assert "- example.net [disabled] (web paid)" in run_tool(domains, "get_domain_list", c)


def test_get_domain_list_empty_paths_and_error():
    assert run_tool(domains, "get_domain_list", RecordingClient()) == "No domain monitoring items found."
    c = RecordingClient({"item.get": [{"hostid": "1"}], "host.get": LIST_HOSTS})
    assert run_tool(domains, "get_domain_list", c, group="nomatch") == "No domains match the filter."
    assert run_tool(domains, "get_domain_list", RecordingClient({"item.get": _boom})) == "Error: unreachable"
