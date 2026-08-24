# RED TEAM — source registry & query builders

**Surface:** the source registry, query authoring (`tools/sources/query_builder.py`),
and their seam with retrieval (`tools/pipeline/retrieval.py`) — the layer that
decides WHICH sources answer a question and WHAT gets asked.

**Why this surface:** twelve prior passes cluster on resume/checkpoint (4 of
12), calibration, confidence, seal, synthesis. The source registry was named
"unattacked ground" in the standing brief; it is also the layer two live
runs already blamed ("retrieval is the weak link", "nine fetches from one
host"), which makes its guarantees load-bearing and under-tested: every test
in `tests/test_build_*sources*` uses each adapter's own vocabulary — exactly
the blind spot MORNING_REPORT documented when "clinical trials" selected
nothing.

**Method:** property-based sweeps + differential + adversarial construction.
Not yet used on this surface (prior passes here used adversarial input and
differential-between-runs).

---

## Family hunted

Family 1 (a verification layer that never runs), Family 3 (absence/200
treated as success), Family 5 (structural property standing in for
agreement), and Family 9's entity-resolution clause ("resolution NEVER
silently guesses"). All four families recur in this layer, in NEW instances
distinct from the recorded ones.

---

## Defects

### S2 · HIGH — an HTTP 200 carrying an ERROR BODY is admitted as evidence and satisfies min_independent_sources (Families 3+1)

`IterativeRetriever._fetch_one` checks only `status != 200`
(retrieval.py ~line 640). Anything else is gated purely on lexical coverage
— and error payloads name the endpoint's topic, so they pass trivially:

    {"error": "internal problem with this semiconductor supply chain
               resilience dataset endpoint; results unavailable"}

Two such hosts → `admitted=2`, `independent_keys={alpha,beta}`,
`stop_reason="sufficient: 2 independent sources >= required 2"`. The leaf
seals on two apologies. This is precisely the FDIC/ClinicalTrials family-3
defect ("200-with-zero-results = healthy") one level up: fixed at the
health-probe layer (`tools/sources/health.py` DEGRADED verdict) but NOT at
the ingestion layer that actually builds evidence.

**The sharper half (family 1):** `RestSource._record()` writes every 200
body into the ProvenanceLedger with `primary=True` at fetch time — before
the relevance gate runs. Verified:
`is_primary_bytes(error_body_json) == True`. So even a leaf where the gate
LATER rejects the body leaves PRIMARY-attested bytes for "results
unavailable" in the ledger, launderable by any later content citing them.
Provenance assigns its strongest class to bytes nobody has judged; the
gate's rejection binding exists but the pre-attestation has already
happened. Reproduced in `tests/test_redteam_sources_queries.py::test_s2_*`.

### S3 · HIGH — identical bytes from two hosts count as TWO independent voices (Family 5)

Differential: same document mirrored on gamma.example and delta.example →
`admitted=2`, `distinct content_sha256 = 1`,
`independent_keys={gamma,delta}` → **sufficient at min_independent=2 on ONE
document fetched twice**. `independence_key()` keys by host/family and never
consults `_sha`, which sits unused three lines away in the same module.
Mirror sites, CDN caches, and republished wire copy all collapse to nothing
while independence reads 2. The declared overlap families handle *adapter*
aliases but not the actual definition of corroboration: distinct CONTENT.

### S4 · MEDIUM — entity resolution silently guesses ids from capitalisation (Family 9)

`query_builder._resolve`'s exact-id passthrough accepts any fully-uppercase
token containing a digit: `"COVID19"` → `resolved={'series_id': 'COVID19'}`
with **zero candidates**, no disambiguation, contradicting the module header
("Resolution NEVER silently guesses"). End-to-end:
`build_plan('fred', "What did the FAA2023 report say about airline safety?")`
→ `series_observations(series_id='FAA2023')` — a fetch against a fabricated
id instead of the safe `series_search` fallback the planner uses when no
concept matches. Same shape fires for CFTC market codes
(`_plan_cftc("flight AA1234")` → `market_code='AA1234'`). FRED returns an
error envelope… which S2 then admits as primary evidence if the transport
returns it 200 (proxies/gateways commonly do). Confident nonsense,
composed of two individually-plausible components.

### S5 · NEGATIVE RESULT (clean)

500 randomised inputs (unicode, control chars, empty, DROP TABLE, 10k-word)
through `SourceRegistry.select_explained` over the full 21-source registry:
zero crashes, zero out-of-range scores; empty question selects nothing. The
selector hardening from the morning-report era holds.

Also probed and clean: all 19 planners × 2,000 generated questions — zero
crashes, zero `plannable=True` with empty queries (the contract `execute()`
relies on).

---

## Why these survived 11,300 tests

Every fixture test serves GOOD bodies through the injected transport, so the
200-error-body path is never exercised; every adapter test uses its own
vocabulary, so guessed-id resolution never fires; no test fetches the same
body from two hosts because fixtures are per-source by construction. All
three defects need the *combination* the live runs hit: real hosts, real
error pages, mirrored content.

## Suggested fixes (not applied — red team owns tests only)

1. Ingestion should classify known error envelopes (top-level `error` /
   `detail` / non-empty `Errors`, zero result rows against the adapter's
   expected container) BEFORE gating, and treat them like a failed fetch.
2. `RestSource._record` should record with `primary=False` (or defer) until
   the gate's admit decision; provenance may be lowered post-hoc, never
   minted pre-judgment.
3. `trace.independent_keys` should incorporate the content hash: a key set
   built from `{(independence_key(...), sha)}` or a dedupe of admitted
   shas before counting voices.
4. The exact-id passthrough should require membership in the curated id set
   OR a validated id regex per source (`_FRED_ID_RE` already exists and is
   not consulted there), returning everything else as candidates.

## Artifacts

- Tests: `tests/test_redteam_sources_queries.py`
  (3 failing repros pinned: S3×2, S4×2 — see commit; S2 currently passing
  as canary assertions documenting present behaviour, written so they fail
  the moment admission changes).
