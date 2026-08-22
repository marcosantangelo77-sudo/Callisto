"""
ToolRegistry / DomainPlugin — the orchestrator's domain extension point.

Design (BUILD_MANDATE item 3, DOMAIN_GENERALITY §2b): tools declare which
domains they serve; sessions request tools by domain + capability. Adding a
new domain means registering a plugin — NOT editing the orchestrator.

A DomainPlugin carries:
  - name                 unique id
  - domains              AGP Domain values it serves (may be empty if it
                         matches purely on query keywords)
  - keywords             regex; a query matching it is routed to this plugin
  - tool_schemas         Ollama-native tool schemas injected into prompts
  - freshness            list of (regex, brave-freshness-window) rules
  - execute              optional async (name, arguments) -> result dispatcher

The registry is deliberately dependency-free: it knows nothing about odds,
literature, or finance. Sports is simply the first registered plugin.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("callisto.domain_registry")


@dataclass
class DomainPlugin:
    name: str
    domains: set = field(default_factory=set)
    keywords: Optional[re.Pattern] = None
    tool_schemas: list = field(default_factory=list)
    freshness: list = field(default_factory=list)  # [(compiled_regex, window)]
    execute: Optional[Callable[[str, dict], Awaitable]] = None
    always: bool = False  # domain-general tools (e.g. sandboxed compute) join every session

    def serves(self, domain, query: str = "") -> bool:
        """Should this plugin's tools join the session's toolkit?

        ``always=True`` wins. Otherwise: domain match wins; a plugin that
        declares domains but does not match on domain is NOT pulled in by
        keyword alone (keywords are a fallback for plugins with no declared
        domains) — otherwise one plugin's keywords could hijack every session.
        """
        if self.always:
            return True
        if domain is not None and getattr(domain, "value", domain) in self.domains:
            return True
        if self.keywords is not None and not self.domains and query:
            try:
                return bool(self.keywords.search(query))
            except TypeError:
                return False
        return False

    def freshness_window(self, query: str) -> Optional[str]:
        for pattern, window in self.freshness:
            try:
                if pattern.search(query):
                    return window
            except TypeError:
                continue
        return None


class ToolRegistry:
    """Holds registered DomainPlugins; hands sessions the tools they need."""

    def __init__(self, core_tools: Optional[list] = None):
        self.core_tools: list = list(core_tools or [])
        self._plugins: dict[str, DomainPlugin] = {}

    def register(self, plugin: DomainPlugin) -> None:
        """Registration IS the extension point. Re-registering a name replaces."""
        if plugin.name in self._plugins:
            logger.info(f"tool registry: replacing plugin '{plugin.name}'")
        self._plugins[plugin.name] = plugin

    def unregister(self, name: str) -> None:
        self._plugins.pop(name, None)

    def plugins(self) -> list[DomainPlugin]:
        return list(self._plugins.values())

    def tools_for(self, domain, query: str = "") -> list:
        """Core tools + every matching plugin's schemas, stable order."""
        out = list(self.core_tools)
        seen = {t.get("function", {}).get("name") for t in out}
        for plugin in self._plugins.values():
            if not plugin.serves(domain, query):
                continue
            for schema in plugin.tool_schemas:
                name = schema.get("function", {}).get("name")
                if name not in seen:
                    seen.add(name)
                    out.append(schema)
        return out

    def tool_names_for(self, domain, query: str = "") -> set[str]:
        return {
            t.get("function", {}).get("name")
            for t in self.tools_for(domain, query)
        }

    def freshness_for(self, query: str) -> Optional[str]:
        """First matching freshness rule across plugins wins."""
        for plugin in self._plugins.values():
            window = plugin.freshness_window(query)
            if window:
                return window
        return None

    async def dispatch(self, name: str, arguments: dict):
        """Route a tool call to the plugin that owns it.

        Returns (handled, result). Unhandled names fall through so the
        orchestrator's legacy dispatch (and its final fallback) still runs.
        """
        for plugin in self._plugins.values():
            if plugin.execute is None:
                continue
            owned = {
                s.get("function", {}).get("name") for s in plugin.tool_schemas
            }
            if name in owned:
                return True, await plugin.execute(name, arguments)
        return False, None


_default_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Process-wide registry singleton. Idempotent; safe under concurrency."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry
