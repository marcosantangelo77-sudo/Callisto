# P3 wave-3 build report — source registry expansion (branch build/sources-2)

## What landed

Registry grew 8 → 19 sources. Everything under tools/sources/ +
tests/test_build_p3_sources.py; no other instances' files touched.
Pushed through 8b01e86.

### Job 1 — the two skipped last round
- courtlistener.py: Token auth via `Authorization: Token {api_key}`
  header substitution; pagination follows the opaque `next` CURSOR url
  verbatim (docs warn ordering shifts between requests); page_size capped
  at 20 because free-tier quota (~125 req/day) dominates; 3s interval;
  missing token raises BEFORE any fetch.
- uspto_odp.py: PatentsView's old API is retired. Verified via web research
  that the stable surface is now the USPTO Open Data Portal,
  api.uspto.gov/api/v1 (data.uspto.gov transition guide). X-API-KEY
  header; GET + POST search forms over the documented simplified query
  syntax; application numbers validated locally.

### Job 2 — nine more
sec_fts (EDGAR full-text search — narrative text across filings since
2001, complements XBRL companyfacts), bea (NIPA/trade/industry accounts),
census (resconstruction, retail trade, ACS flat-array normalization),
eia v2 (seriesid shortcut + facet browse, key in header never query),
fdic (call-report financials, filter DSL injection-guarded), cftc_cot
(Socrata legacy + disaggregated COT), worldbank indicators (nulls stay
null, never imputed), semanticscholar (citation intents/TLDRs —
complements OpenAlex's breadth).

Deliberately skipped: IMF/OECD (registration walls or thin free APIs vs
World Bank overlap), NOAA (weather adds little to retrodiction value
right now), PubChem/UniProt (chemistry/protein questions are rare here;
can land later as one-hour adapters on the proven pattern).

### Wayback — the disproportionate one
wayback.py emits what tools/retrodiction/cutoff.py consumes:
snapshot_proof() resolves the closest capture at-or-before a target date,
fetches its bytes, mints PublicationProof(kind=IMMUTABLE_SNAPSHOT,
published_on=capture_date, locator=snapshot_url, content_sha256=
hash(bytes)). evidence_record() returns an EvidenceRecord ready for
CutoffEnforcer.admit(). Every failure path (no snapshot, nearest capture
after cutoff, bad locator) returns None + reason — fail-closed, never
assumed safe. Optional sign_key signs immediately so the enforcer's
signature check passes. Tests exercise the REAL ProofKind /
EvidenceRecord / CutoffEnforcer machinery end-to-end, including forged
signing keys failing closed.

### Job 3 — selection layer
SourceRegistry.select_explained(question_type) gives EVERY registered
source a SelectionDecision(name, included, score, reasons[]):
- included: ranked by fraction of the question's TOPICAL words (stopwords
  stripped: 'and', 'the', 'data'...) its best answer clause covers,
  tie-broken by provenance tier. Partial coverage still includes above
  min_score=0.34 — EIA answering 'energy prices inventories' only
  partially still bears on the question.
- skipped: reason named — "excluded by caller", "tier 4 exceeds ceiling
  1", "best answer clause covers only 17% of topical words".
select() keeps its old contract on top of it (now ranked). The
source_registry_select tool grows explain=true so conclusions can state
which sources bore on them and why the rest were ignored.

## Honest limits
- Selection matching is lexical (prefix-token overlap). It has no notion
  of synonyms ('CPI' won't match 'inflation'). Good enough at 19 sources;
  revisit if the registry triples.
- CourtListener free quota (~125 req/day) makes it a lookup source, not
  a crawl source; declared in cannot_answer.
- USPTO ODP requires an ID.me-verified account for a key — the adapter is
  tested against fixtures; first live call needs CALLISTO_USPTO_ODP_KEY.
- Wayback coverage off the popular web is sparse by nature; absence of a
  snapshot is a legitimate exclusion, not an error.
- Census 'in' parameter and multi-value facets are minimal; extended when
  a question needs them.

## Tests
tests/test_build_p3_sources.py: 42 tests. NoSocket guard installed before
any import — the SEC-403 failure mode cannot recur from this suite. All
fixtures via injectable transport; keyed adapters asserted to raise BEFORE
fetching when their key env is unset. Combined targeted run: p3 + r4 +
r1_cutoff = 77 passed.
