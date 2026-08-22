"""All source adapters in one import — registration list for the registry.

Adding a source: write tools/sources/<name>.py with a SPEC and an
Adapter class, then add one line to _ADAPTERS below. That is the whole
integration.
"""

from __future__ import annotations

from tools.sources.base import PROVENANCE_TIERS, RestSource, SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry

# name -> (spec, adapter_class)
_ADAPTERS = []


def _entry(mod_name: str, cls_name: str):
    import importlib

    mod = importlib.import_module(f"tools.sources.{mod_name}")
    return (mod.SPEC, getattr(mod, cls_name))


def register_all(registry: SourceRegistry) -> None:
    """Register every adapter that imports cleanly. A source whose module
    fails to import is skipped with a log line, never fatal — the registry
    degrades the way DomainPlugins do."""
    for mod_name, cls_name in [
        ("fred", "FredAdapter"),
        ("openalex", "OpenAlexAdapter"),
        ("clinicaltrials", "ClinicalTrialsAdapter"),
        ("federalregister", "FederalRegisterAdapter"),
        ("treasury", "TreasuryAdapter"),
        ("bls", "BlsAdapter"),
        ("wikidata", "WikidataAdapter"),
        ("gdelt", "GdeltAdapter"),
        # ── wave 3 ─────────────────────────────────────────────────────
        ("sec_fts", "SecFullTextAdapter"),
        ("courtlistener", "CourtListenerAdapter"),
        ("uspto_odp", "UsptoOdpAdapter"),
        ("bea", "BeaAdapter"),
        ("census", "CensusAdapter"),
        ("eia", "EiaAdapter"),
        ("fdic", "FdicAdapter"),
        ("cftc", "CftcCotAdapter"),
        ("worldbank", "WorldBankAdapter"),
        ("semantic_scholar", "SemanticScholarAdapter"),
        ("wayback", "WaybackAdapter"),
        ("kalshi", "KalshiAdapter"),
    ]:
        try:
            spec, cls = _entry(mod_name, cls_name)
        except ImportError:
            import logging

            logging.getLogger("callisto.source_registry").info(
                "source '%s' unavailable (import failed)", mod_name)
            continue
        registry.register(SourceAdapter(spec=spec, make_adapter=cls))
