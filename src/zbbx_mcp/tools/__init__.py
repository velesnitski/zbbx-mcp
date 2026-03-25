__all__ = ["WRITE_TOOLS", "register_all"]

from zbbx_mcp.resolver import InstanceResolver
from zbbx_mcp.tools import (
    hosts, problems, hostgroups, health, dashboards, items,
    triggers, templates, maintenance, events, discovery,
    configuration, scripts, services, macros, rollback_tools,
    inventory, report, alerts, users, proxies, maps, media, slack,
    infra_report, costs, traffic, dashboard_report, full_report,
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
    "delete_host_macro",
    # Rollback
    "rollback_last",
    "rollback_by_index",
    # Costs
    "import_server_costs",
    "set_bulk_cost",
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
        hosts, problems, hostgroups, health, dashboards, items,
        triggers, templates, maintenance, events, discovery,
        configuration, scripts, services, macros, rollback_tools,
        inventory, report, alerts, users, proxies, maps, media, slack,
        infra_report, costs, traffic, dashboard_report, full_report,
    ]
    for module in modules:
        module.register(mcp, resolver, skip=skip)
