"""Back-compat re-exports.

``tools.schema`` was a single module; it is now a package split into
core (domain-general) and the sports plugin. Every public name that
``tools.schema`` used to expose is re-exported here unchanged so that
existing imports — ``from tools.schema import ensure_schema`` in api.py,
``from tools.schema import SCHEMA_SQL`` in tests, ``open_db`` in a dozen
tools modules — keep working with zero edits.
"""

# Import the sports plugin so its tables are registered and included in
# SCHEMA_SQL. Core stays importable without it (see core_only import path
# in tests); the application always loads plugins.
from plugins.sports import schema as _sports  # noqa: F401  (registration side effect)

from tools.schema.core import CORE_SCHEMA_SQL
from tools.schema.registry import (
    get_plugin_schemas,
    plugin_claim_extension_ddl,
    plugin_schema_sql,
    register_plugin_schema,
)

# SCHEMA_SQL is the full applied DDL: core + every registered plugin.
# Kept as a module attribute (not a literal) because tests execute it with
# db.executescript(SCHEMA_SQL) exactly as before.
SCHEMA_SQL = CORE_SCHEMA_SQL + "\n" + _sports.SPORTS_SCHEMA_SQL

# Re-export the sports-owned regime helpers at the old import path.
REGIME_BOUNDARIES = _sports.REGIME_BOUNDARIES
classify_regime = _sports.classify_regime

DB_PATH: str = __import__("os").getenv("CALLISTO_DB_PATH", "memory/callisto.db")

from tools.schema.engine import (  # noqa: E402
    ensure_schema,
    get_book_tier,
    open_db,
    vacuum_db,
)

__all__ = [
    "CORE_SCHEMA_SQL",
    "SCHEMA_SQL",
    "DB_PATH",
    "ensure_schema",
    "open_db",
    "vacuum_db",
    "get_book_tier",
    "classify_regime",
    "REGIME_BOUNDARIES",
    "register_plugin_schema",
    "get_plugin_schemas",
    "plugin_schema_sql",
    "plugin_claim_extension_ddl",
]
