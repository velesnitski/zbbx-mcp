"""Parent/sub-host canonical fold tests (split from test_analytics, ADR 074)."""



class TestParentSubHostCanonicalization:
    """#138: parent + sub-host must fold to one physical machine in counts."""

    def test_build_parent_map_pairs_child_with_parent(self):
        from zbbx_mcp.data import build_parent_map

        hosts = [
            {"hostid": "p", "host": "edge-us65"},
            {"hostid": "c", "host": "edge-us65 us71"},
            {"hostid": "x", "host": "edge-de01"},
        ]
        pm = build_parent_map(hosts)
        assert pm == {"c": "p"}

    def test_canonical_dedup_via_set(self):
        from zbbx_mcp.data import build_parent_map

        # The canonical-id pattern: parent_map.get(hid, hid). After this,
        # a parent + child pair maps to one canonical id.
        hosts = [
            {"hostid": "p", "host": "edge-us65"},
            {"hostid": "c", "host": "edge-us65 us71"},
        ]
        pm = build_parent_map(hosts)
        canonical_ids = {pm.get(h["hostid"], h["hostid"]) for h in hosts}
        assert canonical_ids == {"p"}

    def test_cohesion_does_not_double_count_sub_host(self):
        # End-to-end through _compute_waves: 6 records, but two of them
        # represent parent + sub of one physical machine. After upstream
        # canonicalisation (caller's responsibility) they share hostid
        # "p_us". top-country share over distinct hostids is 2/5 = 40%.
        # Without the fix, share over 6 records would be 3/6 = 50%.
        from zbbx_mcp.tools.disruption import _compute_waves

        # Note: each record carries the canonical hostid. The tool
        # upstream of _compute_waves de-dupes traffic into the parent,
        # so this list has one record per canonical machine.
        drops = [
            {"clock": 1000, "hostid": "p_us", "host": "edge-us65",
             "subnet": "10.0.1.0/24", "hostgroup": "x", "country": "US",
             "drop_pct": 60.0},
            {"clock": 1100, "hostid": "p_us2", "host": "edge-us66",
             "subnet": "10.0.2.0/24", "hostgroup": "x", "country": "US",
             "drop_pct": 60.0},
            {"clock": 1200, "hostid": "p_de", "host": "edge-de01",
             "subnet": "10.0.3.0/24", "hostgroup": "x", "country": "DE",
             "drop_pct": 60.0},
            {"clock": 1300, "hostid": "p_id", "host": "edge-id01",
             "subnet": "10.0.4.0/24", "hostgroup": "x", "country": "ID",
             "drop_pct": 60.0},
            {"clock": 1400, "hostid": "p_mx", "host": "edge-mx01",
             "subnet": "10.0.5.0/24", "hostgroup": "x", "country": "MX",
             "drop_pct": 60.0},
        ]
        # 5 unique machines, US share = 2/5 = 40% — meets default 0.4.
        waves = _compute_waves(drops, min_hosts=5, min_subnets=3)
        assert len(waves) == 1
        assert waves[0]["host_count"] == 5
        assert abs(waves[0]["top_country_share"] - 0.4) < 1e-9

    def test_cluster_unique_host_counts_use_canonical_id(self):
        # The cluster code path: after _build_records sets r["hostid"]
        # to canonical, _cluster_problems' set comprehension dedupes
        # parent+sub to one entry.
        from zbbx_mcp.tools.correlation import _cluster_problems

        records = [
            # Parent and sub-host both have a problem in the same cluster.
            # Both records carry the canonical (parent's) hostid.
            {"clock": 1000, "hostid": "p", "host": "edge-us65",
             "name": "X", "severity": 4, "key": "10.0.0.0/24"},
            {"clock": 1050, "hostid": "p", "host": "edge-us65",
             "name": "Y", "severity": 4, "key": "10.0.0.0/24"},
            # Two distinct other hosts in the same /24.
            {"clock": 1100, "hostid": "h2", "host": "edge-us66",
             "name": "X", "severity": 4, "key": "10.0.0.0/24"},
            {"clock": 1150, "hostid": "h3", "host": "edge-us67",
             "name": "X", "severity": 4, "key": "10.0.0.0/24"},
        ]
        # 4 records but only 3 distinct canonical hosts. min_hosts=3 passes;
        # min_hosts=4 fails (would have passed without canonicalisation).
        c3 = _cluster_problems(records, window_sec=600, min_hosts=3)
        assert len(c3) == 1
        assert c3[0]["host_count"] == 3
        c4 = _cluster_problems(records, window_sec=600, min_hosts=4)
        assert c4 == []

class TestCanonicalHostGroups:
    """Pure-helper tests for canonical_host_groups (ADR 032)."""

    def test_standalone_host_one_group(self):
        from zbbx_mcp.data import canonical_host_groups
        hosts = [{"hostid": "1", "host": "solo"}]
        groups = canonical_host_groups(hosts)
        assert len(groups) == 1
        assert groups[0]["rep_host"]["host"] == "solo"
        assert groups[0]["sub_count"] == 0
        assert groups[0]["sub_hosts"] == []
        assert groups[0]["all_hostids"] == ["1"]

    def test_parent_with_subhosts_folds_to_one_group(self):
        from zbbx_mcp.data import canonical_host_groups
        # Parent "edge01" with five sub-hosts "edge01 v1".."edge01 v5"
        hosts = [{"hostid": "1", "host": "edge01"}] + [
            {"hostid": str(i + 2), "host": f"edge01 v{i + 1}"} for i in range(5)
        ]
        groups = canonical_host_groups(hosts)
        assert len(groups) == 1
        g = groups[0]
        assert g["rep_host"]["host"] == "edge01"
        assert g["sub_count"] == 5
        assert sorted(g["all_hostids"]) == ["1", "2", "3", "4", "5", "6"]

    def test_cost_uses_max_not_sum(self):
        from zbbx_mcp.data import canonical_host_groups
        # The bug we're fixing: 5 sub-hosts each at $280 → group cost = $280,
        # not $1,400. Sub-host {$COST_MONTH} macros typically duplicate the
        # parent's bill.
        hosts = [{"hostid": "1", "host": "edge01"}] + [
            {"hostid": str(i + 2), "host": f"edge01 v{i + 1}"} for i in range(5)
        ]
        cost_map = {str(i + 2): 280.0 for i in range(5)}  # only sub-hosts have macros
        groups = canonical_host_groups(hosts, cost_map=cost_map)
        assert len(groups) == 1
        assert groups[0]["cost"] == 280.0

    def test_traffic_uses_sum(self):
        from zbbx_mcp.data import canonical_host_groups
        # Each VIP has its own interface; group traffic adds across.
        hosts = [{"hostid": "1", "host": "edge01"}] + [
            {"hostid": str(i + 2), "host": f"edge01 v{i + 1}"} for i in range(3)
        ]
        traffic_map = {"2": 50.0, "3": 30.0, "4": 20.0}
        groups = canonical_host_groups(hosts, traffic_map=traffic_map)
        assert len(groups) == 1
        assert groups[0]["traffic"] == 100.0

    def test_cpu_uses_max(self):
        from zbbx_mcp.data import canonical_host_groups
        hosts = [{"hostid": "1", "host": "edge01"}] + [
            {"hostid": str(i + 2), "host": f"edge01 v{i + 1}"} for i in range(3)
        ]
        cpu_map = {"1": 10.0, "2": 25.0, "3": 80.0, "4": 15.0}
        groups = canonical_host_groups(hosts, cpu_map=cpu_map)
        assert len(groups) == 1
        assert groups[0]["cpu"] == 80.0

    def test_cost_none_when_no_subhost_has_macro(self):
        from zbbx_mcp.data import canonical_host_groups
        hosts = [
            {"hostid": "1", "host": "edge01"},
            {"hostid": "2", "host": "edge01 v1"},
        ]
        groups = canonical_host_groups(hosts, cost_map={})
        assert groups[0]["cost"] is None

    def test_mixed_subhost_and_standalone(self):
        from zbbx_mcp.data import canonical_host_groups
        hosts = [
            {"hostid": "1", "host": "edge01"},
            {"hostid": "2", "host": "edge01 v1"},
            {"hostid": "3", "host": "solo"},
        ]
        groups = canonical_host_groups(hosts)
        # 2 groups: edge01 (with 1 sub) + solo
        assert len(groups) == 2
        by_rep = {g["rep_host"]["host"]: g for g in groups}
        assert by_rep["edge01"]["sub_count"] == 1
        assert by_rep["solo"]["sub_count"] == 0

    def test_orphan_subhost_without_visible_parent_is_its_own_group(self):
        from zbbx_mcp.data import canonical_host_groups
        # A sub-host pattern but the parent isn't in the host list (e.g.
        # filtered out upstream). build_parent_map only maps when both
        # are present — so this host stands alone.
        hosts = [{"hostid": "1", "host": "edge01 v1"}]
        groups = canonical_host_groups(hosts)
        assert len(groups) == 1
        assert groups[0]["sub_count"] == 0

    def test_malformed_metric_values_dont_crash(self):
        from zbbx_mcp.data import canonical_host_groups
        hosts = [{"hostid": "1", "host": "edge01"}]
        # Defensive: bad strings, None — should be ignored gracefully.
        groups = canonical_host_groups(
            hosts,
            traffic_map={"1": "not-a-number"},  # type: ignore[dict-item]
            cost_map={"1": None},  # type: ignore[dict-item]
            cpu_map={"1": "abc"},  # type: ignore[dict-item]
        )
        assert groups[0]["traffic"] == 0.0
        assert groups[0]["cost"] is None
        assert groups[0]["cpu"] is None

class TestFoldRowsByCanonicalHost:
    """Pure-helper tests for fold_rows_by_canonical_host (ADR 034)."""

    def test_no_subhosts_passes_through(self):
        from zbbx_mcp.data import fold_rows_by_canonical_host
        rows = [
            {"host": "host-a", "uptime": 99.0},
            {"host": "host-b", "uptime": 50.0},
        ]
        out = fold_rows_by_canonical_host(rows, name_key="host")
        # Both rows preserved; no sub_count field added.
        assert len(out) == 2
        names = {r["host"] for r in out}
        assert names == {"host-a", "host-b"}
        assert all("sub_count" not in r for r in out)

    def test_subhosts_collapse_first_occurrence_wins(self):
        from zbbx_mcp.data import fold_rows_by_canonical_host
        rows = [
            {"host": "parent01", "uptime": 99.0},
            {"host": "parent01 v1", "uptime": 50.0},
            {"host": "parent01 v2", "uptime": 70.0},
        ]
        out = fold_rows_by_canonical_host(rows, name_key="host")
        assert len(out) == 1
        # First occurrence kept (the parent row at 99.0%)
        assert out[0]["host"] == "parent01"
        assert out[0]["uptime"] == 99.0
        assert out[0]["sub_count"] == 2

    def test_sort_key_makes_worst_win(self):
        from zbbx_mcp.data import fold_rows_by_canonical_host
        # Sort ascending by uptime → lowest uptime first → it wins after dedup.
        rows = [
            {"host": "parent01", "uptime": 99.0},
            {"host": "parent01 v1", "uptime": 50.0},
            {"host": "parent01 v2", "uptime": 70.0},
        ]
        out = fold_rows_by_canonical_host(
            rows, name_key="host",
            sort_key=lambda r: r["uptime"],
        )
        assert len(out) == 1
        assert out[0]["uptime"] == 50.0
        assert out[0]["host"] == "parent01"  # rewritten to canonical
        assert out[0]["sub_count"] == 2

    def test_mixed_subhosts_and_distinct_hosts(self):
        from zbbx_mcp.data import fold_rows_by_canonical_host
        rows = [
            {"host": "host-a", "v": 1},
            {"host": "parent01", "v": 2},
            {"host": "parent01 v1", "v": 3},
            {"host": "host-b", "v": 4},
        ]
        out = fold_rows_by_canonical_host(rows, name_key="host")
        names = {r["host"] for r in out}
        assert names == {"host-a", "parent01", "host-b"}
        # Only parent01 has a sub_count
        sub_counts = {r["host"]: r.get("sub_count") for r in out}
        assert sub_counts["parent01"] == 1
        assert sub_counts["host-a"] is None
        assert sub_counts["host-b"] is None

    def test_alternate_name_key(self):
        from zbbx_mcp.data import fold_rows_by_canonical_host
        # The helper should accept any key field, not just "host"
        rows = [
            {"server_name": "parent01 v1", "x": 1},
            {"server_name": "parent01 v2", "x": 2},
        ]
        out = fold_rows_by_canonical_host(rows, name_key="server_name")
        assert len(out) == 1
        assert out[0]["server_name"] == "parent01"

class TestInlineCanonicalFolds:
    """Sanity checks for the inline canonical folds added in v1.9.3.

    The seven tools (`get_high_cpu_servers`, `get_underloaded_servers`,
    `get_low_disk_servers`, `get_low_memory_servers`, `get_stale_servers`,
    `detect_traffic_drops`, `get_traffic_report`) each apply a small
    dedup-by-canonical loop inline. The pattern is exercised in three
    representative shapes here: a tuple list dedup, a (hid, value) tuple
    dedup with host lookup, and a dict-list SUM fold (`get_traffic_report`
    style).
    """

    def test_tuple_first_per_canonical_wins_after_sort(self):
        """Pattern used by `get_high_cpu_servers` / `get_underloaded_servers`."""
        from zbbx_mcp.data import canonical_host_name
        # Three sub-hosts of one box plus one standalone host. Sort desc by
        # value, then keep the first occurrence per canonical.
        items = [
            (95, {"host": "parent01 v1"}),
            (90, {"host": "parent01 v2"}),
            (80, {"host": "host-a"}),
            (75, {"host": "parent01 v3"}),
        ]
        items.sort(key=lambda x: -x[0])
        seen: set[str] = set()
        folded = []
        for val, h in items:
            cn = canonical_host_name(h.get("host", ""))
            if cn in seen:
                continue
            seen.add(cn)
            folded.append((val, h))
        assert len(folded) == 2
        # parent01 group represented once (by its worst-wins occurrence at 95)
        names = {h["host"] for _, h in folded}
        canonical_names = {canonical_host_name(n) for n in names}
        assert canonical_names == {"parent01", "host-a"}
        # Worst value (95) is the surviving parent01 entry
        parent_val = [v for v, h in folded if "parent01" in h["host"]][0]
        assert parent_val == 95

    def test_traffic_report_style_sum_fold(self):
        """Pattern used by `get_traffic_report` — SUM across sub-hosts."""
        from zbbx_mcp.data import canonical_host_name
        rows = [
            {"host": "parent01", "traffic": 100.0, "connections": 10},
            {"host": "parent01 v1", "traffic": 50.0, "connections": 5},
            {"host": "parent01 v2", "traffic": 30.0, "connections": 3},
            {"host": "host-a", "traffic": 20.0, "connections": 2},
        ]
        canonical_rows: dict[str, dict] = {}
        for r in rows:
            cn = canonical_host_name(r["host"])
            g = canonical_rows.get(cn)
            if g is None:
                canonical_rows[cn] = {**r, "host": cn}
            else:
                g["traffic"] += r["traffic"]
                g["connections"] += r["connections"]
                g["sub_count"] = g.get("sub_count", 0) + 1
        for g in canonical_rows.values():
            g["bw_per_client"] = (
                g["traffic"] / g["connections"]
                if g["connections"] > 0 else 0
            )
        out = list(canonical_rows.values())
        assert len(out) == 2
        by_host = {r["host"]: r for r in out}
        # parent01 sums to 180 traffic, 18 connections
        assert by_host["parent01"]["traffic"] == 180.0
        assert by_host["parent01"]["connections"] == 18
        assert by_host["parent01"]["sub_count"] == 2
        assert by_host["parent01"]["bw_per_client"] == 10.0
        # host-a passes through
        assert by_host["host-a"]["traffic"] == 20.0
        assert "sub_count" not in by_host["host-a"]

    def test_hostid_indirection_dedup(self):
        """Pattern used by `get_low_disk_servers` / `get_low_memory_servers`."""
        from zbbx_mcp.data import canonical_host_name
        host_map = {
            "1": {"host": "parent01"},
            "2": {"host": "parent01 v1"},
            "3": {"host": "parent01 v2"},
            "4": {"host": "host-a"},
        }
        # Already sorted worst-first (highest pct first)
        flagged = [("1", 95), ("2", 90), ("3", 80), ("4", 70)]
        seen: set[str] = set()
        folded = []
        for hid, val in flagged:
            h = host_map.get(hid, {})
            cn = canonical_host_name(h.get("host", hid))
            if cn in seen:
                continue
            seen.add(cn)
            folded.append((hid, val))
        assert len(folded) == 2
        # parent01 (worst at val=95) and host-a (val=70)
        assert [v for _, v in folded] == [95, 70]
