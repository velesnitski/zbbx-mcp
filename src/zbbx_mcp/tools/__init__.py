__all__ = ["WRITE_TOOLS", "register_all"]

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import (
    alerts,
    analysis,
    audit,
    availability,
    ceo_report,
    configuration,
    costs,
    dashboard_report,
    dashboards,
    discovery,
    domains,
    events,
    executive,
    full_report,
    geo_health,
    geo_traffic,
    health,
    hostgroups,
    hosts,
    html_report,
    infra_report,
    inventory_load,
    inventory_map,
    items,
    macros,
    maintenance,
    maps,
    media,
    problems,
    proxies,
    report,
    rollback_tools,
    scripts,
    service_brief,
    services,
    slack,
    templates,
    traffic,
    trends_compare,
    trends_health,
    triggers,
    users,
    web_scenarios,
)

# Tools that modify data — blocked in read-only mode
WRITE_TOOLS = frozenset({
    "acknowledge_problem",
    # Host CRUD
    "create_host",
    "update_host",
    "delete_host",
    # Host group CRUD
    "create_hostgroup",
    "delete_hostgroup",
    # Item CRUD
    "create_item",
    "update_item",
    "delete_item",
    # Trigger CRUD
    "create_trigger",
    "update_trigger",
    "delete_trigger",
    # Template linking
    "link_template",
    "unlink_template",
    # Maintenance
    "create_maintenance",
    "delete_maintenance",
    # Configuration
    "import_configuration",
    # Scripts
    "execute_script",
    # Macros
    "set_host_macro",
    "set_bulk_macro",
    "delete_host_macro",
    # Rollback
    "rollback_last",
    "rollback_by_index",
    # Costs
    "import_server_costs",
    "import_costs_by_ip",
    "set_bulk_cost",
    "fill_cost_median",
    "import_from_xlsx",
})


def register_all(
    mcp,
    resolver: InstanceResolver,
    read_only: bool = False,
    disabled_tools: frozenset[str] = frozenset(),
):
    skip: set[str] = set()

    if read_only:
        skip.update(WRITE_TOOLS)

    if disabled_tools:
        skip.update(disabled_tools)

    modules = [
        hosts, problems, hostgroups, health, dashboards, items, availability, audit,
        triggers, templates, maintenance, events, discovery,
        configuration, scripts, services, macros, rollback_tools,
        inventory_map, inventory_load, report, alerts, users, proxies, maps, media, slack, domains,
        infra_report, costs, traffic, dashboard_report, full_report,
        trends_compare, trends_health,
        html_report, geo_traffic, geo_health, executive, ceo_report, service_brief, analysis,
        web_scenarios,
    ]
    for module in modules:
        module.register(mcp, resolver, skip=skip)
