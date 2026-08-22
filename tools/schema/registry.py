"""Plugin registration for domain schemas.

A domain plugin contributes DDL that is applied after the core schema.
The core (tools/schema/core.py) never changes when a new domain is added:
the plugin registers its tables and, if it needs claim-level domain
columns, a side table keyed to hypotheses(hypothesis_id).

Usage (in a plugin module):

    from tools.schema.registry import register_plugin_schema

    SPORTS_SCHEMA_SQL = "..."
    HYPOTHESIS_EXTENSION_DDL = "..."
    register_plugin_schema("sports", SPORTS_SCHEMA_SQL,
                           claim_extension_ddl=HYPOTHESIS_EXTENSION_DDL)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("callisto.schema.registry")

# name -> {"schema_sql": str, "claim_extension_ddl": list[str]}
_PLUGINS: dict[str, dict] = {}


def register_plugin_schema(
    name: str,
    schema_sql: str,
    claim_extension_ddl: str | None = None,
) -> None:
    """Register a plugin's DDL. Idempotent per name; last registration wins
    for the SQL body (plugins are imported once per process in practice)."""
    _PLUGINS[name] = {
        "schema_sql": schema_sql,
        "claim_extension_ddl": claim_extension_ddl or "",
    }
    n_tables = schema_sql.count("CREATE TABLE")
    logger.debug(
        "Registered plugin schema %r (%d CREATE TABLE statements)",
        name,
        n_tables,
    )


def get_plugin_schemas() -> dict[str, dict]:
    """Return all registered plugin schemas (do not mutate the result)."""
    return dict(_PLUGINS)


def plugin_schema_sql() -> str:
    """Concatenated DDL of every registered plugin, in registration order."""
    return "\n".join(p["schema_sql"] for p in _PLUGINS.values())


def plugin_claim_extension_ddl() -> str:
    """Concatenated plugin-owned side-table DDL keyed to core claims."""
    return "\n".join(
        p["claim_extension_ddl"] for p in _PLUGINS.values() if p["claim_extension_ddl"]
    )
