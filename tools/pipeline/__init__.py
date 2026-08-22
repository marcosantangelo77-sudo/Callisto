"""tools/pipeline — P1: the end-to-end research pipeline.

Wires the eleven previously-disconnected components into one chain:

    question
      -> decompose into a ResearchProgram        (agp/research_program.py)
      -> select sources per leaf                 (tools/sources/registry.py)
      -> fetch, recording every result           (agp/provenance.py ledger)
      -> compute in the sandbox where math       (tools/sandbox.run_python)
      -> emit artifacts backing the claim        (tools/artifacts, tools/charts)
      -> assemble a conclusion with confidence   (agp/thresholds clamps +
         derived from provenance                  tools/research_program)
      -> run the Adversary against it            (agp/adversary.Adversary)
      -> seal via AGPSession.seal, or refuse     (agp.AGPSession)

Design rules honored:
  - The model is an INJECTED dependency (PipelineModel). Tests drive the
    whole chain with a scripted fake; production passes the ProviderRouter.
    This is what lets the retrodiction harness exercise the real path
    without a live model and without network.
  - Source fetching goes through RestSource with an injectable transport,
    so tests stage fixture bytes and the no-socket guard holds.
  - Nothing here arms the live execution path or weakens a gate: every
    clamp is one-directional (min / minus), and refusal to seal is the
    default failure mode.

Modules: model (model seam + fakes), engine (the pipeline), retro
(retrodiction-harness bridge so the harness stops using StubResearcher).
"""
