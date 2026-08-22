"""Domain plugins for Callisto.

Each subpackage is a domain: it owns its own tables (registered against
tools.schema.core via tools.schema.registry) and never requires changes
to core schema. Sports is the first plugin; a financial or literature
plugin adds tables the same way without touching core.
"""
