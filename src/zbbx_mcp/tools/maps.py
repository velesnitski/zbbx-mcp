"""Zabbix network maps."""

import httpx

from zbbx_mcp.resolver import InstanceResolver


def register(mcp, resolver: InstanceResolver, skip: set[str] = frozenset()) -> None:

    if "get_maps" not in skip:

        @mcp.tool()
        async def get_maps(instance: str = "") -> str:
            """Get Zabbix network maps with element and link counts.

            Args:
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("map.get", {
                    "output": ["sysmapid", "name", "width", "height", "expandproblem"],
                    "selectSelements": "count",
                    "selectLinks": "count",
                    "sortfield": "name",
                })

                if not data:
                    return "No maps found."

                lines = []
                for m in data:
                    elements = m.get("selements", "?")
                    links = m.get("links", "?")
                    size = f"{m.get('width', '?')}×{m.get('height', '?')}"
                    lines.append(
                        f"- **{m.get('name', '?')}** — "
                        f"{elements} elements, {links} links ({size}) "
                        f"(mapid: {m.get('sysmapid', '?')})"
                    )

                return f"**Found: {len(data)} maps**\n\n" + "\n".join(lines)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"

    if "get_map_detail" not in skip:

        @mcp.tool()
        async def get_map_detail(map_id: str, instance: str = "") -> str:
            """Get detailed view of a network map with all elements and links.

            Args:
                map_id: Map ID
                instance: Zabbix instance name (optional, for multi-instance setups)
            """
            try:
                client = resolver.resolve(instance)
                data = await client.call("map.get", {
                    "sysmapids": [map_id],
                    "output": "extend",
                    "selectSelements": "extend",
                    "selectLinks": "extend",
                })

                if not data:
                    return f"Map '{map_id}' not found."

                m = data[0]
                parts = [
                    f"# Map: {m.get('name', '?')}",
                    "",
                    f"**ID:** {m.get('sysmapid', '?')}",
                    f"**Size:** {m.get('width', '?')}×{m.get('height', '?')}",
                ]

                element_types = {
                    "0": "Host", "1": "Map", "2": "Trigger",
                    "3": "Host group", "4": "Image",
                }

                elements = m.get("selements", [])
                if elements:
                    parts.append(f"\n## Elements ({len(elements)})")
                    for e in elements:
                        etype = element_types.get(e.get("elementtype", ""), "?")
                        label = e.get("label", "?")
                        parts.append(f"- [{etype}] {label}")

                links = m.get("links", [])
                if links:
                    parts.append(f"\n## Links ({len(links)})")
                    for link in links[:20]:
                        color = link.get("color", "")
                        parts.append(
                            f"- Element {link.get('selementid1', '?')} ↔ "
                            f"Element {link.get('selementid2', '?')} "
                            f"(color: #{color})"
                        )
                    if len(links) > 20:
                        parts.append(f"*... and {len(links) - 20} more links*")

                return "\n".join(parts)
            except (httpx.HTTPError, ValueError) as e:
                return f"Error querying Zabbix: {e}"
