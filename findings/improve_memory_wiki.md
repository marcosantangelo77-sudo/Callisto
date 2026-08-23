# MEMORY AND WIKI LAYER — improvement pass (review/ox-alpha-0823)

**Area chosen: the memory and wiki layer** — tools/knowledge_wiki.py,
tools/hermes_memory.py, tools/memory_epistemics.py.

Why this one: every improve run so far covered CLI (twice), artifacts/sandbox,
retrodiction/calibration; waves 1-4 covered retrieval, synthesis, routing,
schema seam. Memory/wiki had only audit and red-team passes — no build-pass
run had ever asked "what would make this good". It is also the surface the
owner's coordination brief names as high-value ("Hermes is the high-value
surface"), at 29%/46% line coverage.

## What was already good (verified, not re-derived)

The epistemic repairs from instance4/P4/red-team are real and pinned by
tests: the trust escalator is dead (no MAX ratchet), article confidence is
min-of-sources fail-closed, learnings decay and trim disconfirming-biased,
rejection of tampered seals in compile ingestion. Nothing here was undone.
memory_epistemics.py needed NO changes — its admission/decay/trim logic is
correct and well-tested.

## What was wrong — measured

**1. The wiki's two LLM calls bypassed the ProviderRouter entirely.**
`_llm_compile` hardcoded `model="gemma4"` and `_detect_contradictions`
hardcoded `model="qwen3.5:4b"` via direct `OllamaInference` construction.
BUILD_MANDATE: "Never hardcode a provider or a model." The consequence is
concrete: when the owner adds a 3090 or DGX Spark, the knowledge wiki keeps
calling one named Ollama model with no health check, no failover, no cost
ledger, no empirical-routing eligibility — while every other model call in
the system routes. The compile prompt also announced "You are a knowledge
compiler for an autonomous sports betting research system" to every call.

**2. Topic extraction was sports-only vocabulary.** `_extract_topic` knew
sports + a handful of system keywords. Measured on ordinary phrasings:

    before: "clinical trial NCT123 phase 3 efficacy"  -> technical_misc
            "US unemployment rate rose to 4.2%"       -> technical_misc
            semiconductor supply chain question        -> technical_misc

Every non-sports source in the system compiled into ONE `<domain>_misc`
article per domain. The wiki's core promise — "knowledge compounds" — was
structurally sports-only, in direct violation of the scope correction
("if a design only makes sense for betting, it is the wrong design").
A Bitcoin question and a semiconductor question landed in the same article
and were compiled together as if related.

**3. Dead weight / false signals in hermes_memory.py.**
- `MESSAGES_FILE = os.path.join(os.path.dirname(DB_PATH),
  "hermes_messages.json")` — zero references anywhere; messages have always
  lived in the `hermes_messages` TABLE. It implied a file-based queue that
  does not exist (the same failure shape as the CLI docstring that claimed
  debug scripts were quarantined when they weren't).
- `_build_identity` hardcoded the proving ground as IDENTITY: book names,
  devig-vs-soft-book method, and "You are Claude Opus 4.6 — the PRIMARY
  reasoning engine" — a provider hardcode injected into EVERY prompt,
  violating BUILD_MANDATE's never-hardcode rule at the prompt layer, and
  telling every session "this system is about betting".

## What changed (4 commits)

- **3fe323f** — both wiki LLM calls now try ProviderRouter first:
  compile under a new `wiki_compile` task class (added to
  config/providers.yaml as [gpu1, ox_alpha]), contradiction detection under
  the existing `classification` class. On any router unavailability/failure/
  garbage output they fall back to the historical direct-Ollama path, so a
  machine with no providers config behaves byte-compatibly. Contradiction
  storage factored into `_store_contradictions`, shared by both paths.
  Compile prompt says "autonomous research system".
- **ef97191** — module-level `TOPIC_TAXONOMY`: 14 domain-general topics
  (macro employment/inflation/rates, clinical trials, pharma, semiconductors,
  supply chain, energy, crypto, equities valuation, real estate,
  geopolitics, AI compute, regulatory) matched by keyword count then earliest
  position. Legacy sport+market matching runs AFTER it unchanged, so all
  existing sports slugs are byte-identical; hypothesis-name extraction still
  wins first. Deliberately small and boring — routing vocabulary, not an
  ontology.
- **fe6eb97** — MESSAGES_FILE removed (with a comment explaining why);
  identity rewritten domain-general: mission, disposition unchanged, rules
  generalized (positions not bets; scored predictions; sports named as
  calibration test bed). No model name appears anywhere in it.
- **0b72370** — tests/test_improve_memory_wiki.py, 14 offline tests pinning
  all three units, including source-level pins that "gemma4" exists ONLY in
  the fallback method and that MESSAGES_FILE cannot return.

## Before/after

| measure | before | after |
|---|---|---|
| wiki LLM model choice | hardcoded gemma4/qwen3.5:4b, unrouted | providers.yaml `wiki_compile`/`classification`, failover + health + budget |
| non-sports topic filing | all → `<domain>_misc` | 5/5 probe phrasings → distinct topics |
| sports slugs | nba_moneyline etc. | byte-identical (regression-pinned) |
| identity prompt | books + "Claude Opus 4.6" hardcode | domain-general mission, zero model names |
| dead constants | MESSAGES_FILE (0 refs) | removed |
| area tests | — | +14, all offline |

Full suite on this Mac after changes: **11,079 passed / 20 failed**, where
every failure was verified pre-existing: 11 backtest_e2e (documented
failing identically on the shared checkout before my changes), 5
test_review_2026_08_23 R1/R2/R3 (intentional recorded repros from commit
ed1cc34, owned by the review pass), 4 lifecycle_claim/redteam_confidence
verified failing identically on a pristine ed1cc34 checkout built with
git archive. Sports/money/gate surfaces untouched.

## What I deliberately did NOT do

- Did NOT adopt an external memory library (e.g. mem0/letta-class tools).
  The epistemic constraints here — provenance classes, seal-gated ceilings,
  disconfirming-biased retention — ARE the product; a general memory store
  would have to be re-constrained back, which is a regression wearing a
  dependency's clothes.
- Did NOT make the taxonomy configurable in YAML. Fourteen keyword tuples
  are readable in place; a config indirection adds nobody-asked-for
  machinery. If the list grows past ~30 topics, revisit.
- Did NOT touch hermes context caching, message sanitization, or the
  WriteCoordinator path — audited, working, tested.
- Left `record_learnings_batch` (dead per ARCHITECTURE_MAP) in place: it is
  in instance-owned territory under active red-team follow-up; noted for a
  future attic run rather than risk cross-instance churn this session.

## Remaining observations for a future run

- The compile path's routed timeout (120s) and Ollama fallback duplicate
  retry policy; if ProviderRouter grows a deadline knob, use it.
- `_pending_embeds` queue has no flush trigger wired anywhere visible;
  deferred embeds appear to wait for the next write. Worth a drain-on-startup.
- Hermes section builders still query sports tables directly (bets,
  ev_opportunities); harmless today, but they will silently vanish from
  context on the workstation DB schema change — a graceful-absence test
  would be cheap.
