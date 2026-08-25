# RED TEAM — source registry, query authoring, adapter ingestion seam

**Surface:** the source registry and query builders — explicitly named
unattacked ground: "what happens when a source lies, or returns 200 with
zero results." Scope: `tools/sources/{registry,base,query_builder}.py`, the
adapter layer, and the ingestion seam in `tools/pipeline/retrieval.py` that
turns fetched bytes into admitted evidence. The prior retrieval passes
(`retrieval_starvation`, `retrieval_relevance`) attacked the gate's scoring
and starvation behaviour; nobody had attacked the registry/planner/adapter
stack itself or what a lying source does to it.

**Method: corrupt-one-field replay** — the one method in PATTERNS.md's
ranking never yet used here (adversarial input, differential, seam analysis,
property sweeps, and mutation are all spent). Take recorded response shapes,
corrupt ONE field at a time, and check whether any invariant notices.
The recorded shapes come from this codebase's own live probes:
`tools/sources/health.py` documents what each API actually returns on this
machine, including two 200-with-error-envelope sources. Every deterministic
repro below was verified against the live code before being written down.

Deliverable: `tests/test_redteam_source_registry.py` — **13 fail on the
pre-fix tree**, 5 honest-negative pins pass. Run:

    python3 -m pytest tests/test_redteam_source_registry.py -q

No test opens a socket; transports are injected, `urlopen` is monkeypatched
and restored.

## Families hunted

- **Family 3 (absence treated as success)** → S1: a 200 body with ZERO
  results is admitted as evidence because its metadata echoes the question.
- **Family 2 (fix lands in one copy)** → S2: the D2 fix (BLS error
  envelopes) landed keyed to BLS alone while health.py documents the same
  disease in BEA and Socrata/CFTC. Also S7 (a second, dead copy of the
  independence rule).
- **Family 1 (verification that never runs)** → S7 (`independence_family`
  has zero callers), plus the limiter claim in S3 (a control that exists
  but is not shared, so it does not bound what it claims to bound).
- **Family 9 (looks exactly like success)** → S1/S4: sufficiency declared
  on empties; attacker bytes wearing PRIMARY provenance.
- **Family 5/6** → S6 (structural near-tie set depends on iteration order),
  S5 (resolution direction wrong — confident nonsense).

---

## CONFIRMED DEFECTS

### S1 · CRITICAL — zero-result bodies become corroborated evidence

`RelevanceGate.judge` scores TEXT COVERAGE only. A fetch whose parsed body
contains zero result items is admitted whenever metadata echoes the
question's vocabulary — and query/condition echoes are real API shapes:

    {"meta": {"query": "<the question>", "count": 0}, "results": []}

admits at 80% coverage. End to end through `IterativeRetriever` with openalex
+ federalregister transports returning exactly that shape for their hosts:

    independent_keys = ['scholarly-aggregator', 'www.federalregister.gov']
    STOP REASON: sufficient: 2 independent sources >= required 2

Two unrelated hosts answering NOTHING satisfy `min_independent_sources`.
The leaf proceeds to synthesis carrying "corroborated" evidence that is
entirely metadata. Worse, when the requirement stays unmet the refine loop
re-fetches and re-admits the identical empty body every round (pinned:
2 admissions of byte-identical empties across rounds).

The strict structural route compounds it: `numeric_window_matches` admits
`{"total": 0, "date": "2023"}` for a question about 2023 — one matching
year plus any number is enough, results optional.

Nothing anywhere checks "did the source actually return anything?"
Absence must fail closed (family 3); here absence IS the success.
Tests: `test_gate_admits_zero_result_metadata_echo`,
`test_sufficiency_declared_on_two_zero_result_hosts` (headline),
`test_refinement_refetches_empty_bodies`,
`test_numeric_route_admits_empty_envelope_with_date`.

### S2 · HIGH — the 200-error-envelope chokepoint covers BLS only

`query_builder.classify_fetch_failure(source_name, ...)` begins
`if source_name != "bls": return None`. The D2 fix (findings/
retrieval_starvation.md) taught the pipeline that a 200 payload can be an
ERROR ENVELOPE — then keyed the lesson to one source.

Meanwhile `tools/sources/health.py`'s own probe table documents the same
shape elsewhere: BEA answers 200 with `BEAAPIs.Error[...]`; CFTC/Socrata
answers 200 with `{"error": ...}`. Both flow through `_fetch_one`
unclassified, reach the gate as ordinary data — and get ADMITTED, because
error descriptions echo the very parameters the planner authored from the
question ("TableName GROSSOUTPUT invalid for DataSetName GDPBYINDUSTRY"
covers a question about gross output / GDP by industry at 67%). Pinned end
to end: the BEA envelope enters `trace.admitted` as evidence.

This is family 2 verbatim: same rule, second copy untreated. Any new
source with an error-envelope habit inherits the defect silently.
Tests: `test_classify_failure_covers_bea`, `..._socrata_cftc`,
`test_bea_error_envelope_admitted_end_to_end`.

### S3 · HIGH — politeness limiter is per-instance; the claim is false

`base.py` docstring: *"Thread-safe minimum-interval limiter **shared per
source per process**."* Implementation: `self._limiter = _limiter or
_RateLimiter(...)` — every `RestSource` builds its OWN. Grep confirms zero
production callers pass `_limiter` (only tests, always with interval 0.0
to disable it). Two instances of one spec fire back-to-back: measured gap
0.000s against gdelt's claimed ~1 req/5s. The parallel fan-out comment in
retrieval.py ("politeness intervals are unchanged") holds per INSTANCE only;
parallel leaves × successive rounds multiply straight past every
self-imposed ceiling — an operational/legal exposure against live APIs and
exactly the class of thing rate-limit bans are made of.
Tests: `test_two_instances_share_no_politeness` (+ contrast pin showing a
single instance does wait — proving the defect is sharing, not the limiter).

### S4 · MEDIUM-HIGH — redirects launder provenance

`RestSource._http_transport` uses urllib, which follows redirects
transparently; `_record` binds the bytes to the REQUESTED url. `resp.geturl()`
is never consulted and `FetchRecord` has no final-url field. Demonstrated:
request `api.openalex.org/works?...`, server (or network attacker, or
misconfigured mirror) redirects to `evil.example/mirror/works` — the ledger
records those bytes `primary=True, urls=[api.openalex.org/...]`. Attacker
bytes now wear tier-1 provenance under the real host, and any model text
citing that URL re-classes SECONDARY→legit via the citation rule. Fix
shape: record final URL after redirect; refuse cross-host redirects for
tier≤2 sources.
Test: `test_record_must_bind_bytes_to_final_url_after_redirect`.

### S5 · MEDIUM — entity resolution resolves confidently to the wrong series

Two precedence bugs in `query_builder._resolve`:

1. **The uppercase-token bypass overrides the curated table.** Any question
   spelling "GDP" in caps resolves `series_id="GDP"` (Nominal GDP level,
   table confidence 0.85) before the table is consulted — GDPC1 (Real GDP,
   0.95) is unreachable for natural phrasing. "How did GDP growth compare
   with inflation in 2023?" → fetches the nominal level series.
2. **Longest-concept-wins ignores subject.** Lowercase the same question
   and it matches concepts {"gdp","inflation"} and picks by STRING LENGTH:
   inflation wins → CPI candidates for a GDP-growth subject.

Both violate the module's own contract ("Resolution NEVER silently guesses…
A wrong series id produces confident nonsense"). Tests:
`test_uppercase_bypass_overrides_curated_table`,
`test_longest_concept_wins_picks_wrong_subject`.

### S6 · LOW-MEDIUM — near-tie candidate set depends on iteration order

`translate_question_type` accumulates `best_names` with `score >= best*0.9`
against a MOVING best, and a later strictly-better score RESETS the list.
Same three scores (0.50, 0.47, 0.51), three insertion orders → membership
{high,mid,low} vs {high} vs {high}. The documented contract ("within 90%
of best") is order-invariant; the code is not. Registry order is stable
today, so impact is latent — but any re-registration order change quietly
changes which sources serve a leaf.
Test: `test_near_tie_set_is_order_invariant`.

### S7 · LOW — dead un-normalised duplicate of the independence rule

`base.independence_family()` (base.py:362) has **zero callers** anywhere
(grep-verified) and matches names RAW, while `retrieval.in_family()` /
`independence_key()` normalise spellings. Family 1 (a check nothing runs)
plus family 2 (two copies already disagreeing on 'semantic_scholar' vs
'semanticscholar'). Today's specs use the canonical spelling, so it is
latent — but whoever next touches family membership and reaches for the
base-module helper gets the un-normalised behaviour that caused the
original why.py defect.
Test: `test_base_independence_family_matches_normalised_rule`.

---

## HONEST NEGATIVES — attacks that did NOT land (kept as pins)

- **Corrupt-one-field sweep over the gate's text path (68 single-field
  corruptions across three recorded shapes):** coverage is corruption-
  STABLE under the degradation alphabet {None, "", 0, [], {}, delete-item};
  no corruption raised coverage or admitted a rejected body. The gate's
  weakness is not synthesising relevance from corrupted fields — it is the
  missing RESULT-COUNT invariant (S1). Pin:
  `test_gate_text_path_is_corruption_stable`.
- `in_family` normalisation itself is correct for all spellings tried
  (`Semantic_Scholar`, `semantic-scholar` collapse correctly).
- The diagnostic floor does NOT lower an explicit caller `min_score`
  (0.99 still excludes a floor-0.5 diagnostic match).
- `execute()` raises loudly on stale plans (AttributeError names the
  method) — plan drift fails closed.
- Selection scoring (`_overlap` prefix rules, stopword stripping) survived
  targeted probing without producing phantom matches beyond design intent.

## WHAT TO FIX (ordered by leverage)

1. **Result-count invariant at ingestion** (S1): a fetch whose parsed body
   yields zero result items for its adapter shape is a FAILED fetch (or an
   honest null), never an admission; count it toward neither n_admitted nor
   independent_keys. One per-adapter `result_paths` declaration kills the
   whole class including the numeric-route hole.
2. **Generalise classify_fetch_failure** (S2): move the envelope shapes
   from health.py's probe table into the classifier (BEAAPIs.Error,
   Socrata error, wayback snapshot status) — one rule, all sources, both
   layers reading the same table.
3. **Share the limiter per spec name** (S3): module-level dict
   {spec.name: _RateLimiter}; the docstring then becomes true.
4. **Record the final URL** (S4): add `final_url` to FetchRecord from
   `resp.geturl()`; treat cross-host redirect as failure for tier ≤ 2.
5. **Fix resolution precedence** (S5): consult the concept table BEFORE
   the exact-id bypass (bypass should require the token to be a KNOWN id
   AND not part of a longer matched concept); resolve by subject position
   or explicit mention count, not concept-string length.
6. Make near-tie selection two-pass (S6): collect max first, then take all
   ≥ 0.9·max; delete or normalise-and-use `base.independence_family` (S7).

## Relation to prior passes

The starvation pass found the gate starving honest sources; this pass found
the opposite seam feeding it junk — and that the starvation pass's own D2
fix stopped at BLS (family 2, third instance of half-landed fixes recorded
in PATTERNS.md). The confidence red team's F4 showed laundering through
citation strings; S1/S4 extend laundering to the two layers beneath it: the
fetch record (redirects) and the admission decision (empties). The money
path pass pinned rounding direction; S5 is the same disease in identifier
space — a resolution that moves certainty toward the WRONG answer.

## Reproduce

```
python3 -m pytest tests/test_redteam_source_registry.py -q
# 13 failed (defects), 5 passed (pins)
python3 -m pytest tests/test_build_w5_query_authoring.py tests/test_build_p3_sources.py \
  tests/test_build_r4_sources.py tests/test_redteam_retrieval_starvation.py -q
# unchanged vs pre-fix tree (same 4 pre-existing env-dependent failures)
```

No live API was contacted during this pass; all bodies are recorded shapes
from tools/sources/health.py and prior findings. No confidence score was
raised anywhere.
