# ADR 115 — A template can rescue a host its groups left unclassified

**Status**: Accepted (2026-08-17)
**Affected**: `src/zbbx_mcp/classify.py` (`get_template_product_map`,
`template_product_group`, `NON_SERVING_PRODUCTS`), `src/zbbx_mcp/fetch.py`
(`fetch_enabled_hosts`), `tests/test_template_fallback.py`.
**Closes**: task 187. Mirrors the reporting side's ADR 0080.

## Context

A host can sit only in a mixed host group whose members belong to several
different products. The group then classifies it as infrastructure, and it
disappears from every product-scoped view — availability, unit economics,
per-country facts — while carrying production traffic and a monthly bill.

Mapping the group to any one product is wrong by construction, because the
group genuinely holds more than one family. What separates them is the
**template**: the deploy's own statement of the stack a machine runs.

## Decision

When a host's groups classify to a **non-serving** product
(infrastructure / monitoring / unknown) and one of its templates appears in an
operator-supplied allow-list, the implied product group is **prepended** to the
host's group list inside `fetch_enabled_hosts`. Every downstream
`classify_host()` call site — there are dozens — then sees it with no signature
change. The original group is kept, so the evidence survives the rewrite.

**Groups always win when they answer.** The template is consulted only for a
host the groups declined to classify, and can never override a real product
group. That is the rule that makes this safe, and it is pinned by test.

### Configuration, not a constant

The reporting side can hardcode its allow-list because it serves exactly one
deployment. This server cannot: a hardcoded template-to-product table here
would be right for one deployment and wrong for every other. `ZABBIX_TEMPLATE_PRODUCT_MAP`
holds it instead, next to `ZABBIX_PRODUCT_MAP`, accepting JSON or
`tpl:group,tpl2:group2`.

Unset — the default — disables the feature completely, **including the extra
`selectParentTemplates` on the host fetch**, so a deployment that does not use
it pays nothing. A malformed map disables rather than half-applies.

### Why it needs both maps

Without `ZABBIX_PRODUCT_MAP`, `classify_host` returns each host's first group
name as its product, so *every* group answers and nothing is ever non-serving —
the fallback is inert. That is correct: with no product map there is no notion
of "infrastructure" to be rescued from. Pinned by test, because it looked like
a bug the first time the tests hit it.

## Consequences

- A production host in a mixed group becomes visible to product-scoped
  analytics without touching Zabbix group membership — which is shared infra
  whose groups also scope permissions and actions.
- Reclassification is logged with a count, not silent.
- The template is weaker evidence than a group and is treated that way. The
  real fix remains putting the hosts in the right group; this makes their
  absence visible in the meantime rather than papering over it.

## Verification

1002 tests pass (+14): map parsing in both forms and malformed input; a real
product group winning; unknown template and no template changing nothing; the
extra select appearing only when configured; the group prepended and the
original preserved; and the no-product-map case staying inert.
