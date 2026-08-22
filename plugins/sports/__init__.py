"""Sports plugin — schema registration.

Importing this package registers the sports DDL against the core
schema registry. The application imports tools.schema, which imports
this package; the core never needs to know sports exists.
"""

from plugins.sports import schema  # noqa: F401

from tools.schema.registry import register_plugin_schema

register_plugin_schema(
    "sports",
    schema.SPORTS_SCHEMA_SQL,
    claim_extension_ddl=schema.HYPOTHESIS_EXTENSION_DDL,
)
