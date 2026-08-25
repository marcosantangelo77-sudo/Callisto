# RED TEAM — source registry & query builders (2026-08-24)

Branch: `redteam/rotating-0824-225959` · Worktree: `loop/`
Repro: `python3 -m pytest tests/test_redteam_source_registry.py -q`
→ **18 failed (defects), 5 passed (pins)**. All offline; the end-to-end test
injects a transport. No confidence score was raised.

---

## Surface, method, and why

**Surface:** the source registry (`tools/sources/registry.py`), query
authoring (`tools/sources/query_builder.py`), and the adapter/gate seam where
fetched payloads meet `RelevanceGate.judge()`. Named unattacked ground in the
brief ("what happens when a source lies, or returns 200 with zero results").
Selection was live-battery-tested in MORNING_REPORT and the planners got a
known-answer harness, but nobody had attacked *identity* — what turns a
question's words into a fetch of a specific series/dataset/code — or the
payload shapes adapters hand the gate. This is load-bearing twice over:
wrong identifiers produce confident nonsense (mandate property 4), and the
gate decides which bytes ever become evidence.

**Method:** invariant checks over the question→plan space plus adversarial
payloads at the gate seam. Rotation note: all six listed attack styles have
now each been used once across passes, so nothing is untouched; I picked the
pair PATTERNS.md ranks most productive (property-style sweeps) pointed at a
module that had never been swept, plus the brief's own named scenario for
this surface (the lying source). Last pass (cli_persistence) used manual
tamper-replay; before that, mutation.

**Families hunted:** 1 (verification that never runs — hit four times
here), 3 (absence treated as success — the headline), 4 (a label standing in
for evidence — identity by spelling), 9 (internally consistent, externally
wrong — half-right entity answers). One new sub-shape for family 3 is
described under R1: **absence laundered through provenance metadata**.

---

## R1 · CRITICAL — the `_fetch` trailer pollutes relevance judging

Twelve adapters embed a provenance trailer INSIDE the parsed payload they
return (`data["_fetch"] = {url, sha256, fetched_at}` — fred.py:76, bls.py:78,
bea.py:76, census.py:69, eia.py:77, worldbank.py:67/93, cftc.py:72,
fdic.py:80, sec_fts.py:70, cmefedfut.py:134). The retrieval loop passes that
object straight into `RelevanceGate.judge()` (retrieval.py:730-731), whose
`extract_text()` flattens ALL string values — including `url`, `sha256`, and
`fetched_at`. Consequences, all reproduced:

- **R1a — zero data admitted.** FRED answers `{"observations": []}` (200 OK)
  for a question naming January 2023; the planner's own URL parameters in the
  trailer echo `observation_start=2023-01-01`; the token "2023" matches one of
  four question tokens → coverage 25% ≥ min_coverage 25% → **admitted**.
  Strip the trailer and the identical body is honestly rejected (pin R1c).
- **R1b — error envelopes admitted.** A Treasury-shaped error body
  `{"error": true, "message": "dataset not found"}` with a request URL
  containing `debt` and a question-named date admits at 50% coverage.
  Any source that echoes request parameters into URLs (all of them) gets
  partial relevance credit from its own requests.
- **R2 — end to end.** Through the real `IterativeRetriever.retrieve()` loop
  (registry select → planner → RestSource → gate), the zero-data 200 body
  lands in `trace.admitted` and becomes an Evidence item / synthesis voice.
  Absence of data masquerades as data, then votes.

This is family 3 with a new mechanism worth adding to PATTERNS.md:
**absence laundered through the record of the attempt.** The provenance
trailer exists to make fetching auditable; because it rides inside the
judged payload, the audit trail itself satisfies the quality gate.

Fix shape: strip `_fetch` (and any non-payload keys) before `judge()`;
better, judge only the body the source actually sent. Also note
`classify_fetch_failure` covers BLS only (by design today); after R1 is
fixed, other sources' 200-error-envelopes fall back to honest rejection —
acceptable, but their reason will say "irrelevant" where "fetch failed"
would be truer.

## R3 · HIGH — passthrough-before-curation identifier hijack (family 4)

`_resolve()`'s exact-id passthrough fires BEFORE curated concept matching and
accepts any fully-uppercase token containing a digit. Ordinary prose
supplies those constantly:

| planner | question | resolves to |
|---|---|---|
| fred | "Inflation trend since COVID19" | series_id `COVID19` |
| treasury | "National debt in Q1 2024" | dataset `Q1` |
| bls | "CPI inflation in Q2 2024" | series_id `Q2` |
| cftc_cot | "COT positioning … week 202401" | market_code `202401` |
| worldbank | "GDP for country X1 in 2020" | indicator_code `X1` |

Five planners confidently author fetches of identifiers the question never
supplied as identifiers. Sub-case: "What was US GDP growth in Q1 2024?"
resolves to series `GDP` (nominal level) instead of the curated table's best
candidate `GDPC1` (real GDP) — because the English word "GDP" is also a
series id, a string coincidence overrides curation. Downstream this is
family 9: a sealed answer about the wrong quantity, machinery flawless.

Fix shape: passthrough requires the token to be validated against the
source's id grammar (see R4) or appear in a known-id set AND be marked in
the question (quotes/"series X"); otherwise route to candidates like any
other ambiguity. Never let a bare quarter/acronym short-circuit.

## R4 · HIGH — four id validators exist and never run (families 1+2)

`_FRED_ID_RE`, `_BLS_ID_RE`, `_CIK_RE`, `_WB_INDICATOR_RE` are defined in
query_builder.py and referenced nowhere else (grep + AST-count both confirm;
pinned by test). Meanwhile the LIVE passthrough validates with "has a digit
or is already known". The correct checks were written and never wired —
family 1 exactly, found four more instances. Concrete harm: World Bank's own
refusal message says "supply an explicit indicator code like SP.POP.TOTL",
and supplying exactly that is unplannable (`SP.POP.TOTL` has no digits, so
passthrough skips it; no concept names match) — the documented contract is
false while the regex that would honour it sits dead three lines away.

Fix shape: use the per-source regexes inside the passthrough (they encode
real grammars: FRED `[A-Z0-9_]{4,}`, BLS `[A-Z]{2,3}[A-Z0-9]{6,}`,
CIK `\d{10}`, WB `[A-Z]{2}\.[A-Z0-9]{3}\.[A-Z0-9]{2,4}`), and add dotted-code
detection to the WB planner.

## R5 · MEDIUM — FDIC answers half a comparison, silently (family 9)

"Compare JPMorgan Chase and Wells Fargo deposits" → `resolved =
{"bank_name": "Wells Fargo"}`, no candidates, no disclosure that a second
named institution was dropped. A downstream conclusion compares one bank
with itself. Additionally, camelcase names ("JPMorgan") defeat the
proper-noun regex entirely (`[A-Z][a-z]{2,}` needs two lowercase letters
after the initial), producing an honest-gap refusal for a question the FDIC
adapter can answer via `NAME:` filter. Fix: collect ALL proper nouns into
candidates when several are present; allow internal capitals in names.

## R6 · LOW-MED — USPTO assignee extraction (families 4+9)

Two defects, one regex: (a) "patents assigned to Apple Inc and Microsoft
Corp" produces `assigneeName:"Apple Inc and Microsoft Corp"` — two entities
ANDed into one filter, guaranteed zero hits, silently answering "no such
patents" about two real assignees; (b) the pattern is case-sensitive, so
sentence-initial "Patents" disables extraction that lowercase "patents"
enables — spelling deciding plan structure.

## What held up (serious attempts, kept as pins/tests)

- **Selection monotonicity**: 400 randomized questions × random min_score
  pairs — raising the threshold never added a source; the diagnostic-score
  floor respects caller strictness (its comment promises "0.99 stays 0.99";
  verified).
- **The MORNING_REPORT selection battery** now passes: scholarly works /
  clinical trials / semiconductor-literature questions all route correctly.
  Pinned so R3/R4 fixes cannot regress it.
- **Gate fails closed on clean empties**: bodies without the `_fetch`
  trailer are rejected (R1c pin).
- **BLS quota envelopes** are classified as failures, not irrelevant data
  (existing D2 fix verified still standing).
- **FRED unknown-series hijacks fail closed at HTTP**: FRED returns real 400s,
  which `RestSource.get` raises rather than parses — the R3 hijack wastes a
  round-trip but does not fabricate data there. World Bank is different: it
  answers invalid indicators with 200 + error JSON, which today reaches the
  gate and can be admitted via URL echo (R1b mechanism).

Minor notes, no tests: `SourceRegistry.register()` silently overwrites a
duplicate name (last wins); `_tf_cache` is built once and goes stale if
adapters register after the first select (probed, not exploitable for
mis-selection today); kalshi/cmefedfut are selected for macro questions by
prefix coincidence ("rate" ⊂ vocabulary), wasting fan-out budget but not
integrity.

## Fix order (leverage-ranked)

1. Strip `_fetch`/non-payload keys before `judge()`; consider judging only
   raw response bodies (closes R1a/R1b/R2 — one seam).
2. Wire the four dead id regexes into the passthrough + dotted-code support
   for WB (closes R3's worst cases and R4 together).
3. Multi-entity candidates in FDIC/USPTO planners; case-insensitive assignee
   regex; split conjunction captures (R5/R6).
4. Add "200-with-envelope" classification beyond BLS so misdiagnosed
   "irrelevant" reasons become honest "fetch failed".

## Relation to prior passes

The confidence red team found laundering through evidence classes; artifacts
found laundering through mutable metadata about immutable bytes; this pass
finds laundering through the *request record* (R1) and through *spelling*
(R3/R6) — same families, fresh organs. The C1 lineage ("absent digest skips
verification") recurs as "the fetch record's own metadata satisfies the
content gate". And family 1 keeps scoring: four dead validators in a single
module that also contains the live, weaker check doing the same job badly.
