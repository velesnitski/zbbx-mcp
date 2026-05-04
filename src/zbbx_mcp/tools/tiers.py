"""Tier presets for ZABBIX_TIER env var.

Setting ZABBIX_TIER limits the registered tools to a focused subset,
which cuts the tools/list handshake cost. A typical ops session pays
~10k tokens at handshake vs ~30k for the full 154-tool catalog.

Tiers compose: every non-full tier extends ``core``. ``DISABLED_TOOLS``
still applies on top — anything listed there is removed even if the
tier would have included it.

Any tool not in any tier is still available in ``full`` (the default
when ZABBIX_TIER is unset).
"""

from __future__ import annotations

# Day-to-day Zabbix querying primitives. Every other tier extends this.
CORE_TOOLS: frozenset[str] = frozenset({
    # hosts
    "search_hosts", "get_host", "search_hosts_by_ip", "search_hosts_by_location",
    # problems
    "get_problems", "get_problem_detail", "acknowledge_problem",
    # host groups
    "get_hostgroups",
    # triggers
    "get_triggers",
    # templates
    "get_templates",
    # items
    "get_host_items", "search_items", "get_item_history", "get_graphs",
    # events
    "get_events", "get_trends",
    # dashboards
    "get_dashboards", "get_dashboard_detail", "find_host_dashboard",
    # maintenance
    "get_maintenance",
    # configuration / audit
    "get_audit_log",
    # services / SLA
    "get_services", "get_sla",
    # macros (read-only)
    "get_host_macros", "get_global_macros",
    # users / proxies / maps
    "get_users", "get_proxies", "get_maps", "get_map_detail",
    # alerts
    "get_alerts",
    # health basics
    "check_connection", "get_active_problems",
    "get_host_availability",
    # server dashboard (most-called per session)
    "get_server_dashboard",
})

# Operations / incident-response tier. Adds correlation, disruption
# detection, risk scoring, IP-rotation history, and extended health.
OPS_EXTRA: frozenset[str] = frozenset({
    # correlation + flood detector
    "get_idle_relays", "get_outage_clusters", "get_host_floods",
    # disruption detection
    "detect_service_port_split", "detect_regional_traffic_loss",
    "detect_disruption_wave", "detect_loss_drift",
    # risk + impact
    "get_at_risk_hosts", "get_disruption_blast_radius",
    # external IP history
    "get_external_ip_history", "get_recovery_score",
    # extended health
    "get_agent_unreachable", "get_stale_servers", "get_stale_items",
    # extended triggers / problems
    "get_trigger_timeline", "bulk_acknowledge",
    # traffic-side
    "detect_traffic_drops", "detect_traffic_anomalies", "get_traffic_report",
    "get_traffic_drop_timeline",
    # health analysis
    "get_health_assessment", "get_predictive_alerts", "get_alert_summary",
    "get_recent_changes",
    # logs
    "correlate_logs",
})

# Cost / billing tier.
FINANCE_EXTRA: frozenset[str] = frozenset({
    # cost ingestion
    "import_server_costs", "import_costs_by_ip", "import_cluster_ip_fees",
    "import_from_xlsx", "set_bulk_cost", "fill_cost_median",
    # cost analysis
    "analyze_cost_import", "reconcile_billing_audit",
    "find_stale_billing_ips", "detect_cost_anomalies", "export_cost_audit",
    "get_cost_summary", "get_cost_gaps", "get_cost_efficiency",
    # IP audit
    "audit_host_ips", "audit_external_ips", "classify_external_ips",
    # provider context
    "get_provider_summary", "get_unknown_providers", "identify_providers",
    "get_server_map", "get_product_summary",
})

# Executive / reporting tier.
REPORTS_EXTRA: frozenset[str] = frozenset({
    # report generators
    "generate_server_report", "generate_infra_report", "generate_full_report",
    "generate_html_report", "generate_ceo_report", "generate_service_brief",
    "generate_product_map", "export_dashboard",
    # executive analytics
    "get_executive_dashboard", "get_month_over_month", "get_fleet_risk_score",
    "get_sla_dashboard", "get_report_snapshot", "get_peak_analysis",
    "get_product_audit", "get_predictive_alerts",
    # inventory
    "get_server_map", "get_product_summary", "get_server_load",
    "get_high_cpu_servers", "get_underloaded_servers",
    "get_low_disk_servers", "get_low_memory_servers",
    "get_provider_summary", "get_unknown_providers",
    # geo
    "detect_regional_anomalies", "get_geo_traffic_trends",
    "get_service_uptime_report", "get_service_health_matrix",
    "get_traffic_drop_timeline", "get_expansion_report",
    "get_regional_density_map", "get_latency_estimate",
    "get_servers_by_ping",
    # trend analysis
    "get_trends_batch", "compare_servers", "get_health_assessment",
})

TIER_PRESETS: dict[str, frozenset[str]] = {
    "core": CORE_TOOLS,
    "ops": CORE_TOOLS | OPS_EXTRA,
    "finance": CORE_TOOLS | FINANCE_EXTRA,
    "reports": CORE_TOOLS | REPORTS_EXTRA,
}


def resolve_tier_disabled(tier_name: str, all_tools: frozenset[str]) -> frozenset[str]:
    """Return the set of tools to disable for a given tier.

    Empty frozenset for ``"full"`` (or unset / unknown) — meaning no tier
    restriction. For known tiers, returns the complement of the tier's
    enabled set within ``all_tools``.

    The caller still applies any explicit ``DISABLED_TOOLS`` on top.
    """
    if not tier_name:
        return frozenset()
    name = tier_name.strip().lower()
    if name in ("full", ""):
        return frozenset()
    enabled = TIER_PRESETS.get(name)
    if enabled is None:
        # Unknown tier — fall back to no restriction (safer than disabling
        # everything; surfaces a typo without breaking the server).
        return frozenset()
    return frozenset(all_tools - enabled)


__all__ = [
    "CORE_TOOLS",
    "OPS_EXTRA",
    "FINANCE_EXTRA",
    "REPORTS_EXTRA",
    "TIER_PRESETS",
    "resolve_tier_disabled",
]
