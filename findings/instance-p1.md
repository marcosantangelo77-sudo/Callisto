# P1 — End-to-End Pipeline (build/pipeline), 2026-08-22

## What landed

`tools/pipeline/` — three modules wiring the eleven previously-disconnected
components into one chain:

    question -> decompose (agp/research_program.py via injected model)
             -> select sources per leaf (tools/sources/registry.py)
             -> fetch with injectable transport; every body recorded in
                ProvenanceLedger as primary bytes (agp/provenance.py)
             -> optional sandbox compute when the model asks for it
                (tools/sandbox.run_python)
             -> artifacts sealed into the content-addressed store
                (tools/artifacts.py store_sandbox_outputs; charts verified)
             -> leaf confidence = min(model proposal, provenance-assigned
                source-class ceiling, evidence-requirement gate)
             -> parent confidence = tools/research_program.clamp_parent_confidence
                (inheritance rule: zero resolved descendants cap at 0.55)
             -> Adversary attack + apply_verdict (agp/adversary.py),
                dissent logged SUSTAINED/OVERRULED
             -> AGPSession.seal() or honest refusal

**The model is an injected dependency** (`tools/pipeline/model.py`:
PipelineModel / RouterModel / ScriptedModel). This is what lets
`tools/pipeline/retro.py::PipelineResearcher` implement the retrodiction
harness's Researcher seam — the harness no longer needs StubResearcher to be
exercised end-to-end.

Tests: `tests/test_build_p1_pipeline.py` (8), `tests/test_build_p1_retro.py`
(2), `tests/test_build_p1_findings.py` (6). All offline: fixture transport +
ScriptedModel, no_socket guard installed. Neighboring suites re-run green
(sources, resolvers, research program, inheritance, adversary, sandbox,
artifacts, harness, agp seal).

## Findings — what did not fit cleanly

1. **SourceRegistry.select is word-overlap, not semantic.** The decomposer's
   `question_type` must share >=3-char vocabulary with the adapters'
   `answers` strings or selection returns []. "federal register documents"
   selects nothing; "final/proposed agency rules with dates and docket refs"
   selects federalregister. The pipeline passes question_type through
   unchanged rather than papering over this with a synonym hack.

2. **Generic fetch covers 4 of 8 sources.** openalex, federalregister,
   clinicaltrials, gdelt have a no-parameter search call. fred (API key +
   series id), bls (POST + series ids), treasury (dataset name), wikidata
   (raw SPARQL) need query authoring by the model — a real capability gap,
   skipped honestly (logged) rather than inventing queries.

3. **run_python deletes its workspace**, so sandbox artifacts are attested
   only by child-reported hashes (`meta["attested_by_child_only"]=True`)
   unless keep_workspace=True. Code-hash provenance chain is intact; file
   bytes are not independently re-hashed.

4. **ProvenanceLedger is memory-only per process.** Nothing persists fetch
   records; a restart loses the evidence a seal was grounded in. Fine for
   one run, a durability gap for the system.

5. **SPECULATIVE_CAP == TIER_PROBABLE_MIN (0.55)** in
   tools/research_program.py — the "zero resolved descendants caps at
   SPECULATIVE" docstring sits exactly on the PROBABLE boundary. Verified,
   not changed (not my file): callers should not treat tier==PROBABLE as
   inheritance failure.

6. **Adversary.apply_verdict returns a reason string for mere penalties
   too**, not just BLOCKING vetoes. A first-draft pipeline treated any
   truthy reason as a veto and refused seals that should have gone through
   with lowered confidence. Only `is_blocking` may veto; penalties lower
   the score. Pinned by test.

7. **RestSource does not raise on non-200 from an injected transport** —
   status is returned but get_json parses whatever body arrived. The
   pipeline checks last_record.status itself. Worth a look by whoever owns
   base.py.

## Honest scope notes

- The orchestrator.py integration point (real sessions dispatching through
  this pipeline) is NOT wired — orchestrator.py is another instance's file.
  The pipeline is importable and driven directly; the handoff seam is
  `ResearchPipeline(model=RouterModel(router), adversary_router=router)`.
- The decomposer prompt asks the model for evidence requirements and
  horizons; a weak model can emit requirements that are trivially met.
  The gates themselves (unmet_reasons, clamps) are enforced downstream
  regardless of what the model proposes — asymmetry holds.
