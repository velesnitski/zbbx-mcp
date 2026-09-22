"""Wire tests for the cost-ingestion tools (``tools/costs_import.py``).

Each test drives the real tool through a recording fake and pins the exact
``usermacro.*`` payload the tool sends — a cost import that writes the wrong
value, description or host is a finance error that reads as success, so the
wire is the layer to test. Fixtures are synthetic: documentation addresses,
uninhabited-territory codes, round invented prices.
"""

from __future__ import annotations

import csv
import json

import openpyxl
import pytest

from tests.wiretest import RecordingClient, run_tool
from zbbx_mcp import classify
from zbbx_mcp.tools import costs_import

COST = "{$COST_MONTH}"


@pytest.fixture(autouse=True)
def _no_product_map(monkeypatch, tmp_path):
    # Group-name classification (no operator map) and file access confined
    # to the test directory (ADR 076).
    monkeypatch.setattr(classify, "_PRODUCT_MAP", {})
    monkeypatch.setenv("ZBBX_FILE_ROOTS", str(tmp_path))


def _host(hid, name, ip=None, group="app_free"):
    h = {"hostid": hid, "host": name, "groups": [{"name": group}], "interfaces": []}
    if ip:
        h["interfaces"] = [{"ip": ip, "main": "1", "type": "1"}]
    return h


def _macro(mid, hid, value, description=""):
    return {"hostmacroid": mid, "hostid": hid, "macro": COST,
            "value": value, "description": description}


def _by_hostids(macros):
    """``usermacro.get`` that honours a ``hostids`` scope, like Zabbix does."""
    def f(p):
        ids = p.get("hostids")
        return [m for m in macros if not ids or m["hostid"] in ids]
    return f


def _calls(c, method):
    return [p for m, p in c.calls if m == method]


HOSTS = [
    _host("1", "srv-aq9001", "192.0.2.10"),
    _host("2", "srv-aq9002", "192.0.2.11"),
    _host("3", "srv-bv9001", "198.51.100.20"),
    _host("4", "srv-hm9001", "203.0.113.30", group="app_paid"),
    _host("5", "mon-9001"),                                   # no interface
]


class TestImportServerCosts:
    def _client(self, existing):
        return RecordingClient({"host.get": HOSTS, "usermacro.get": existing})

    def test_patterns_create_update_and_leave_alone(self):
        c = self._client([_macro("m1", "1", "20"), _macro("m2", "2", "5")])
        out = run_tool(costs_import, "import_server_costs", c,
                       costs_json=json.dumps({"srv-aq*": 20, "srv-bv9001": 30}))
        assert "Matched: 3 servers" in out, out
        assert "Created: 1 new macros" in out
        assert "Updated: 1 existing macros" in out
        assert "Unchanged: 1" in out
        assert "Unmatched: 2 servers" in out
        assert _calls(c, "usermacro.update") == [{"hostmacroid": "m2", "value": "20"}]
        assert _calls(c, "usermacro.create") == [
            {"hostid": "3", "macro": COST, "value": "30", "description": "src:bulk_pattern"},
        ]
        # The existing-macro lookup is scoped to the enabled hosts, one call.
        assert c.sent("usermacro.get") == {
            "hostids": ["1", "2", "3", "4", "5"],
            "output": ["hostmacroid", "hostid", "value"],
            "filter": {"macro": COST},
        }

    def test_only_if_empty_keeps_a_costed_host(self):
        c = self._client([_macro("m2", "2", "5")])
        out = run_tool(costs_import, "import_server_costs", c,
                       costs_json=json.dumps({"srv-aq*": 20}), only_if_empty=True)
        assert "Updated: 0 existing macros" in out and "Unchanged: 1" in out
        assert _calls(c, "usermacro.update") == []
        assert _calls(c, "usermacro.create") == [
            {"hostid": "1", "macro": COST, "value": "20", "description": "src:bulk_pattern"},
        ]

    def test_bad_input_is_refused_before_any_call(self):
        c = self._client([])
        assert run_tool(costs_import, "import_server_costs", c,
                        costs_json="{not json").startswith("Invalid JSON")
        assert run_tool(costs_import, "import_server_costs", c,
                        costs_json="[1, 2]").startswith("Expected a JSON object")
        assert c.calls == []

    def test_a_failing_write_is_reported_per_host(self):
        def create(_p):
            raise ValueError("macro rejected")
        c = RecordingClient({"host.get": HOSTS, "usermacro.get": [], "usermacro.create": create})
        out = run_tool(costs_import, "import_server_costs", c,
                       costs_json=json.dumps({"srv-aq9001": 20}))
        assert "Created: 0 new macros" in out
        assert "Errors (1):" in out and "- srv-aq9001: macro rejected" in out


class TestSetBulkCost:
    def _client(self, groups, hosts, existing):
        return RecordingClient({"hostgroup.get": groups, "host.get": hosts,
                                "usermacro.get": existing})

    def test_group_hosts_get_the_cost(self):
        c = self._client([{"groupid": "10"}], HOSTS[:2], [_macro("m1", "1", "20.0")])
        out = run_tool(costs_import, "set_bulk_cost", c, group="app_free", cost=20.0)
        assert out == ("Set $20.0/month on 2 servers in 'app_free'. "
                       "Created: 1, Updated: 0, Unchanged: 1.")
        assert c.sent("hostgroup.get")["filter"] == {"name": ["app_free"]}
        assert c.sent("host.get")["groupids"] == ["10"]
        assert _calls(c, "usermacro.create") == [
            {"hostid": "2", "macro": COST, "value": "20.0",
             "description": "Monthly server cost in USD"},
        ]

    def test_a_changed_value_is_updated_in_place(self):
        c = self._client([{"groupid": "10"}], HOSTS[:1], [_macro("m1", "1", "20.0")])
        out = run_tool(costs_import, "set_bulk_cost", c, group="app_free", cost=25.0)
        assert "Created: 0, Updated: 1, Unchanged: 0." in out
        assert _calls(c, "usermacro.update") == [{"hostmacroid": "m1", "value": "25.0"}]

    def test_unknown_or_empty_group(self):
        assert run_tool(costs_import, "set_bulk_cost", self._client([], [], []),
                        group="nope", cost=1.0) == "Host group 'nope' not found."
        assert run_tool(costs_import, "set_bulk_cost",
                        self._client([{"groupid": "10"}], [], []),
                        group="app_free", cost=1.0) == "No enabled hosts in group 'app_free'."


# A billing file exercising the IP pass, the /24 pass, an exact name, an
# out-of-range price and an entry nobody owns.
BILLING = {
    "by_ip": {
        "192.0.2.10": 45,        # exact interface match: host 1
        "192.0.2": 12,           # /24 prefix: host 2 (host 1 keeps its stronger 45)
        "203.0.113.99": 9000,    # above max_cost: skipped
    },
    "by_name": {
        "srv-bv9001": 30,        # exact hostname: host 3
        "ghost-um9001": 33,      # nobody: unmatched
    },
}


class TestImportCostsByIp:
    def _client(self, macros=()):
        return RecordingClient({"host.get": HOSTS, "usermacro.get": _by_hostids(list(macros))})

    def test_dry_run_reports_every_pass_and_writes_nothing(self):
        # Existing costs give the provider median the sanity check compares to.
        c = self._client([_macro("m1", "1", "20"), _macro("m2", "2", "20")])
        out = run_tool(costs_import, "import_costs_by_ip", c, costs_json=json.dumps(BILLING))
        assert "**DRY RUN — 3 hosts matched**" in out, out
        assert "By IP: 1 | By /24: 1 | By name: 1 | Translated: 0 | Compound: 0" in out
        assert "Unmatched: 1 name entries" in out
        assert "Skipped: 1 outside $1-$5000" in out
        assert "**Total: $87.00/mo** ($1,044.00/yr)" in out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert rows == [
            "| srv-aq9001 | ip | $45.00 |",
            "| srv-bv9001 | name | $30.00 |",
            "| srv-aq9002 | ip/24 | $12.00 |",
        ]
        assert "**⚠ Sanity check: 1 prices far from provider median**" in out
        assert "- srv-aq9001: $45.00 is 2.2× Other median $20.00" in out
        assert out.endswith("Set `dry_run=false` to apply.")
        assert _calls(c, "usermacro.update") == [] and _calls(c, "usermacro.create") == []
        assert c.sent("host.get")["selectInterfaces"] == ["ip"]

    def test_apply_sends_provenance_tagged_macros(self):
        c = self._client([_macro("m1", "1", "20"), _macro("m2", "2", "12.0")])
        out = run_tool(costs_import, "import_costs_by_ip", c,
                       costs_json=json.dumps(BILLING), dry_run=False)
        assert "**Cost import complete — 3 servers**" in out, out
        assert "By IP: 1 | By name: 1" in out
        assert "Created: 1 | Updated: 1 | Unchanged: 1" in out
        assert "Total: $87.00/mo ($1,044.00/yr)" in out
        assert "Unmatched: 1 name entries" in out
        assert _calls(c, "usermacro.update") == [
            {"hostmacroid": "m1", "value": "45.0", "description": "src:billing_ip"},
        ]
        assert _calls(c, "usermacro.create") == [
            {"hostid": "3", "macro": COST, "value": "30.0", "description": "src:billing_name"},
        ]
        # The pre-write lookup is scoped to the matched hosts only.
        assert c.sent("usermacro.get")["hostids"] == ["1", "2", "3"]

    def test_fuzzy_name_passes(self, monkeypatch):
        monkeypatch.setenv("ZABBIX_BILLING_RENAMES", "legacy-:srv-")
        hosts = [
            _host("1", "srv-aq9001", "192.0.2.10"),
            _host("3", "srv-bv9001", "198.51.100.20"),
            _host("4", "srv-hm9001", "203.0.113.30"),
            _host("6", "srv-gs9001", "203.0.113.50"),
            _host("7", "srv-pn9001", "203.0.113.60"),
            _host("8", "srv-sj9001 sj9002", "203.0.113.70"),   # compound: two boxes, one host
            _host("9", "srv-tf9001", "203.0.113.80"),
        ]
        names = {
            "srv-aq9001": 40,                    # pass 2: exact
            "provider a - srv-bv9001": 30,       # pass 3: last dash segment
            "box 203.0.113.30": 15,              # pass 4: address inside the name
            "srv-gs9001-node": 22,               # pass 5: prefix
            "pn9001-srv": 27,                    # pass 6: reversed billing name
            "legacy-tf9001": 19,                 # pass 6: configured rename
            "srv-sj9001": 10, "srv-sj9002": 11,  # pass 1c: summed into the compound host
        }
        c = RecordingClient({"host.get": hosts, "usermacro.get": []})
        out = run_tool(costs_import, "import_costs_by_ip", c,
                       costs_json=json.dumps({"by_name": names}))
        assert "**DRY RUN — 7 hosts matched**" in out, out
        assert "By IP: 0 | By /24: 0 | By name: 4 | Translated: 2 | Compound: 1" in out
        assert "Unmatched: 0 name entries" in out
        assert "**Total: $174.00/mo**" in out
        assert "| srv-sj9001 sj9002 | compound(2) | $21.00 |" in out
        assert "| srv-pn9001 | translated | $27.00 |" in out
        assert "| srv-tf9001 | translated | $19.00 |" in out
        assert "| srv-gs9001 | name | $22.00 |" in out

        # Strict mode drops only the permissive prefix pass.
        strict = run_tool(costs_import, "import_costs_by_ip", c,
                          costs_json=json.dumps({"by_name": names}), name_match_strict=True)
        assert "**DRY RUN — 6 hosts matched**" in strict, strict
        assert "By name: 3 | Translated: 2 | Compound: 1" in strict
        assert "Unmatched: 1 name entries" in strict
        assert "srv-gs9001" not in strict

    def test_duplicate_names_are_dropped_and_disclosed(self):
        by_ip = {
            "192.0.2.10": {"name": "twin", "price": 20},
            "192.0.2.11": {"name": "twin", "price_monthly": 25},
            "198.51.100.20": {"name": "srv-bv9001"},          # no price at all: skipped
        }
        c = self._client()
        out = run_tool(costs_import, "import_costs_by_ip", c, costs_json=json.dumps({"by_ip": by_ip}))
        assert "**DRY RUN — 2 hosts matched**" in out, out
        assert "**⚠ Duplicate-name entries (dropped from name-match): 1**" in out
        assert "- `twin`: $20.00, $25.00" in out
        assert "Skipped: 1 outside" in out

    def test_only_if_empty_consults_existing_costs_first(self):
        c = self._client([_macro("m1", "1", "20"), _macro("m2", "2", "0")])
        out = run_tool(costs_import, "import_costs_by_ip", c,
                       costs_json=json.dumps({"192.0.2.10": 45, "192.0.2.11": 12}),
                       only_if_empty=True)
        assert "**DRY RUN — 1 hosts matched**" in out, out
        assert "Skipped (already costed): 1" in out
        assert "| srv-aq9002 | ip | $12.00 |" in out
        assert "srv-aq9001" not in out.split("| Host |")[1]
        assert c.sent("usermacro.get") == {
            "hostids": ["1", "2"], "output": ["hostid", "value"], "filter": {"macro": COST},
        }

    def test_file_input_and_unmatched_export(self, tmp_path):
        src = tmp_path / "billing.json"
        src.write_text(json.dumps(BILLING))
        export = tmp_path / "unmatched.json"
        c = self._client()
        out = run_tool(costs_import, "import_costs_by_ip", c,
                       file_path=str(src), export_unmatched=str(export))
        assert f"Unmatched exported to `{export}`" in out, out
        assert json.loads(export.read_text()) == {"ghost-um9001": 33.0}

        applied = run_tool(costs_import, "import_costs_by_ip", self._client(),
                           file_path=str(src), export_unmatched=str(export), dry_run=False)
        assert f"Exported to `{export}`" in applied

    def test_input_problems_are_named(self):
        c = self._client()
        assert run_tool(costs_import, "import_costs_by_ip", c) == "Provide file_path or costs_json."
        assert run_tool(costs_import, "import_costs_by_ip", c,
                        file_path="/nonexistent-root/x.json").startswith("Failed to load:")
        assert run_tool(costs_import, "import_costs_by_ip", c,
                        costs_json="[1]").startswith("Expected JSON object")
        assert c.calls == []


FEES = [
    {"cluster": "srv-aq9001", "extra_ips": ["192.0.2.101", "192.0.2.102"], "extra_cost_month": 6},
    {"cluster": "srv-bv9001", "extra_ips": ["198.51.100.21"], "extra_cost_month": 3},
    {"cluster": "srv-hm9001", "extra_ips": ["203.0.113.31"], "extra_cost_month": 4},
    {"cluster": "srv-um9099", "extra_ips": ["203.0.113.99"], "extra_cost_month": 5},  # unknown
    {"cluster": "srv-aq9001", "extra_ips": [], "extra_cost_month": -1},               # negative: ignored
]


class TestImportClusterIpFees:
    def _client(self, **overrides):
        hosts = [
            {"hostid": "1", "host": "srv-aq9001",
             # A prior run already added these extras: the base must be
             # recovered from the description, not stacked again.
             "macros": [_macro("m1", "1", "26", "src:cluster_extras base 20.00 + 2 extra IPs (6.00)")]},
            {"hostid": "3", "host": "srv-bv9001", "macros": [_macro("m3", "3", "40", "src:billing_ip")]},
            {"hostid": "4", "host": "srv-hm9001", "macros": []},
        ]

        def host_get(p):
            return [h for h in hosts if h["host"] in p["filter"]["host"]]
        return RecordingClient({"host.get": host_get, **overrides})

    def test_dry_run_plan_is_idempotent_against_prior_extras(self):
        c = self._client()
        out = run_tool(costs_import, "import_cluster_ip_fees", c, fees_json=json.dumps(FEES))
        assert out.startswith("**Cluster IP fees import** — 3 clusters, $13.00/mo added (1 missing)  DRY RUN"), out
        rows = [ln for ln in out.splitlines() if ln.startswith("| srv-")]
        assert rows == [
            "| srv-aq9001 | $26.00 | $20.00 | $6.00 | $26.00 | 2 |",   # converges, no stacking
            "| srv-hm9001 | $0.00 | $0.00 | $4.00 | $4.00 | 1 |",
            "| srv-bv9001 | $40.00 | $40.00 | $3.00 | $43.00 | 1 |",
        ]
        assert "**Missing (not found in Zabbix):** srv-um9099" in out
        assert out.endswith("*Set dry_run=false to apply.*")
        sent = c.sent("host.get")
        assert sent["filter"]["host"] == ["srv-aq9001", "srv-bv9001", "srv-hm9001", "srv-um9099", "srv-aq9001"]
        assert sent["selectMacros"] == ["hostmacroid", "macro", "value", "description"]
        assert len(c.calls) == 1

    def test_apply_updates_existing_and_creates_missing(self):
        c = self._client()
        out = run_tool(costs_import, "import_cluster_ip_fees", c,
                       fees_json=json.dumps(FEES), dry_run=False)
        assert "**Applied: 3/3**" in out, out
        assert sorted(_calls(c, "usermacro.update"), key=lambda p: p["hostmacroid"]) == [
            {"hostmacroid": "m1", "value": "26.0",
             "description": "src:cluster_extras base 20.00 + 2 extra IPs (6.00)"},
            {"hostmacroid": "m3", "value": "43.0",
             "description": "src:cluster_extras base 40.00 + 1 extra IP (3.00)"},
        ]
        assert _calls(c, "usermacro.create") == [
            {"hostid": "4", "macro": COST, "value": "4.0",
             "description": "src:cluster_extras base 0.00 + 1 extra IP (4.00)"},
        ]

    def test_overwrite_base_replaces_the_recovered_base(self):
        out = run_tool(costs_import, "import_cluster_ip_fees", self._client(),
                       fees_json=json.dumps(FEES[:1]), overwrite_base=50)
        assert "| srv-aq9001 | $26.00 | $50.00 | $6.00 | $56.00 | 2 |" in out, out

    def test_a_failed_write_is_counted_not_hidden(self):
        def update(p):
            if p["hostmacroid"] == "m3":
                raise ValueError("boom")
            return {"hostmacroids": [p["hostmacroid"]]}
        c = self._client(**{"usermacro.update": update})
        out = run_tool(costs_import, "import_cluster_ip_fees", c,
                       fees_json=json.dumps(FEES), dry_run=False)
        assert "**Applied: 2/3**" in out, out
        assert "Errors: 1" in out and "  - boom" in out

    def test_file_input_and_malformed_input(self, tmp_path):
        src = tmp_path / "fees.json"
        src.write_text(json.dumps(FEES[:2]))
        out = run_tool(costs_import, "import_cluster_ip_fees", self._client(), file_path=str(src))
        assert out.startswith("**Cluster IP fees import** — 2 clusters, $9.00/mo added  DRY RUN")
        c = self._client()
        assert run_tool(costs_import, "import_cluster_ip_fees", c).startswith("Provide either")
        assert run_tool(costs_import, "import_cluster_ip_fees", c,
                        fees_json="{}").startswith("Expected JSON array")
        assert run_tool(costs_import, "import_cluster_ip_fees", c,
                        fees_json="[{}]") == "No cluster names in input."
        assert c.calls == []


def _detail_sheet(ws, ip_header="IP server"):
    """The wide accounting layout: name in col L, addresses in M, prices in N/O, add-ons in Q."""
    hdr = [""] * 17
    hdr[11], hdr[12], hdr[13], hdr[14], hdr[16] = "Server name", ip_header, "Price server", "Price $", "Addons"
    ws.append(hdr)

    def row(name, ip, p1=None, p2=None, addon=None):
        r = [None] * 17
        r[11], r[12], r[13], r[14], r[16] = name, ip, p1, p2, addon
        ws.append(r)
    row("srv-aq9001", "192.0.2.10, 192.0.2.11", 30, None, 5)   # two addresses: the first carries the price
    row("srv-bv9001", "198.51.100.20", None, 40)               # price only in the second column
    row("srv-hm9001", "203.0.113.30", 7000, None)              # implausible price: dropped
    row("srv-tf9001", None)                                    # no address cell
    row("srv-gs9001", "n/a")                                   # no address in the cell


def _simple_sheet(ws, rows):
    ws.append(["Name", "NS", "IPv4", "Price EUR"])
    for r in rows:
        ws.append(list(r))


class TestImportFromXlsx:
    def _workbook(self, path, ip_header="IP server"):
        wb = openpyxl.Workbook()
        _detail_sheet(wb.active, ip_header)
        wb.active.title = "detail"
        _simple_sheet(wb.create_sheet("simple"), [
            ("srv-hm9001", "ns", "203.0.113.30", 10),
            ("srv-tf9001", "", "203.0.113.40", "12,5"),     # decimal comma
            ("bad", "", "203.0.113.41", "abc"),             # unparsable price: dropped
            ("empty", "", None, 5),                         # no address: dropped
        ])
        wb.save(path)
        return path

    def test_both_sheet_shapes_flatten_to_one_csv(self, tmp_path):
        src = self._workbook(tmp_path / "billing.xlsx")
        out_csv = tmp_path / "flat.csv"
        out = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                       file_path=str(src), output_csv=str(out_csv), eur_usd=2.0)
        assert out == (
            "Parsed 5 unique IPs from 2 sheets\n"
            "Priced: 4 | Extras (price=0): 1\n"
            "Total: $120.00/mo\n"
            f"Wrote: {out_csv}"
        ), out
        with open(out_csv, newline="") as f:
            rows = {r["ip"]: (r["billing_name"], r["price_monthly"]) for r in csv.DictReader(f)}
        assert rows == {
            "192.0.2.10": ("srv-aq9001", "35.00"),      # 30 + 5 add-on
            "192.0.2.11": ("srv-aq9001", "0.00"),       # second address of the same line
            "198.51.100.20": ("srv-bv9001", "40.00"),
            "203.0.113.30": ("srv-hm9001", "20.00"),    # 10 EUR at 2.0
            "203.0.113.40": ("srv-tf9001", "25.00"),    # "12,5" EUR at 2.0
        }

    def test_localised_address_header_is_honoured(self, tmp_path, monkeypatch):
        src = self._workbook(tmp_path / "billing.xlsx", ip_header="Adresse IP")
        out_csv = tmp_path / "flat.csv"
        without = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                           file_path=str(src), output_csv=str(out_csv), eur_usd=2.0)
        assert without.startswith("Parsed 2 unique IPs"), without   # only the simple sheet
        monkeypatch.setenv("ZABBIX_BILLING_IP_HEADER", "adresse")
        with_env = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                            file_path=str(src), output_csv=str(out_csv), eur_usd=2.0)
        assert with_env.startswith("Parsed 5 unique IPs"), with_env

    def test_a_repeated_address_keeps_the_higher_price(self, tmp_path):
        wb = openpyxl.Workbook()
        _simple_sheet(wb.active, [("srv-aq9001", "", "192.0.2.10", 10), ("srv-aq9001", "", "192.0.2.10", 30)])
        src = tmp_path / "dup.xlsx"
        wb.save(src)
        out_csv = tmp_path / "flat.csv"
        out = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                       file_path=str(src), output_csv=str(out_csv), eur_usd=1.0)
        assert out.startswith("Parsed 1 unique IPs from 1 sheets\nPriced: 1"), out
        assert "Total: $30.00/mo" in out

    def test_unreadable_or_unwritable_paths_are_reported(self, tmp_path):
        out = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                       file_path=str(tmp_path / "missing.xlsx"), output_csv=str(tmp_path / "o.csv"))
        assert out.startswith("Failed to open XLSX: File not found"), out
        src = self._workbook(tmp_path / "billing.xlsx")
        out = run_tool(costs_import, "import_from_xlsx", RecordingClient(),
                       file_path=str(src), output_csv="/nonexistent-root/o.csv")
        assert out.startswith("Failed to write output CSV:"), out


class TestFillCostMedian:
    HOSTS = [
        _host("1", "srv-aq9001", "192.0.2.10"),
        _host("2", "srv-aq9002", "192.0.2.11"),
        _host("3", "srv-aq9003", "192.0.2.12"),                   # no macro at all
        _host("6", "srv-aq9006", "192.0.2.13"),                   # macro present but empty
        _host("4", "srv-hm9001", "203.0.113.30", group="app_paid"),  # no costed peer in its product
        _host("5", "mon-9001"),                                   # no address: never costed
    ]
    MACROS = [_macro("m1", "1", "20"), _macro("m2", "2", "30"), _macro("m6", "6", "0")]

    def _client(self, **overrides):
        return RecordingClient({"host.get": self.HOSTS, "usermacro.get": self.MACROS, **overrides})

    def test_product_median_dry_run(self):
        c = self._client()
        out = run_tool(costs_import, "fill_cost_median", c)
        assert out.startswith("**Fill cost by product median — DRY RUN**"), out
        assert "Candidates: 2 hosts, $50.00/mo" in out
        assert "Skipped: 1 no-IP, 1 no peer" in out
        assert "| srv-aq9003 | app_free/Default | $25.00 |" in out
        assert "| srv-aq9006 | app_free/Default | $25.00 |" in out
        assert "- app_free/Default: 2 hosts @ $25.00" in out
        assert out.endswith("Set dry_run=false to apply.")
        assert [m for m, _ in c.calls] == ["host.get", "usermacro.get"]

    def test_product_median_apply(self):
        c = self._client()
        out = run_tool(costs_import, "fill_cost_median", c, dry_run=False)
        assert "**Filled 2/2 empty-cost hosts with product median**" in out, out
        assert "Added: $50.00/mo" in out
        assert _calls(c, "usermacro.create") == [
            {"hostid": "3", "macro": COST, "value": "25.0", "description": "src:product_median"},
        ]
        assert _calls(c, "usermacro.update") == [
            {"hostmacroid": "m6", "value": "25.0", "description": "src:product_median"},
        ]

    def test_provider_grouping_finds_a_peer_across_products(self):
        c = self._client()
        out = run_tool(costs_import, "fill_cost_median", c, group_by="provider")
        assert "Candidates: 3 hosts, $75.00/mo" in out, out
        assert "Skipped: 1 no-IP, 0 no peer" in out
        assert "| Host | Provider | Median $/mo |" in out
        assert "| srv-hm9001 | Other | $25.00 |" in out
        c2 = self._client()
        applied = run_tool(costs_import, "fill_cost_median", c2, group_by="provider", dry_run=False)
        assert "Filled 3/3 empty-cost hosts with provider median" in applied
        assert [p["description"] for p in _calls(c2, "usermacro.create")] == ["src:provider_median"] * 2
        assert [p["description"] for p in _calls(c2, "usermacro.update")] == ["src:provider_median"]

    def test_a_failed_write_is_counted(self):
        def create(_p):
            raise ValueError("nope")
        c = self._client(**{"usermacro.create": create})
        out = run_tool(costs_import, "fill_cost_median", c, dry_run=False)
        assert "**Filled 1/2 empty-cost hosts with product median**" in out, out
        assert "Errors: 1" in out
