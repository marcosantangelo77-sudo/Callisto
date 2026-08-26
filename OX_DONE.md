# OX_DONE — split tools/hypothesis_generator.py into tools.hypgen

Branch: `cursor/ox-hypgen-split-2ac0`

## Line counts

| File | Lines |
|---|---|
| tools/hypothesis_generator.py (facade, was 1684) | 764 |
| tools/hypgen/__init__.py | 21 |
| tools/hypgen/templates.py | 677 |
| tools/hypgen/prompts.py | 259 |
| tools/hypgen/persistence.py | 258 |
| tools/hypgen/seeds.py | 26 |
| tests/test_hypgen_split.py | 281 |

## What moved where

- `tools/hypgen/templates.py` — HYPOTHESIS_TEMPLATES (all 25), generator
  constants (CANDIDATE_DEDUP_SIM, PRIOR_CORPUS_SIM, WIKI_CONTEXT_TOP_K,
  NEGATIVE_EXAMPLES_N), and pure `expand_variables`.
- `tools/hypgen/prompts.py` — Claude prompt builder, grounded prompt builder,
  tolerant candidate/JSON parsing, `enforce_variance`, `avg_pairwise_distance`.
- `tools/hypgen/seeds.py` — non-fatal wrapper over thesis_seeds seed picking.
- `tools/hypgen/persistence.py` — HypgenDB connection lifecycle,
  `compute_temporal_metadata`, wiki / rejection-example / recent-theses
  retrieval, sharpening-loop wiki write-back.
- Facade keeps the public API: `HypothesisGenerator`, all constants,
  HYPOTHESIS_TEMPLATES, DB_PATH re-exported; private helpers delegate.

## Write safety

No `signal_generated` or `edge_threshold` UPDATE statements exist anywhere in
the package. The only direct SQL write is the documented sharpening-loop
INSERT OR REPLACE into wiki_articles; hypothesis creation goes exclusively
through HypothesisManager.create_hypothesis. Enforced by tests
(test_no_signal_generated_or_edge_threshold_updates,
test_persistence_only_write_is_sharpening_upsert).

## Verification

- `/tmp/callisto-pytest/bin/python -m pytest tests/test_hypgen_split.py -q`
  → 16 passed
- Pre-existing hypgen suites still pass unchanged:
  test_hypgen_variance.py + test_hypgen_integration_smoke.py +
  test_hypgen_retrieval.py → 6 passed (added `_db` setter for connection
  injection compat).
