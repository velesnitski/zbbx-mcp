import logging
import os
from dataclasses import dataclass, field

__all__ = ["ZabbixConfig", "load_config", "load_all_configs"]


@dataclass(frozen=True)
class ZabbixConfig:
    url: str
    token: str
    read_only: bool = False
    disabled_tools: frozenset[str] = field(default_factory=frozenset)


def _validate_url(url: str) -> str:
    """Validate URL scheme. Returns empty string if blocked."""
    if url and not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        logging.getLogger("zbbx_mcp").warning(
            "ZABBIX_URL does not use HTTPS. "
            "Set ZABBIX_ALLOW_HTTP=1 to allow insecure connections."
        )
        if not os.environ.get("ZABBIX_ALLOW_HTTP"):
            return ""
    return url


def _parse_global_settings() -> tuple[bool, frozenset]:
    """Parse server-level settings shared across all instances."""
    read_only = os.environ.get("ZABBIX_READ_ONLY", "").lower() in ("1", "true", "yes")

    disabled_raw = os.environ.get("DISABLED_TOOLS", "")
    disabled = {
        t.strip().lower().replace("-", "_")
        for t in disabled_raw.split(",")
        if t.strip()
    }

    tier = os.environ.get("ZABBIX_TIER", "").strip().lower()
    if tier and tier != "full":
        # Lazy import — tools.* is heavy, only pay the cost when ZABBIX_TIER is set.
        from zbbx_mcp.tools import ALL_TOOLS
        from zbbx_mcp.tools.tiers import resolve_tier_disabled
        disabled.update(resolve_tier_disabled(tier, ALL_TOOLS))

    return read_only, frozenset(disabled)


def load_global_policy() -> tuple[bool, frozenset]:
    """Load server-level policy settings (read-only mode, disabled tools).

    These are global and not tied to any specific instance.
    """
    return _parse_global_settings()


def load_config() -> ZabbixConfig:
    url = _validate_url(os.environ.get("ZABBIX_URL", "").rstrip("/"))
    token = os.environ.get("ZABBIX_TOKEN", "")
    read_only, disabled = _parse_global_settings()

    if not url:
        raise ValueError("ZABBIX_URL is required (must be HTTPS or set ZABBIX_ALLOW_HTTP=1)")
    if not token:
        raise ValueError("ZABBIX_TOKEN is required")

    return ZabbixConfig(
        url=url,
        token=token,
        read_only=read_only,
        disabled_tools=disabled,
    )


def load_all_configs() -> dict[str, ZabbixConfig]:
    """Load configs for all Zabbix instances.

    Backward compatible: if ZABBIX_INSTANCES is not set, returns a single
    'default' instance using ZABBIX_URL / ZABBIX_TOKEN.

    Multi-instance example:
        ZABBIX_INSTANCES=prod,staging
        ZABBIX_PROD_URL=https://zabbix.prod.company.com
        ZABBIX_PROD_TOKEN=xxx
        ZABBIX_STAGING_URL=https://zabbix.staging.company.com
        ZABBIX_STAGING_TOKEN=yyy

    The first instance falls back to unprefixed ZABBIX_URL / ZABBIX_TOKEN
    if its prefixed vars are not set.
    """
    instances_raw = os.environ.get("ZABBIX_INSTANCES", "")

    if not instances_raw:
        return {"default": load_config()}

    instances = [i.strip() for i in instances_raw.split(",") if i.strip()]
    if not instances:
        return {"default": load_config()}

    read_only, disabled = _parse_global_settings()

    configs: dict[str, ZabbixConfig] = {}
    for i, name in enumerate(instances):
        prefix = name.upper()

        url = os.environ.get(f"ZABBIX_{prefix}_URL", "")
        if not url and i == 0:
            url = os.environ.get("ZABBIX_URL", "")
        url = _validate_url(url.rstrip("/"))

        token = os.environ.get(f"ZABBIX_{prefix}_TOKEN", "")
        if not token and i == 0:
            token = os.environ.get("ZABBIX_TOKEN", "")

        if not url:
            raise ValueError(
                f"Instance '{name}' is missing URL "
                f"(set ZABBIX_{prefix}_URL or ZABBIX_ALLOW_HTTP=1 for non-HTTPS)"
            )
        if not token:
            raise ValueError(
                f"Instance '{name}' is missing token (set ZABBIX_{prefix}_TOKEN)"
            )

        configs[name] = ZabbixConfig(
            url=url,
            token=token,
            read_only=read_only,
            disabled_tools=disabled,
        )

    return configs
