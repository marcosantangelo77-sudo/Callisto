# HYPOTHESIS LIFECYCLE — the OutcomeResolver seam and the inheritance rule
(2026-08-23, branch improve/rotating-0823-175824)

## Area chosen

The hypothesis lifecycle — specifically BUILD_MANDATE queue item 1's seam:
`tools/resolvers/*` (OutcomeResolver + implementations) and its consumer,
the inheritance rule in `tools/research_program.py`.

Why: BUILD_MANDATE calls OutcomeResolver "the single highest-value change in
the repo — it is what turns a betting engine into a research engine." No
improve run has owned it (CLI ×2, artifacts/sandbox, retrodiction; retrieval,
synthesis, routing, schema, checkpointing before those). And per PATTERNS.md's
highest-yield check — "for every verification layer, ask what calls it and
what happens when its input is MISSING" — this layer had exactly the defect
shape that check finds.

## Family hunted: #2 ("a fix lands in one copy while another keeps the bug"),
crossed with #1/#3 (a check whose missing input is success)

### Defect 1 (measured): two vocabularies for one outcome, no bridge — every
resolver-produced resolution was silently discarded

- The resolver side (`tools/resolvers/base.py`) speaks
  `positive / negative / indeterminate`; `_norm_outcome` maps every domain's
  raw tokens there.
- The inheritance rule (`tools/research_program.py`, `ResolutionRecord`)
  speaks `hit / miss / stale / void`; `.counted` accepted only those.
- Nothing translated between them. Both modules' docstrings point at each
  other ("B1 owns resolution... this module consumes the RESULT"), but grep
  shows zero production code performing the conversion.

Measured before/after (real code path):

    before: inherited_ceiling(12× outcome="positive", PRIMARY) == 0.55
            (SPECULATIVE_CAP — identical to ZERO resolved descendants)
    after:  same input → 0.8919

A perfectly-resolving claim earned exactly nothing from its track record. The
capstone feature of the build mandate ("a parent claim's confidence ceiling
is a function of its resolved descendants' record") could never fire from
resolver data — only from hand-built dicts in tests using the canonical
vocabulary. This is family 2 verbatim: the membership rule landed twice under
different names and neither copy knew the other existed.

Family 4 rider fixed in the same edit: unknown outcome tokens previously
fell through to "not counted" — a typo'd `"positve"` silently deleted an
observation from the parent's evidence. Now it raises ValueError. A malformed
resolution must fail loudly, not shrink n.

Mapping chosen: positive/won/true/yes/confirmed → hit; negative/lost/false/
no/retracted → miss; indeterminate/push → **stale** (unresolved-at-deadline —
it must still count toward the staleness penalty, not vanish); cancelled/n-a
→ void. Normalisation lives in ONE place (`ResolutionRecord.__post_init__`),
so both vocabularies work everywhere downstream.

### Defect 2 (measured): the domain-general prediction tables do not exist

`SqlitePredictionResolver` documents and queries:

    predictions(id, claim_id, event_id, predicted_prob, context_key, created_at)
    outcomes(prediction_id, resolved_outcome, payoff, resolved_at)

Verified by grep over tools/, agp/, plugins/, migrations/: **no migration or
schema creates them; nothing writes to them.** The resolver's tolerance of
their absence ("reports zero evidence rather than raising") is failure family
3 — absence treated as success, forever. The generic resolution path could
store nothing and honestly reported "not yet tested" for eternity.

Fix: migration `016_domain_general_predictions.py` creates both tables with:
UNIQUE(claim_id, event_id) on predictions; UNIQUE(prediction_id) on outcomes
(a claim resolves once); raw outcome tokens stored verbatim (normalisation
happens in ResolutionRecord, so new domains need no schema edits); source +
raw_json columns for outcome provenance and forensic replay. Down migration
provided (destructive, guarded with the same note as 012).

End-to-end verified against the real runner:

    apply_pending_migrations(tmp.db) → applied [1..16]
    insert prediction + outcome → SqlitePredictionResolver.summarize()
      → total=1 positive=1 hit_rate=1.00 fully_resolved=True
    inherited_ceiling(×12) → 0.8919   (was 0.55 before both fixes)
    down() drops cleanly

## Tests

6 added to tests/test_build_b4_inheritance.py
(TestResolverVocabularyBridge), each constructed to fail on pre-fix code:
bridge equivalence vs canonical vocabulary, indeterminate-counts-as-stale,
typo raises, record normalisation, and the migration→resolver→inheritance
end-to-end path. Suite: 28 passed for the file.

## What I deliberately did NOT do

- No writer for the tables beyond the migration. Who records predictions is
  a pipeline-intake decision (the decomposer should register predictive
  sub-questions at claim time); wiring that into engine.py is a separate
  unit with its own design questions. The storage now exists; the intake is
  the next run's opportunity.
- KalshiOutcomeResolver left as-is; it already emits correct vocabulary and
  now benefits from the bridge automatically.

## State of the area after this pass

Sports untouched and green (resolvers/base.py betting mapping unchanged;
migration adds only new tables). The lifecycle can now, for the first time,
actually accumulate non-sports resolution history — which unblocks the
retrodiction harness feeding descendant_resolutions for real, and is the
prerequisite for NEXT.md §7 surprise-driven exploration ever having a corpus.
