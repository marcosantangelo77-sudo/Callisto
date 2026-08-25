# RED TEAM — source registry, query builders, independence families

**Surface:** `tools/sources/{base,registry,query_builder}.py` plus the
independence consumers (`tools/pipeline/retrieval.py`, `tools/why.py`).
Named in the brief as unattacked ground ("what happens when a source lies,
or returns 200 with zero results"). Twelve prior surfaces were attacked,
four of them resume/checkpoint variants clustering where rotation last found
blood; the registry/planner layer had zero passes despite being the layer
MORNING_REPORT called "the single thing standing between the system and a
useful answer".

**Method: F — CROSS-MODULE**, never used as a primary instrument before:
find every copy of a rule and make the copies fight each other, then aim
adversarial inputs at the seams the disagreement exposes. Sub-methods:
differential between duplicate implementations (IND1, IND2), adversarial
"lying source" constructions end-to-end through the real retriever (SR1b),
and two property sweeps (SR2b, plus boundary work in QB2).

Deliverable: `tests/test_redteam_source_registry.py` — **12 fail on master**
(defects below), **6 honest-negative pins pass**. Run:

    python3 -m pytest tests/test_redteam_source_registry.py -q

Baseline note: `tests/test_redteam_retrieval_relevance.py` (6) and
`test_redteam_synthesis_corroboration.py` (6) carry 24 pre-existing
failures on this branch from PRIOR passes awaiting fixes. They are not
this file's.

---

## CONFIRMED DEFECTS

### SR1 · CRITICAL — dates-in-window is a relevance bypass (family 5: structure standing in for evidence)
`RelevanceGate.judge` has a second admission route
(`numeric_window_matches`, added as "D4"): any body whose date-years fall
inside the question's named years and which carries ANY digit is admitted
"even with ~0 token coverage". Its comment claims prose and catalogue rows
"gain nothing". False. A marathon/theatre news dump dated 2026-04/05
admits against "will gdp growth slow during 2026" — zero topical tokens.
End-to-end (`test_sr1b`): TWO lying hosts returning the SAME junk feed are
both admitted and the leaf stops **SUFFICIENT at min_independent_sources=2**,
with two independent voices recorded for evidence that bears on nothing.
The prior retrieval pass's 2,000-document sweeps generated dateless docs
and structurally could not see this route. Any current-year question makes
every dated feed on the internet admissible; error pages carrying
timestamps qualify too.

### SR1c · MEDIUM — the audit trail then lies about what happened (family 4)
When the D4 route admits, `judge()` returns coverage
`max(actual, min_coverage)` — the trace records `relevance: 0.25` for a
body whose true overlap is 0.0%. An auditor reading `RejectedItem`/
round-detail relevance scores cannot distinguish quarter-relevant from
wholly irrelevant. The label stands in for the evidence.

### SR2 · HIGH — years count as topical coverage (family 3/6)
Question tokens include year strings, so any body merely CONTAINING the
asked-about year earns that fraction of coverage. On a 4-token sub-question,
an FRED-style error page matching only "2023" scores 25% and is admitted
through ORDINARY token coverage — no structural route needed. Sweep
(`test_sr2b`): ~100% of random topic-free bodies dated inside the asked
year are admitted (300/300 trials).

### QB1 · HIGH — uppercase known-id passthrough hijacks concept resolution
`_resolve` checks exact-id passthrough BEFORE the curated concept table,
on any ALL-CAPS token. "GDP" is itself a curated candidate id, so:
- "Is GDP growth slowing in 2026?" plans **series_id=GDP** — the nominal
  LEVEL series — for a GROWTH question.
- "GDP per capita trend 2020" plans aggregate GDP.
Wrong quantity fetched silently; combined with SR1 the wrong-series body
is admissible (its years match the question), so nothing downstream
notices. Internally consistent, externally wrong — family 9's shape
arriving through the query planner.

### QB2 · MEDIUM-HIGH — float boundary disables half the concept tables (+ a test pinning it as correct)
`_resolve` requires `top.confidence - second.confidence >= 0.10`. The
curated tables declare pairs differing by exactly 0.10 (GDPC1/GDP
0.95/0.85, CPIAUCSL/CPILFESL 0.9/0.8, DFF/FEDFUNDS 0.9/0.8) — but IEEE-754
makes `0.95 - 0.85 == 0.09999999999999998`. Result: **gdp, inflation and
interest rates NEVER auto-resolve for FRED**; every such question returns
"ambiguous macro concept", fred goes unplannable and is silently skipped
(recorded only in `skipped_sources`). Meanwhile capitalized "GDP" resolves
to the wrong series (QB1) — there is NO casing that lands on GDPC1 except
typing "GDPC1" verbatim. Family 6 (boundary direction) compounding into
family 7: `test_build_w5_query_authoring.py::test_fred_ambiguous_concept_returns_candidates_not_a_guess`
asserts ambiguity for inflation as if intended, codifying the defect.

### RL1 · MEDIUM-HIGH — rate limiting is per-instance; the "shared per source per process" contract is fiction (family 1 shape: a protection layer that never constrains)
`_RateLimiter` documents itself "shared per source per process", but
nothing shares it — `_RateLimiter(spec.min_interval_s)` is constructed per
RestSource, and both fetch paths build fresh RestSources per call/leaf.
Measured: one client × 4 sequential fetches = ≥0.75s; four clients × 1
concurrent fetch = **~0.00s**. With parallel leaves multiplying fan-out,
worldbank's documented 2 req/s ceiling becomes leaf-count×2 req/s. This
machine has already been 403-banned by SEC and ClinicalTrials after live
testing; this defect is a plausible mechanism for earning the next ban.

### IND1 · LOW-MEDIUM — the third copy of the membership rule is still raw AND dead (families 2+1)
`tools/sources/base.py::independence_family` uses raw `in members` without
normalisation and has ZERO production callers — the live rule lives in
`retrieval.independence_key`/`in_family`. Under spellings the codebase
itself uses (`semantic_scholar` is both the module name and a planner key),
the base copy says "standalone" where the live rule says
"scholarly-aggregator". PATTERNS family 2 named exactly this file in the
last audit; the fix landed in retrieval and why.py while the base copy
kept the bug — and nobody calls it, so it is also family 1. One wiring
decision away from manufacturing an independent voice.

### IND2 · LOW — honest-gap table keyed by a spelling the registry never emits (family 4)
`query_builder._HONEST_GAPS` keys `"sec_fts"`; the registered spec name is
`"sec_fulltext"`. The carefully-worded deliberate-gap message is
unreachable; callers see `unknown source 'sec_fulltext'`. Same class as
the I2 planner-spelling bug fixed in this very file.

## What did NOT break (serious attempts kept as pins)

- **Selection still gates fabricated adapters**: a lying host whose answer
  clauses don't cover the question is never selected, so never fetched
  (`neg_selection_refuses...`). The exploit surface requires declaring
  matching vocabulary — trivial for a real adapter, but not free.
- **WorldBank collapses API error envelopes to empty rows** (meta={},
  rows=[]) and the gate rejects empty bodies — fail-closed.
- **Census raises ValueError** on unexpected response shapes rather than
  minting evidence.
- **Host fallback collapses different sources on one host to ONE voice** —
  conservative direction correct.
- **classify_fetch_failure correctly flags BLS REQUEST_NOT_PROCESSED**
  envelopes; other sources return None unchanged.
- The declared INDEPENDENCE_FAMILIES map is derived (not re-declared) by
  the only consumer that reads it — the derivation agrees today.

## Reproduce

```
python3 -m pytest tests/test_redteam_source_registry.py -q
# 12 failed (defects), 6 passed (documented behaviour/pins)
python3 -m pytest tests/test_redteam_retrieval_relevance.py \
  tests/test_redteam_synthesis_corroboration.py -q
# 12 failed — PRE-EXISTING failures from prior passes, not this pass's
```

## Recommended fix order

1. **SR1**: require the D4 route to ALSO satisfy a minimal topical floor
   (e.g. ≥1 non-year question token matched) or restrict it to adapters
   that declare time-series answers; stop inflating reported coverage when
   it fires (closes SR1c).
2. **SR2**: strip bare years from question tokens used for coverage (keep
   them for the window check only).
3. **QB1/QB2**: run concept-table matching BEFORE id-passthrough, and make
   the gap comparison integer-safe (`round(top*100) >= round(second*100)
   + 10`) or declare confidences as integers; update the pinning test to
   expect resolution.
4. **RL1**: process-wide limiter registry keyed by spec name
   (`functools.lru_cache` over spec.name holding one _RateLimiter), passed
   into every RestSource construction site.
5. **IND1**: delete `base.independence_family` or re-point it at the
   normalised rule; add the drifted-spelling differential to unit tests.
6. **IND2**: key `_HONEST_GAPS` off `SPEC.name` values imported from the
   modules, not hand-copied strings.

## Relation to prior passes

The confidence pass found inflation inside scoring; this pass found it one
layer upstream — the machinery that decides WHAT EXISTS as evidence. Same
theme as S2b (mirrors count as voices) but at acquisition: you don't need
ten mirrors of one document if two irrelevant feeds will say "two
independent sources". The registry layer's honesty contracts (cannot_answer,
min_interval_s, honest gaps) are enforced nowhere mechanically — three of
today's defects (SR1, RL1, IND2) are docstrings promising properties the
code does not implement, which is family 1 wearing a documentation hat.
