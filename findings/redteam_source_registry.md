# RED TEAM — source registry & query builders

**Surface:** `tools/sources/registry.py`, `tools/sources/base.py`,
`tools/sources/query_builder.py`, `tools/pipeline/retrieval.py`
(RelevanceGate + independence accounting), and the engine's consumption
of `RetrievalTrace.independent_keys` (`tools/pipeline/engine.py`).

**Why this surface:** unattacked ground per the rotation list ("the source
registry and query builders — what happens when a source lies, or returns
200 with zero results"). The morning report already proved selection was
brittle on live phrasing; nobody has since attacked what happens AFTER
selection — the gate, the independence mint, and the ledger recording.
The registry grew 8 → 20 sources since the last look at it.

**Method:** property-based sweeps (random questions x parameter spaces —
the method that found the rounding bug) PLUS one differential
(live trace vs resumed trace independence counting) PLUS adversarial
inputs (200-with-error-body, echo pages, split-word spam). Two methods
new to this rotation combined.

**Families hunted:** #1 (a check that cannot fail — the status check that
isn't there), #3 (absence treated as success — empty `independent_keys`
on resume re-routes into a WEAKER counting rule), #5 (structural property
standing in for agreement — distinct source NAMES counted as independent
sources), #2 (the same prefix-match rule duplicated in registry and gate,
both with the same defect), #6-direction (errors move trust UP).

**Deliverable:** `tests/test_redteam_source_registry.py` — **12 fail on
master**, 19 honest-negative pins pass. No socket opened; all fixtures via
injectable transports.

    python3 -m pytest tests/test_redteam_source_registry.py -q

---

## CONFIRMED BREAKS

### SR1 (CRITICAL) — N identical junk documents mint N "independent sources"

`retrieval.py:446-448`: every admitted fetch adds its host to
`independent_keys`. Nothing checks whether the CONTENT differs. One
byte-identical payload served by four endpoints (a mirror, a CDN cache, a
syndication copy, or one compromised upstream) produces four independent
keys and stops retrieval at "sufficient: 4 independent sources >= required 2".
Demonstrated end-to-end: four sources admitted the SAME sha256 in round 1.
Independence is currently a property of URL spelling, not of evidence.

### SR2 (HIGH) — the relevance gate cannot distinguish evidence from echo

`RelevanceGate.judge` matches question tokens bidirectionally by prefix
(`h == t or h.startswith(t) or t.startswith(h)`) with no minimum fragment
length on the page side and no penalty for pure repetition:

- Three boilerplate fragments `"non com sta"` cover 66% of the question
  `"monetary noncommercial stationary"` (bar is 25%).
- Hyphenation/wrapping artifacts pass: `"unem ploy insu ance"` covers
  100% of "unemployment insurance".
- An error page that echoes the question scores coverage **1.0** — the
  maximum relevance signal — e.g. `{"error": "no results for <question>"}`
  is admitted as perfect evidence.

The same duplicated rule exists in `registry._overlap` (family #2), where
it also drives SELECTION (SR4). Both copies share the defect.

### SR3 (HIGH) — a resumed run beats the equivalent live run

Differential finding. Live: `trace.independent_keys = {"scholarly-aggregator"}`
→ n_indep=1 → requirement unmet → ceiling capped at 0.54. Resumed from a
checkpoint payload whose `independent_keys` field is missing/lost:
`_trace_from_payload` degrades absence to an EMPTY SET, and engine.py
~435 then takes the fallback branch `len({f.source_name for f in fetches})`,
which counts openalex and semanticscholar as TWO sources → requirement met
→ no cap. A resumed run clears the exact gate the live run failed — the
resume_boundary family again, new mechanism (empty set is ambiguous between
"genuinely zero" and "keys were lost").

Same fallback branch adds `+1 if sandbox_status == "ok"` — OUR OWN sandbox
computation counts as an INDEPENDENT SOURCE corroborating the fetches.

### SR4 (MEDIUM) — diagnostic floor admits sources off garbage tokens

`registry.select_explained`: any matched token with term-frequency ≤
len(registry)/3 earns the 0.5 score floor. Combined with the SR2 prefix
rule this means ONE random 3+-char token can select a source at full
score: `"nonk"` selects cftc_cot (matches fragment "non"); a 2,000-case
sweep found context words ADDED previously-unselected sources in 24 cases
(e.g. `'cpco'` → uspto_odp via 'cpc'). Adding words to a question must
never ADD sources; selection monotonically gets worse with more input.

### SR5 (LOW) — stopword-only questions select seven sources

`core = [w for w in q_words if w not in _STOPWORDS] or q_words` — when
every word is a stopword the guard falls back to the raw list, and
`'and'`/`'for'` match the same connectives inside answer clauses.
`select("the and for about")` returns seven sources for a question with
zero topical content. A pipeline fed degenerate decomposer output fans
out to half the registry instead of refusing.

### SR6 (HIGH) — non-200 bodies are recorded PRIMARY in the provenance ledger

`RestSource.get()` calls `_record()` BEFORE any status check, and
`_record` writes every body to the ledger with `primary=True`. Verified
end-to-end: an HTTP 500 body `{"error": "upstream exploded"}` is assigned
`SourceClass.PRIMARY` by `ProvenanceLedger.assign_source_class`. The
retriever's post-hoc `status != 200` raise is the only defense, and only
for callers that go through `IterativeRetriever` — every direct adapter
consumer (and any future one) inherits PRIMARY-trusted error text. This
is family #1: the check (status gate) lives one frame away from the
recording it must precede, and the recorder itself cannot fail.

### SR7 (MEDIUM) — injected transport silently drops POST payloads

`RestSource.post(url, payload)` builds the request body ONLY inside the
production closure `_do`; when `self._transport` is set the payload is
never passed to it. Every fixture test of a POST-based adapter verifies a
call that never carries the request production would send — the entire
no-socket test strategy is blind to malformed payloads on POST adapters
(courtlistener, clinicaltrials-style POST forms). Test/prod divergence
hidden in the seam the suite trusts most.

### SR8 (MEDIUM) — planners author confident queries from weak signals

- FDIC: any capitalized token becomes `NAME:'Jerome Powell'` — a person
  searched as a bank, plannable=True, no candidates offered. The module
  docstring says "A wrong series id produces confident nonsense — the
  worst failure this system can have"; the planner does it by construction.
- FRED/BLS: ANY fully-uppercase token containing a digit auto-resolves as
  a series id — invented `'TSLA500'` becomes
  `series_observations(series_id='TSLA500')` with `resolved=` confidence.
- Treasury: any `v\d/...` substring passes through as a dataset path
  ("See v2/docs changelog" queries dataset v2/docs).

---

## HONEST NEGATIVES (attacks that did NOT land)

1. **Empty/error containers vs the gate** — property sweep, 13 shapes ×
   3,000 random questions, ZERO admissions. The gate genuinely fails
   closed on true empties; SR2 needed content to attack with. Pinned.
2. **The morning-report selection misses** — `translate_question_type`
   now fixes all reported live misses ("clinical trials" finds
   clinicaltrials; scholarly phrasing finds openalex). Pinned.
3. **Silent registration skip** — the kalshi shim closes the
   "registered-but-broken indistinguishable from absent" hole for that
   entry; `register_all` still degrades silently for import failures
   generally, but each current module imports cleanly.
4. **Live-path family collapse** — openalex+semanticscholar collapse
   correctly whenever the trace is populated live; only the resume/
   fallback branch (SR3) un-collapses them.
5. **Rate-limiter / Retry-After** — bounded correctly (MAX_RETRY_AFTER_S),
   monotonic clock, thread-safe. Could not break it in the time budget.

## RECOMMENDED FIX DIRECTIONS (for the owning instance; nothing edited)

- SR1: dedupe admissions by content hash within a leaf; keys require
  DISTINCT content, not distinct hosts.
- SR2/SR4: drop `t.startswith(h)` (keep `h.startswith(t)` with a longer
  minimum); penalize echo-only content; make the diagnostic floor apply
  only to tokens of length ≥ 5.
- SR3: persist a sentinel distinguishing "keys lost" from "keys empty";
  never fall back to name counting; remove the sandbox +1.
- SR6: move `_record()` behind the status check; record non-2xx bodies
  with primary=False (or not at all).
