# Source Repair — Round 2 (2026-08-24)

Branch: `fix/source-repair-round2` (merged with `build/fed-pubmed-adapters`).
Method: every fix verified against the LIVE endpoint before commit. A
200-with-zero-results is treated as BROKEN, not OK.

## Before / After health table

| source | before (start of round) | after | what it took |
|---|---|---|---|
| cftc_cot | BROKEN (probe KeyError: name mismatch) | **OK** (2 rows) | health probe used unregistered names 'cftc'/'sec_fts'; alias logic couldn't match spec names |
| sec_fulltext | BROKEN (AttributeError list.get) | **OK** (5 rows) | probe read the raw ES envelope but the adapter returns the normalized one; probe fixed to match reality |
| eia | BROKEN (HTTP 404 /v2/seriesid/...) | **OK** (3 rows) | `/v2/seriesid/{ID}` RETIRED in the v2 reorg. Adapter now maps ids → facet routes (`COPRPUS`→steo/data?facets[seriesId], `RWTC`/`RBRTE`→petroleum/pri/spt?facets[series]); strips legacy .A/.M suffixes; raises on zero rows |
| bea | DEGRADED (200, zero rows) | BROKEN (loud) | Root cause: key `100DD694-...` is NOT ACTIVATED (BEA error code 4 — the signup email's activation link was never clicked). Also: BEA's envelope key is `BEAAPI` singular, never `BEAAPIs`; adapter now raises SourceError on embedded error payloads and on zero rows instead of silently returning nothing. **USER ACTION: click the activation link in the BEA signup email (or re-register at apps.bea.gov/api/signup).** |
| census | BROKEN ("non-JSON response") | BROKEN (clear cause) | Census now REQUIRES a key for ALL datasets: keyless requests 302 → data/missing_key.html (HTML). Adapter translates the non-JSON error into an actionable missing-key message. **USER ACTION: free key at api.census.gov/data/key_signup.html → CALLISTO_CENSUS_API_KEY.** |
| federalregister | BROKEN (HTTP 500) | **OK** (5 rows) | bare `conditions=<term>` now 500s server-side; query_term routed through `conditions[term]` (live-verified both spellings) |
| gdelt | BROKEN (429) | **OK** (5 rows) | reproduced live: identical URL 200s ~45s after a 429; self-limit raised 5s → 30s |
| semantic_scholar | BROKEN (403) | BROKEN (documented) | unauth shared pool 429/403s near-constantly (not an IP block — API invites applying for a key). Backed off 1s → 5s; **real fix: CALLISTO_S2_API_KEY** |
| bls | OK earlier tonight | BROKEN (403) | IP-level WAF block: curl itself gets 403 from this host (matches prior observations). Not a code defect; will clear when the block expires |
| clinicaltrials | flaky 403 | **OK** (5 rows) | P-256 TLS-curve pinning in base.py (istio-envoy WAF 403s X25519-first ClientHello); kept intact through merge |

Final tally: BROKEN=4 (bea, bls, census, semantic_scholar — all key/WAF
externalities), OK=17, SKIPPED=1 (uspto_odp, deliberately deferred).

## Gap adapters (task flagged)

Both already existed from task 111 on branch `build/fed-pubmed-adapters`
(built, fixture-tested) but were NOT merged into master — exactly the
kalshi trap (adapted but unusable because nothing could plan a query).

Merged and verified end-to-end:

- **federalreserve** — Board RSS feeds (`/feeds/speeches.xml`,
  `/feeds/press_all.xml`). Live: 15 speeches parsed, 5 Monetary-Policy
  items (FOMC statements/minutes subset). Registered in adapters.py,
  planner `_plan_federalreserve` in query_builder (routes FOMC/statement/
  minutes questions to monetary_policy_items, others to speeches),
  live health probe added → OK.
- **pubmed** — NCBI E-utilities esearch+esummary. Live: "semaglutide
  cardiovascular outcomes" → count 930, PMIDs returned, esummary parses
  title/journal/DOI. Planner `_plan_pubmed` extracts the topical core.
  Live health probe added → OK.

No confidence scores were touched. No fixtures were committed with keys;
no keys printed or committed.

## Offline suite

- `tests/test_build_p3_sources.py`: 45 passed (EIA tests re-pinned to the
  retired-seriesid reality; new BEA embedded-error + zero-row tests; new
  census missing-key test).
- `tests/test_build_fed_pubmed_adapters.py`: 10 passed (fixture-only, no
  sockets).
- Full offline suite (ml collection errors excluded — environmental,
  xgboost/joblib): **24 failed / 11,321 passed** vs baseline 25. All 24
  verified byte-identical failure-for-failure against origin/master in a
  clean worktree of the same scope (red-team money-path/retrieval/
  synthesis, tier7, speed golden) — pre-existing, none introduced here.
  The speed_golden fixtures were regenerated because the federalregister
  URL shape changed intentionally (conditions[term], per-field fields[]);
  answers/answers-shapes unchanged (sealed/conf values identical).
- No live calls in the offline suite; no_socket guard intact.

## User actions needed (cannot be done from here)

1. BEA: activate the API key via the link in the registration email.
2. Census: register a key (free) and add CALLISTO_CENSUS_API_KEY to
   ~/callisto-wt/.env.local.
3. Optional: Semantic Scholar API key form for CALLISTO_S2_API_KEY.
4. bls 403 is a host-level block — retry the health probe later.
