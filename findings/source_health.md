# Source Health Audit — 2026-08-23

Tool: `tools/sources/health.py` — opt-in, network-gated live probes.

    CALLISTO_SOURCE_HEALTH_NET=1 python3 -m tools.sources.health [--json]

Exit code 1 if any source is DEGRADED or BROKEN. Without the env var it
refuses to run; the normal test suite never touches the network
(`tests/helpers/no_socket.py` untouched). Live tests:
`tests/test_source_health_live.py` (skip without the env var).

## Verdicts

- **OK** — reachable, known-good query returns rows, shape matches adapter.
- **DEGRADED** — HTTP 200 but ZERO results for a query that must return data.
  This is a FAILURE, not a pass: it is exactly how the ClinicalTrials and
  FDIC defects hid behind fixtures that only tested our parsing.
- **BROKEN** — unreachable (DNS / HTTP error / non-JSON) or response shape no
  longer matches what the adapter expects.
- **SKIPPED** — requires an API key that isn't configured; live health unknown.

## Per-source table (run 2026-08-23, no API keys configured)

| source | verdict | rows | evidence |
|---|---|---|---|
| openalex | OK | 5 | works search returned 5, `results[].id` present |
| treasury | OK | 5 | avg_interest_rates dataset resolves, `data[]` populated |
| fdic | OK | 5 | `filters=STALP:TX` returns institutions (host + filters DSL both fine now) |
| cftc_cot | OK | 2 | dataset `6dca-aqww` returns WTI/gold COT rows with expected keys |
| sec_fulltext | OK | 5 | FTS search normalizes to 5 hits with cik/form/filed |
| wayback | OK | 1 | closest snapshot for example.com: 20260824000046 |
| wikidata | OK | 3 | SPARQL bindings returned |
| worldbank | OK | 3 | USA GDP indicator rows |
| kalshi | OK | 5 | public market listing |
| gdelt | BROKEN | – | HTTP 429 after retries on doc query (rate-limited at probe time) |
| bls | BROKEN | – | HTTP 403 on POST /timeseries/data (no-key tier refused / blocked from this IP) |
| census | BROKEN | – | HTML "Missing Key" page instead of JSON — ACS key now REQUIRED for acs/acs1; adapter got non-JSON |
| clinicaltrials | BROKEN | – | HTTP 403 after retries (API rejected our client from this host) |
| federalregister | BROKEN | – | HTTP 400 on comma-joined `fields[]` — the known defect is STILL LIVE in federalregister.py:43 (`FIELDS` joined with commas into one `fields[]` param) |
| semanticscholar | BROKEN | – | HTTP 403 after retries (untiered rate limit / blocked without key) |
| bea | SKIPPED | – | needs CALLISTO_BEA_API_KEY |
| census* | SKIPPED | – | would pass with CALLISTO_CENSUS_API_KEY set (failure above is missing-key, not dead endpoint) |
| courtlistener | SKIPPED | – | needs CALLISTO_COURTLISTENER_TOKEN |
| eia | SKIPPED | – | needs CALLISTO_EIA_API_KEY |
| fred | SKIPPED | – | needs CALLISTO_FRED_API_KEY |
| uspto_odp | SKIPPED | – | needs CALLISTO_USPTO_ODP_KEY |

\* census verdict depends on credentials: with a key the same query should
return JSON rows; re-run after configuring keys to reclassify.

## Findings

1. **9 of 20 sources are usable right now without any credentials** (openalex,
   treasury, fdic, cftc_cot, sec_fulltext, wayback, wikidata, worldbank,
   gdelt-when-not-429'd, kalshi).
2. **federalregister.py still carries its historical defect**: `FIELDS` is a
   single comma-joined string passed as one `fields[]` value → HTTP 400. Fix:
   pass each field as a separate repeated `fields[]` param (or drop the param).
   NOT fixed here (out of scope: no behavior changes to adapters).
3. **census effectively requires an API key now** for acs/acs1 — the spec has
   `key_env_var` but the failure mode is a 200-HTML page, i.e. silent garbage;
   health check catches it as non-JSON → BROKEN.
4. **403-class sources** (bls no-key tier, clinicaltrials, semanticscholar)
   look host/IP-blocked or key-gated rather than shape-dead; re-run from a
   different egress or with keys before declaring them dead.
5. Registry name mismatches worth knowing: modules register under
   `cftc_cot`, `sec_fulltext`, `semanticscholar` — not their filenames.
6. No confidence score was raised by this work; this audit changes zero
   scoring state.

Re-run any time; the table regenerates via
`CALLISTO_SOURCE_HEALTH_NET=1 python3 -m tools.sources.health`.
