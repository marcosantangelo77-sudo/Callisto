# Membership rule, third copy — fix/membership-third-copy

## The defect
`tools/sources/base.py::independence_family()` tested family membership with
a raw `spec_name in members`. The canonical rule is
`tools/pipeline/retrieval.py::in_family()`: strip non-alphanumerics and
lowercase. Skipping it means a source arriving as `Semantic-Scholar` or
`semantic_scholar` did NOT collapse into `scholarly-aggregator` — it stood
alone as its own independence unit, so two dependent sources read as two
INDEPENDENT voices and inflated `min_independent_sources` counts and the
confidence built on them.

Reproduced pre-fix: `independence_family('Semantic-Scholar')` returned
`'Semantic-Scholar'`, not `'scholarly-aggregator'`.

## The fix
base.py now normalises both sides with `_norm_source_name()` — byte-identical
semantics to `retrieval.in_family()`'s regex (`[^a-z0-9]` stripped,
lowercased). No behaviour change for already-canonical names.

## Fourth-copy sweep
Grepped the entire tree (not just `in_family` — any membership test over
source names):

- `grep -rn "in members"` across all *.py: only three hits, all accounted:
  - retrieval.py:168 (`in_family`) — canonical, correct.
  - retrieval.py:183 (`independence_key` first loop) — normalised, correct.
  - base.py:353 — THE bug, fixed.
- retrieval.py:186 has a **second** raw `source_name in members` loop inside
  `independence_key` as a fallback after the normalised loop. Left as-is:
  it is dead in practice (anything matching raw also matches the normalised
  first pass) and removing it changes no observable behaviour; flagging here
  rather than unifying blindly.
- Checked agp/, scripts/, web/, config/, attic/, migrations/: no other copy.
  synthesis.py reuses `independence_key` verbatim (correct by construction).
  why.py imports `in_family` + `independence_key` from retrieval (correct).
  scripts/live_smoke_i1.py uses `independence_key` directly (correct).

Conclusion: no fourth live copy found.

## Tests
New `tests/test_membership_normalisation.py` — one test per public entry
point that performs this membership test:

1. `tools.sources.base.independence_family` — "Semantic-Scholar",
   "semantic_scholar", "SEMANTICSCHOLAR" all → "scholarly-aggregator";
   unrelated source stands alone.
2. `tools.pipeline.retrieval.in_family` — normalises BOTH sides.
3. `tools.pipeline.retrieval.independence_key` — variants collapse to the
   same key as "openalex".
4. `tools.why.independence_from_fetches` — fetches from "openalex" +
   "Semantic-Scholar" yield n_independent == 1 with a spelled-out collapse.

Each asserts the declared-family/variant match, so an unnormalised copy at
any entry point fails its own test.

## Verification
Full suite (`--ignore tests/test_ml_classifier.py tests/test_ml_drift.py`,
pre-existing collection errors): **21 failed / 11128 passed / 9 skipped —
identical to the stated baseline of 21.** All 4 new tests pass. No confidence
score was raised anywhere.

Committed 41169b3, pushed to origin/fix/membership-third-copy.
