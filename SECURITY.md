# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it privately:

1. **Do NOT open a public issue**
2. Email the maintainer or use [GitHub Security Advisories](https://github.com/velesnitski/zbbx-mcp/security/advisories/new)
3. Include steps to reproduce and potential impact

We will respond within 48 hours and provide a fix timeline.

## Supported Versions

| Version | Supported |
|---|---|
| 0.x | Yes |

## Security measures

- Zabbix API tokens are passed via environment variables, never hardcoded
- HTTPS enforced by default (HTTP blocked unless `ZABBIX_ALLOW_HTTP=1`)
- Error messages truncated to 200 chars to prevent information leakage
- Optional read-only mode via `ZABBIX_READ_ONLY=true`
- Individual tools can be disabled via `DISABLED_TOOLS`
