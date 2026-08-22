# Instance B3 findings — tool registry + citation grounding (build/tool-registry)

Commits: 4eb1cd3 (ToolRegistry), 3398804 (citation grounding), 22ca92a (B2
compute hook), 7b03380 (test fix). Full runnable suite green: 1129 passed,
8 skipped (excluded files are pre-existing missing-dep failures: fastapi,
polars, joblib — identical on a clean tree). Sports stayed green throughout.

## LANDED

**1. ToolRegistry / DomainPlugin (BUILD_MANDATE item 3).**
`tools/domain_registry.py` — dependency-free registry: plugins declare
domains/keywords/tool_schemas/freshness/execute; `tools_for(domain, query)`
returns core + matching plugin schemas. `tools/domains/sports.py` registers
the 21 odds tools, the freshness rules (moved verbatim from the old
`_SPORTS_FRESHNESS_PATTERN`), and the sports dispatcher (moved verbatim from
the `_execute_tool` if/elif chain — it used no instance state).

orchestrator.py:1415 `available_tools = [WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL] +
ODDS_TOOLS` is gone; `_default_registry().tools_for(session.domain,
session.query)` replaces it. `_detect_freshness` routes through the registry.
`_execute_tool` delegates to the registry first, falls through to the legacy
generic dispatcher for unknown names.

Scoping rule that matters: a plugin that *declares* domains is NOT pulled in
by keyword alone (keywords are only a fallback for keyword-only plugins) —
otherwise one plugin's keyword list would hijack every session. Sports
matches on keywords only (sports is not an AGP Domain value). A Bitcoin
question now gets exactly web_search + claude_code; a betting question gets
all 24 tools as before.

**2. Citation grounding (findings/instance4.md P1).**
- `_step_collect_evidence` builds a per-session `ProvenanceLedger`; every
  Brave search result and every tool-call return is recorded by the code
  path that executed it. Before returning, `relabel_evidence(combined,
  ledger, MAX_CONFIDENCE_BY_SOURCE)` assigns source class from provenance:
  real tool bytes → SECONDARY/PRIMARY, declared-SECONDARY without
  provenance → INFERRED (0.55), citing a genuinely fetched URL → SECONDARY.
  Demotions are logged per session.
- `_response_cites_urls` deleted; both call sites in
  `_step_escalate_to_claude` now use `ledger.cites_verified_url`.
- The non-JSON fallback no longer grants the full ceiling: it assigns
  `_clamp_confidence(MAX_CONFIDENCE_BY_SOURCE[tier.value], tier.value)` —
  an unparseable response containing "http://" gets 0.55 INFERRED, never 0.75.
- `scripts/sentinel.py` PROTECTED_FILES: `"agp.py"` → `"agp/__init__.py"`.

Defect-pinning tests updated to repair-pinning (test_tier3_epi_trust
TestCitationCheckVacuity, test_tier7_deepresearch TestCitationGrounding);
new suite `tests/test_build_b3_registry_grounding.py` (21 tests) pins both
jobs including the headline property: declared SECONDARY + invented URL →
INFERRED/0.55, no exceptions.

**3. B2 handoff (build/sandbox-artifacts).**
B2's branch ships `tools/sandbox.py` (sync hardened `run_python()`) +
`tools/artifacts.py` but no explicit diff text for the hook, so I built the
receiving end: `tools/domains/compute.py` registers `run_python` as an
`always=True` domain-general plugin via guarded import — degrades cleanly
to core tools until B2 merges, then joins every session with zero
orchestrator edits. `DomainPlugin.always` exists for exactly this class of
domain-general capability. When B2 merges, nothing on my side needs to change.

## NOTES / FOLLOW-UPS
- The registry is per-process singleton; POST /task domain/toolset fields
  (DOMAIN_GENERALITY §6) can now be a 5-line change in api.py — not my file.
- relabel_evidence can PROMOTE real tool bytes declared INFERRED to
  SECONDARY/PRIMARY — VERIFIED tier is now reachable for real documents,
  as instance4 intended.
- The Claude enhancement path's ledger is the same session ledger; a
  conclusion citing a URL from this session's actual searches earns
  SECONDARY. Fabricated URLs fail because nothing enters the ledger except
  executed tool returns.
