# MEMORY & WIKI LAYER — improvement pass (build/cli-front-door)

**Area chosen: the memory and wiki layer** (`tools/knowledge_wiki.py`,
`tools/hermes_memory.py`; `tools/embeddings.py` read as its dependency).

Why this one: every other area in the list is covered by an existing
improve_*.md or has a peer's uncommitted work in flight (the source registry
and pipeline retrieval files are mid-edit on this branch; the schema seam
likewise). The memory/wiki layer had **uncommitted, unverified work sitting in
the tree** — written ~07:00 this morning with a complete test file but no
commit, no verification, no findings record. The genuinely best improvement
available was to finish that landing properly: verify it, prove sports stays
green, attribute one adjacent failure correctly, commit, and record it.

---

## What was already in the tree (now verified and committed)

Three changes, each matching a mandate rule:

### 1. Wiki compile/lint route through the ProviderRouter (no hardcoded models)

**Before:** `_llm_compile` hardcoded `"gemma4"` and `_detect_contradictions`
hardcoded `"qwen3.5:4b"` via direct `OllamaInference` construction — violating
BUILD_MANDATE §1 ("never hardcode a provider or a model") at two seams.

**After:** both go through `router.complete(task_class, ...)` with task classes
`knowledge_compile` / `knowledge_lint`, so model-per-purpose is a config entry
in providers.yaml. The direct-Ollama path remains only as fallback when no
router exists (behaviour degrades, doesn't break). Structured-output schemas
(`_COMPILE_JSON_SCHEMA`, `_LINT_JSON_SCHEMA`) are passed to the router either
way.

**Measured:** 7 new tests; `test_compile_routes_via_provider_router` asserts
the exact task class + schema reach `complete()`.

### 2. The deferred-embedding queue became drainable

**Before:** when Ollama was down at article-write time,
`_pending_embeds` accumulated payloads that were **never retried** — those
articles were permanently invisible to semantic search (SQL/LIKE only). A
write-only queue: silent knowledge loss shaped like resilience.

**After:** `flush_pending_embeds()` drains the queue into the VectorStore
(bounded, per-item failure isolation, embed-server-down keeps the queue
intact), called opportunistically from `search()`. Queue depth exposed via
`get_embed_queue_depth()`.

**Measured:** end-to-end test — write with embed server down → queue holds 1;
server recovers → next `search()` drains it and the article is semantically
stored. Plus the negative test: server still down → queue intact.

### 3. Identity prompt de-welded

**Before:** `hermes_memory._build_identity` injected "Books: DraftKings
(primary)…", "Core method: devig sharp books…", and "You are Claude Opus 4.6"
into EVERY prompt built by the most-injected seam in the system. Two mandate
violations in one string: domain-welding (property 1) and provider-welding.

**After:** identity states the proven home turf without welding it ("Proven
home turf: quantitative edge detection against sports books; the same engine
must work for any falsifiable claim") and names no model. Test asserts no
"Opus"/book-name leakage and presence of the general-research framing.

## Verification

- New suite: `tests/test_improve_memory_wiki_layer.py` — 7 passed.
- Adjacent wiki/memory suites (wiki_semantic, wiki_loop, memory,
  redteam_prov_memory_wiki, tier3_epi_wiki_ingestion): 37 passed.
- Full branch suite: 2,166 passed / 39 failed / 12 skipped. **Every one of the
  39 failures is pre-existing at branch tip or owned by another peer's
  in-flight work** — verified by running the same files in a clean worktree at
  HEAD with only these two modules + test file copied in:
  - `test_backtest_e2e` (16) and `test_prop_scanner` (1) fail at HEAD too —
    the routing commit `9ecffbb` touched their seam; not this pass.
  - The b6/r5 workbook tests (6) fail on a missing `openpyxl` install in this
    environment, not code.
  - `test_wiki_loop::test_real_demotion_flow_writes_article` passes at HEAD +
    these files (11/11 there); it breaks only against the schema-seam peer's
    uncommitted core/plugin DDL (fresh DBs now born without a `sport`
    column; that fixture still inserts one). Their own untracked
    tests/test_improve_schema_seam.py covers the new shape (11 passing);
    their fixture needs the same fresh-shape treatment.
  - w1/w6/i1/p1 retrieval+synthesis failures are caused by those peers'
    uncommitted edits to tools/pipeline/*; they pass at HEAD.

Nothing in this area regresses anything.

## What I would still change here (not done — scoped out / needs the live DB)

1. `ProvenanceLedger` durability (MORNING_REPORT open item) touches seals more
   than wiki rows; belongs to an AGP-core pass.
2. `_extract_topic` is still sports-keyword-first (mlb/nba/... slugs); fine as
   a slug vocabulary, but GENERAL-domain sources collapse into `<domain>_misc`
   buckets that will get fat as non-sports traffic grows. Worth revisiting
   once retrodiction produces real non-sports volume.
3. `flush_pending_embeds` opens/closes a VectorStore per call rather than per
   batch item — fine at queue depth ≤500, worth batching if Ollama outages
   become common.
