# SYNTHESIS ADOPTION — improvement pass (review/ox-alpha-0823)

**Area chosen: synthesis** — specifically, wiring the built-but-inert
cross-source synthesizer (`tools/pipeline/synthesis.py`, I3) into the only
place a confidence score is minted: `ResearchPipeline.run`, stage 6b.

Why this one, of the areas not recently covered: retrieval (w1/P1),
checkpointing (w3), routing, schema seam, CLI ×2, artifacts/sandbox,
memory/wiki, retrodiction/calibration are all taken. And this run's own
family-1 hunt had already found the shape once: **a mechanism that exists,
is tested, and nothing in production calls.** `confidence_from_agreement`
was family 1 wearing a different hat — not a verifier nobody invokes, but a
*scorer* nobody invokes. `tools/calibration/instrument.py` says it plainly:

    "synthesis_agreement"   # built (tools/pipeline/synthesis.py) —
                            # not consumed by run()

## What was wrong — measured

`findings/i3_synthesis.md` shipped an "EXACT ENGINE ADOPTION DIFF" and said
"for the merge pass". No merge pass ever applied it. On master today:

- engine.run derives parent confidence from `best_leaf.confidence` alone.
  The evidence's agreement structure is never computed.
- Nine fetches from ONE host score identically to nine independent sources
  at the parent level. The second live run's own adversary named this
  ("nine fetches, but all from one source, so independence stayed at 1")
  and MORNING_REPORT celebrated that independence "stays at 1" — but the
  number the pipeline SEALS never read it.
- A live contradiction between sources lowered nothing. The synthesizer's
  contradiction objects died in unit tests.

Reproduction on master (fixture pipeline, scripted model proposing 0.8/0.7):

    BEFORE adoption: sealed=True score=0.55 tier=PROBABLE
    AFTER  adoption: sealed=True score=0.54 tier=SPECULATIVE

The before-run sealed PROBABLE on evidence whose two leaves disagreed
numerically and whose independence was thin. After adoption the same run
seals SPECULATIVE with the reason in notes.

## What landed (one commit + tests)

`tools/pipeline/engine.py`, stage 6b, after conclusion assembly and before
the inheritance clamp:

1. Build `EvidenceItem`s from each leaf's admitted fetches; claim text is
   the leaf answer; source class replayed from session evidence;
   base_url from the registry spec (real hosts → real independence keys).
2. Thin leaves feed their retrieval traces into honest-null classification.
3. `synthesize()` produces the report → stored on
   `PipelineResult.synthesis` (serialisable dict) and
   `.contradictions`; both surfaced in `summary_dict()`.
4. **Asymmetry kept:** `proposed = min(proposed, rep.confidence)` — the
   structural score can only lower. Nothing in synthesis can raise a
   confidence.
5. When it lowers, a note names the numbers ("synthesis agreement lowered
   the parent proposal 0.70 -> 0.54: 2 independent source unit(s) ... 1
   live contradiction(s)") — no silent lowering.
6. Whole stage wrapped in try/except-log: if synthesis ever fails, sealing
   proceeds exactly as before. Degradation, never breakage of the chain.

Tests: `tests/test_synthesis_adoption.py` (6) pin: report ships on the
result; min()-only lowering; one independence unit scores like one source;
contradiction caps parent at SPECULATIVE with tier reflecting it;
synthesis failure never breaks sealing; lowering is explained in notes.

## Verification

- Full suite before the change (clean master): 11,184 passed / 34 failed.
- Full suite after: 11,189 passed (5 new adoption tests green) /
  34 failed — **byte-identical failure set** (diff of sorted FAILED lines:
  empty). All 34 pre-existing and documented: test_backtest_e2e DB
  fixtures, joblib-less ML collection errors (excluded from both runs),
  red-team repro tests that pin UNFIXED defects on purpose.
- Sports untouched; no gate, threshold, floor, or alpha changed.

## Family note for PATTERNS.md

Family 1 should read "a verification-or-scoring layer that never actually
runs". Same hunt, same method ("what calls this?"), same fix (make the seal
path its production caller). Candidate 5th instance checked this run:
`agp/ensemble.PanelVerdict` and `install_adversary()` remain declared-but-
unwired into engine.run (instrument.py documents them as present-inert);
they were NOT adopted here because they change adversary plumbing, which
belongs to whoever owns the ensemble area next.
