# Prior Art Survey — Open Deep-Research / Multi-Agent Frameworks

**Date:** 2026-08-23 · **Method:** live fetches of repo LICENSE files, GitHub metadata API, READMEs,
and (for Skywork) actual planner/memory/optimizer source on `main` and `v1.0.0` tags. Every license
below was read from the repo itself, not recalled from memory. Claims I could not verify are marked
UNVERIFIED. Nothing was vendored; this document only identifies and ranks.

**Grounding in our code:** `tools/pipeline/engine.py` (stages: `decompose` → per-leaf
select/fetch/compute/answer → confidence clamp → adversary → seal; see header comment lines 7–22),
`agp/research_program.py` (ResearchProgram schema), `tools/pipeline/model.py:142`
(`decompose_messages` — one LLM call), `memory.py` (domain-partitioned SQLite+WAL, `world_{domain}`
views, CHECK floor on confidence_score).

---

## 0. The line (restated, because everything below is graded against it)

> **FORBIDDEN side:** any automated actor may not raise a confidence score or weaken a threshold.
> We already survived a self-repair routine that responded to "nothing passes" by lowering the bar
> (`engine.py:21` encodes the lesson: *every confidence adjustment in this file is min(...) or minus*).
>
> **ALLOWED side:** optimising planning, decomposition, prompts, tool selection, routing.

Two extra corollaries this survey adds, learned from what these repos actually ship:

- **C1.** If you ever tune a prompt (e.g., the decomposer) with an automated optimizer, the reward
  signal MUST be ground truth (retrodiction hit rate, sealed-conclusion correctness on past runs) —
  NEVER "did the output pass the gate." Optimizing against gate passage is Goodhart with a key to
  the vault: it will discover that fewer requirements → easier passes.
- **C2.** Memory writes may inform **retrieval and planning**, never **scoring**. A remembered fact
  may suggest a new sub-question or a better query; it may never raise another entry's
  `confidence_score`, relax an `EvidenceRequirement`, or move `DB_CONFIDENCE_FLOOR`.

---

## 1. Executive ranking (value to Callisto × implementation leverage)

Scores are 1–5 each; product shown. "Side" = which side of the invariant line the idea lives on.

| # | Idea (named precisely) | From | Value | Leverage | Score | Side |
|---|------------------------|------|-------|----------|-------|------|
| 1 | Round-based **replanning loop**: structured `PlanDecision` (analysis-of-last-round + parallel named dispatches + `is_done`) emitted by a pure planner; a bus owns execution | Skywork v1.0 (`src/agent/planning_agent.py`) | 5 | 4 | **20** | ALLOWED |
| 2 | **Background memory distillation**: gated LLM pass converting raw trajectory events into deduplicated, importance-ranked summaries+insights, persisted and auto-loaded next session | Skywork v2 `src/memory/general_memory_system.py` | 5 | 4 | **20** | ALLOWED (w/ C2 guard) |
| 3 | **Code-as-action** for the compute stage: model writes Python that composes multiple tool calls in one step | smolagents CodeAgent (GAIA-ablated) | 4 | 3 | **12** | ALLOWED |
| 4 | **Context quarantine/compression**: each sub-agent compresses its findings to a bounded digest before returning to the supervisor | LangChain open_deep_research | 4 | 4 | **16** | ALLOWED |
| 5 | **Perspective-guided question asking**: discover expert perspectives from similar articles, interrogate the topic per-perspective | Stanford STORM | 3 | 4 | **12** | ALLOWED |
| 6 | **Multi-signal memory retrieval**: fuse semantic + BM25 + entity-linked matches; time-aware ranking | mem0 v3 algorithm | 4 | 3 | **12** | ALLOWED (C2) |
| 7 | **Task ledger** (facts / plan / progress re-derived every step, drives error recovery) | Magentic-One on AutoGen | 3 | 3 | **9** | ALLOWED |
| 8 | **Recursive breadth/depth research tree** with accumulated learnings carried down-level | GPT-Researcher Deep Research / dzhng deep-research | 3 | 3 | **9** | ALLOWED |
| 9 | **plan.md as human-readable run artifact** (YAML todos + mermaid flow + per-round log) | Skywork v1 `PlanFile` | 2 | 4 | **8** | ALLOWED |
| — | SEPL propose/assess/**commit** self-evolution protocol | Skywork v2 | — | — | — | **FORBIDDEN** near CONFIDENCE/GATE; conditional-ALLOWED for decomposer prompts only under C1 + human review |
| — | GRPO / Reinforce++ / TextGrad optimizers | Skywork v2 `src/optimizer/` | — | — | — | **FORBIDDEN** for scoring path (see §6) |

---

## 2. Per-framework assessments

### 2.1 SkyworkAI/DeepResearchAgent — the anchor case, and a trap

- **License:** MIT (read from `LICENSE`; copyright line reads "Copyright (c) 2025 AgentOrchestra").
  Usable with attribution. 3,525★.
- **Benchmark evidence:** YES, and it is the strongest open number set on GAIA. `v1.0.0` README
  (fetched): GAIA **validation 82.4 avg** (L1 92.5 / L2 83.7 / L3 57.7), **test 83.39 avg**
  (93.55 / 83.02 / 65.31). Paper: *AgentOrchestra: A Hierarchical Multi-Agent Framework for
  General-Purpose Task Solving*, arXiv:2506.12508.
- **CRITICAL FINDING — the repo on `main` is no longer that system.** `main` was rewritten into a
  "self-evolution protocol and runtime" (RSPL resource substrate + SEPL evolution layer) with
  trading agents, debate configs, and a vendored TextGrad. The GAIA system survives only on tag
  **v1.0.0** (and the arXiv paper). Anyone cloning `main` today and expecting the 82.42 agent gets a
  different machine. Pin `v1.0.0` in any future reference.

#### 2.1.1 What the v1 planner actually does (Gap 1 answer)

Read from `src/agent/planning_agent.py` @ v1-era design (structure confirmed on both tags):

1. **The planner is pure.** It imports neither the bus nor any tool. One LLM call per round returns
   a pydantic `PlanDecision`: `{thinking, analysis, plan_update, dispatches[], is_done,
   final_result}`. All dispatching, result collection and loop control live in a separate **bus**.
   Separation of "decide" from "execute" is total.
2. **It re-plans every round, conditioned on results.** The bus feeds back an execution history
   (goal, dispatched agents, per-agent OK/FAIL + truncated results) and the decision schema forces
   an `analysis` field evaluating the previous round before new dispatches are emitted. Our
   `_decompose` (`engine.py:264`) fires once and the pipeline then runs a fixed stage order; there
   is no mechanism to look at leaf outputs and change the remaining plan.
3. **Dispatches name capabilities, not stages.** Each `SubTaskDispatch{agent_name, task, files}` is
   routed through an **agent contract** (a markdown description of available agents injected into the
   planner prompt). Specialists in the shipped config: Deep Analyzer, Deep Researcher, Browser Use,
   MCP Manager, General Tool Caller. Multiple dispatches in one round execute **concurrently**
   (UNICAST/BROADCAST modes). Our equivalent surface is a hard-coded five-stage enum.
4. **Explicit termination semantics.** `is_done` + mandatory `final_result` make "research complete"
   a first-class planner output instead of falling off the end of a stage list. This maps cleanly
   onto our seal/refuse decision.
5. **The plan is an artifact.** `PlanFile` renders `workdir/<session>.plan.md`: YAML frontmatter
   todos with live statuses, mermaid execution graph, per-round log with results and analysis.
   Our checkpoints (`tools/pipeline/checkpoint.py`) are machine-resumable but not human-reviewable
   plans; a reviewer cannot read a run's intent mid-flight.

**Concrete delta vs ours:** ours decompose-once → fixed DAG; theirs decide-execute-analyze loop with
capability routing, intra-round parallelism, forced retrospective analysis, explicit done-state, and
a readable plan artifact. None of that touches scoring.

#### 2.1.2 What "Remember" actually is (Gap 2 answer)

From `src/memory/general_memory_system.py` on `main` (v2 runtime — the Act/Observe/Optimize/Remember
loop is documented verbatim in the current README):

- Every tool step / task-end becomes a `ChatEvent` appended to a session-scoped buffer (fast path).
- A **background task** asks an LLM gate: "should this slice be processed?" with explicit criteria
  (<5 events → no; repetitive → no; significant new insights → yes; >10 events → yes). Cheap runs
  never pay the distillation tax.
- If yes, a second structured-output call extracts **two kinds of atoms**:
  `Summary{id, importance∈{high,med,low}, content}` and `Insight{id, importance, content,
  source_event_id, tags[]}` — with an explicit instruction to *not* repeat anything already stored
  (dedup is prompt-enforced against the current memory text), then sort-by-importance and hard-cap
  counts.
- Persistence: whole thing serialized to `memory_system.json`; `start_session` **auto-loads** it.
  That load-back at session start is the entire cross-run mechanism — there is no vector search in
  this module (embeddings exist elsewhere in the repo). It is write-side curation + full-context
  reload, not retrieval.

So: their Remember = **asynchronous, gated, deduplicating distillation of trajectories into ranked
summaries/insights that survive across sessions**. Compare ours: `memory.py` has the storage half
(catalogue, domains, promotion_history, floors) but — per the reviewer finding — no automatic
post-seal distiller feeding it and no retrieval hook inside `decompose_messages`. The inertness is
on both edges of the store, not in the store.

- **Port vs rebuild:** port. The distiller is ~200 lines of logic independent of their runtime; the
  write target is our existing `catalogue`/`world_{domain}` tables. Their JSON-file reload becomes
  redundant — we already have SQLite + vector collections.
- **What it does that we can't today:** mid-run replanning (§2.1.1), concurrent specialist fan-out,
  cross-session insight carry-over, plan artifacts. And, on `main`, prompt-evolution machinery we
  will deliberately NOT copy into the scoring path (see §6).

### 2.2 huggingface/smolagents (+ `examples/open_deep_research`)

- **License:** Apache-2.0 (verified). 29k★.
- **Benchmark evidence:** YES, with an ablation that matters. HF blog (Feb 2025): their
  open-deep-research hit **GAIA validation 55.15** (o1-driven), beating the previous open SoTA
  (~46, Magentic-One); switching the identical setup from CodeAgents to JSON tool-calling dropped it
  to **~33**. That 22-point delta is the cleanest public evidence that *action representation*
  (model-written code vs per-step JSON) is a first-order architectural variable.
- **One structural idea worth taking:** **code-as-action** — the agent expresses a whole plan segment
  as executable Python (loops, conditionals, parallel calls, variable reuse across steps), executed
  in a sandboxed interpreter; ~30% fewer steps than JSON actions (Wang et al. 2024, confirmed by
  their ablation).
- **Port vs rebuild:** port narrowly. We already have `tools/sandbox.run_python` behind the compute
  stage. Upgrade: let the compute/answer stage emit a *program* referencing registered source
  wrappers instead of a single expression — keeping the existing sandbox restrictions and
  ProvenanceLedger recording. Do not adopt the smolagents runtime.
- **Beyond Callisto today:** one generated program replaces N round-trips for multi-source
  aggregation math; variable persistence across steps inside a leaf.
- **Side:** ALLOWED (tool/compute representation). The sandbox boundary stays; nothing here touches
  gates.

### 2.3 langchain-ai/open_deep_research

- **License:** MIT (verified). 12.7k★.
- **Benchmark evidence:** YES, current and reproducible: **Deep Research Bench** RACE score
  **0.4344** (#6 at submission, Aug 2025), **0.4943** with a GPT-5 research model; full LangSmith
  experiment links published; $46–187/run costs disclosed. DRB (100 PhD-level tasks, RACE + FACT
  metrics) has since migrated its judge to GPT-5.5 (May 2026) and spawned DRB II
  (arXiv:2601.08536) — this is where live DRA rankings happen now, because GAIA's public
  leaderboard is a Gradio app that could not be scraped from this environment (fetch returned shell
  HTML only; treat any "current GAIA standing" claim you see without a dated screenshot with
  suspicion, including ones from me).
- **One structural idea worth taking:** **context quarantine with compression.** Supervisor spawns
  researchers; each researcher works over raw search dumps but returns a compressed digest; the
  supervisor never sees raw pages. Plus role-model separation: a cheap mini-model summarizes search
  results, a strong model reasons, a third writes the final report.
- **Port vs rebuild:** port the pattern, not the LangGraph app. In our terms: `answer_leaf` already
  produces a leaf conclusion; add an explicit compression step so the seal-stage synthesis and any
  future replanner consume digests + citations, never raw bodies. Model-per-role maps directly onto
  our PipelineModel injection.
- **Beyond Callisto today:** cost-controlled scaling to long horizons (their token totals are
  published per config); MCP-tool flexibility.
- **Side:** ALLOWED.

### 2.4 assafelovic/gpt-researcher

- **License:** Apache-2.0 (verified; README disclaimer states Apache 2). 29.1k★.
- **Benchmark evidence:** NONE public. Cost/speed claims only (~$0.4, ~5 min per deep research).
  Huge community, zero published benchmark rows. By the "actually running and benchmarked" standard
  it fails the test — while still containing two portable mechanisms.
- **One structural idea worth taking:** **recursive depth/breadth research tree with carried
  learnings** (their Deep Research mode; same idea independently in dzhng/deep-research, MIT,
  19.6k★): level-N subtopic reports generate `learnings` that seed level-N+1 query generation —
  i.e., decomposition is *iterative and informed by prior levels*, not one-shot.
- Also notable: their bias-mitigation heuristic — scrape ≥20 sources per question, prefer
  information frequency across sources. Crude, but it is exactly a corroboration-count prior, and we
  have a principled version already (SourceClass ranks); theirs is a reminder that count-of-
  independent-corroboration belongs in *evidence selection*, upstream of our clamps.
- **Port vs rebuild:** port the tree shape into the planner loop of §4; trivially expressible over
  ResearchProgram (sub-programs with inherited evidence requirements).
- **Side:** ALLOWED.

### 2.5 microsoft/autogen ⚠️ and ag2ai/ag2

- **License — FLAG:** `microsoft/autogen`'s LICENSE file is **CC-BY-4.0** (I fetched and decoded the
  actual file; it is the full Creative Commons Attribution 4.0 text). CC-BY-4.0 is a content
  license, not an OSI-approved software license — **do NOT vendor anything from that repo into this
  codebase**; treat even "reference reading" of its code with care. The maintained fork
  `ag2ai/ag2` ("AG2", formerly AutoGen) is **Apache-2.0** (verified) if we ever need a look.
  microsoft/autogen: 60.6k★, last push Apr 2026 (effectively winding down); AG2: 4.9k★, active.
- **Benchmark evidence:** the framework itself ships none; the meaningful number comes from
  **Magentic-One** (MS Research, built on AutoGen): GAIA validation ≈**46** per HF's Feb-2025 blog
  (their own Nov-2024 report claims higher; UNVERIFIED here). Either way: below smolagents'
  55.15 and far below AgentOrchestra's 82.4.
- **One structural idea worth taking:** Magentic-One's **task ledger** — a running ledger with
  FACTS (established, immutable), PLAN, and PROGRESS (is the loop making progress? is the loop
  stuck?) re-evaluated by the orchestrator at every step; stuck-detection triggers plan revision or
  agent swap. It is error-recovery as data structure.
- **Port vs rebuild:** concept-port only. Our analogue is thin: checkpoint traces record what
  happened, nothing decides whether we're *stuck*. A progress-ledger check between leaf waves fits
  the §4 planner loop naturally.
- **Side:** ALLOWED (orchestration). Adopting either framework wholesale: NO — negative leverage;
  we'd inherit their abstraction layers to get ideas we can lift in days.

### 2.6 crewAIInc/crewAI

- **License:** MIT (verified). 57.5k★.
- **Benchmark evidence:** NONE on any public benchmark. Marketing benchmarks only. This is the
  canonical "prompt-chaining with a README": a pleasant DSL over role/persona/task assignment whose
  hierarchical mode is a manager-agent delegation loop — the same control flow LangGraph or plain
  asyncio gives you, with worse observability.
- **Structural idea worth taking:** honestly none that isn't already covered by stronger entries
  (its hierarchical process ≈ §2.1's planner/bus, minus the structured contract and minus evidence).
- **Port vs rebuild:** nothing to port. Skip.

### 2.7 langchain-ai/langgraph

- **License:** MIT (verified). 40.3k★.
- **Benchmark evidence:** none as a framework (its flagship app is assessed in §2.3). It is plumbing,
  and good plumbing: durable graph-state checkpointing (time-travel resume), and a **cross-thread
  Store** with namespaced, semantically searchable long-term memory distinct from thread-scoped
  checkpoints.
- **One structural idea worth taking:** the **checkpoint/store split** — run-scoped resumable state
  vs global retrievable memory as two different primitives with different lifetimes. We already have
  the first (`checkpoint.py`, keyed by run+stage+hash — see tests/test_build_w3_checkpoint.py); we
  nominally have the second (memory.py) without the retrieval edge. The lesson is architectural:
  keep them separate, wire different consumers.
- **Port vs rebuild:** adopt the split as a design rule, not the library.
- **Side:** ALLOWED.

### 2.8 stanford-oval/storm

- **License:** MIT (verified). 31.1k★. Last push Sep 2025 — research-complete, mostly dormant.
- **Benchmark evidence:** partial. Peer-reviewed (NAACL 2024 / EMNLP 2024 Co-STORM) but evaluated on
  FreshWiki article quality rubrics and FreshQA — not GAIA/DRB-class agent benchmarks. Numbers are
  modest and apples-to-oranges; the value is the mechanism, not the scoreboard.
- **One structural idea worth taking:** **perspective-guided question asking.** Before asking any
  questions, survey *similar existing articles* to discover the perspectives real experts take, then
  simulate a writer-vs-expert conversation per perspective to generate grounded follow-up questions.
  Directly applicable to our decomposer: instead of one monolithic "split this question" call, first
  retrieve adjacent prior work (our own world memories + corpus) to enumerate perspectives, then
  decompose per-perspective. Attacks decomposer myopia (coverage gaps) cheaply.
- **Port vs rebuild:** port as a decompose_messages pre-phase. Built on dspy internally; we need
  none of it.
- **Side:** ALLOWED (decomposition quality).

### 2.9 camel-ai/owl

- **License:** Apache-2.0 — note the LICENSE lives at `licenses/LICENSE`, so naive tooling (incl.
  the GitHub API) reports "none"; verified via README badge + license path. 20.1k★.
- **Benchmark evidence:** YES historically: GAIA **58.18** (Mar 2025), then **69.09** (Apr 2025),
  #1 among open frameworks at the time; NeurIPS 2025 paper (arXiv:2505.23885). Repro requires their
  pinned `gaia69` branch with a customized CAMEL vendored in-tree — a reproducibility smell worth
  noting.
- **One structural idea worth taking:** the **hint channel** — a subordinate agent can ask the
  supervising agent for guidance mid-task (role-playing society with an asymmetric help path).
  Modest idea; the bigger pivot is that OWL's current energy is *training* ("Optimized Workforce
  Learning", released datasets + checkpoints) — i.e., they concluded prompting-only orchestration
  plateaued. Relevant strategic signal for us; irrelevant mechanically (we are not training).
- **Port vs rebuild:** skip the framework (deep CAMEL dependency); the hint channel could be a
  planner-round escape hatch later.
- **Side:** ALLOWED (mechanism); their training pipeline is out of scope for us entirely.

### 2.10 Cross-run memory specialists (Gap 2 support cast)

**mem0ai/mem0** — Apache-2.0 (verified). 63.9k★.
- **Benchmarks:** YES, with a caveat they disclose themselves: LoCoMo **92.5**, LongMemEval **94.4**
  (Apr 2026 algorithm; scores reflect their managed platform; OSS SDK "directionally similar"; eval
  harness open-sourced at mem0ai/memory-benchmarks).
- **What Remember is here:** v3 switched to **ADD-only single-pass extraction** (no UPDATE/DELETE —
  memories accumulate, conflicts resolved at read time), **entity extraction/linking across
  memories**, **multi-signal retrieval fusion** (semantic + BM25 + entity match), and **temporal
  reasoning** at rank time (which dated instance answers "current state" vs "past event"). The
  removal of destructive update paths is philosophically identical to our min()-only asymmetry:
  append-only writes, conservative resolution.
- **Port vs rebuild:** port the read-side pattern onto `world_{domain}` collections (we already have
  embeddings + sqlite FTS candidates); adopt ADD-only discipline in the distiller of §2.1.2.

**getzep/graphiti** — Apache-2.0 (verified). 30.2k★. Temporal knowledge graph with bi-temporal edge
invalidation (facts are valid-between-T and known-after-T). Right primitive if/when our memory needs
contradiction handling over time; too much machinery for the first iteration. Benchmark numbers
reported upstream (LongMemEval/DMR) — UNVERIFIED here.

**letta-ai/letta** — Apache-2.0 (verified). 24.4k★. MemGPT lineage: tiered core/archival/recall
memory the agent edits via tools. Note for our purposes: "self-editing memory" is fine under C2 as
long as edits land in retrieval space, never in scoring inputs. Not needed for v1 of our fix.

---

## 3. Gap 1 synthesis — hierarchical planning, concretely

Target: replace "decompose as a single pipeline step" with a planner loop **around** the leaf
machinery, leaving confidence/adversary/seal byte-identical.

Proposed shape (mapped onto our files):

```
engine.run()
 └─ PlannerLoop (new, mirrors Skywork bus/planner split)
     ├─ plan_decision = LLM(decompose_messages + agent_contract + execution_history)
     │    → {analysis_of_last_wave, plan_update, dispatches[leaf_specs], is_done}
     ├─ wave: dispatch leaf_specs concurrently through EXISTING
     │        select/fetch/compute/answer stages (unchanged, still checkpointed)
     ├─ append wave results (digests, per §2.3 compression) to execution_history
     ├─ optional progress-ledger check (§2.5): stuck? revise plan
     └─ is_done → proceed to confidence clamp → adversary → seal (UNCHANGED)
```

What this buys us that Skywork's proves out at GAIA-82 scale: informed replanning after seeing
results, capability-named dispatch (our SourceRegistry + tool set become the contract), intra-wave
parallelism (engine already async), explicit done/refuse semantics feeding the seal, and a rendered
plan artifact per run for post-hoc audit (fits our provenance culture; complements, not replaces,
AGP sealing).

Invariant compliance: the planner chooses **questions and routes**, never scores. Confidence clamps,
EvidenceRequirements and inheritance rules sit downstream and stay min()-only. The planner can
*add* evidence requirements; it can never remove one without that edit being visible in the sealed
trace (and per C1, any future prompt-tuning of this loop optimizes on sealed-outcome ground truth
only).

## 4. Gap 2 synthesis — making memory load-bearing

Three moves, all on the ALLOWED side, all landing on infrastructure we already own:

1. **Distiller (Skywork pattern):** after `seal()`, enqueue a background job over the run trace →
   gated LLM pass (<N events skip; repetitive skip) → emit `Summary`/`Insight` atoms with
   importance + tags + `source_event_id` (= our SessionStep/provenance ids) → write into
   `catalogue` with `source_class=INFERRED` cap and existing domain CHECKs. Dedup against current
   world view before insert (mem0's ADD-only discipline: never overwrite, supersede at read time).
2. **Retrieval edge (mem0 pattern):** `decompose_messages` (and SourceRegistry.select) gain a
   pre-phase that queries `world_{domain}` via fused signals (vector + FTS/BM25 + entity tags) +
   recency/temporal weighting, injecting top-k prior conclusions WITH their source_class and
   provenance into the decompose prompt. This is the missing edge that made our modules "partly
   inert": write path existed, read path didn't.
3. **Invariant guard (ours, stricter than anyone above):** memory entries are INFERRED-class by
   construction, participate only in query formation and evidence selection, and are structurally
   incapable of touching thresholds/floors/clamps. Cross-domain reads stay logged exactly as
   memory.py does today.

## 5. Blunt calls

**Actually running and benchmarked (not just starred):**
- **AgentOrchestra/Skywork v1** — GAIA 82.4 val / 83.39 test, repro scripts in-repo. Real.
- **smolagents open-deep-research** — 55.15 GAIA val with a published ablation (code vs JSON
  actions: 55 vs 33). Real, and the ablation is worth more than the score.
- **LangChain open_deep_research** — public DRB rows with costs and experiment links. Real.
- **OWL** — GAIA 69.09, NeurIPS paper, but repro pinned to an in-tree CAMEL fork. Real number,
  soft repro.
- **STORM** — peer-reviewed twice; wrong benchmarks for our purpose, right mechanisms.
- **mem0** — publishes numbers AND the harness; discloses platform-vs-OSS gap. Respectable.
- **Magentic-One (AutoGen)** — ~46 GAIA val per third-party citation; the ledger idea outranks the
  score.

**Prompt-chaining with a README (bluntly):**
- **CrewAI** — no public benchmark anywhere; a persona DSL. Its star count measures Node-adjacent
  hype, not capability.
- **GPT-Researcher** — genuinely useful engineering, zero benchmark evidence; adopt its research-tree
  shape, ignore its leaderboard silence.
- **dzhng/deep-research** — honest about being minimal; nothing to benchmark; the learnings-carry
  trick is the takeaway.
- **AutoGen/AG2/LangGraph-as-frameworks** — adopting any of them wholesale buys abstractions, not
  results. Steal ledger, checkpoint/store split, done.

**And the cautionary tale in our own direction:** Skywork's `main` is now an elaborate
self-evolution protocol (SEPL commit/rollback over prompts, GRPO/Reinforce++/TextGrad optimizers)
with NO benchmark claim on the README — the GAIA system is frozen on an old tag while the repo's
energy went to machinery that optimizes the optimizer. That is the exact failure mode our invariant
exists to prevent: energy flowing from *verifiable task performance* into *self-modification
capacity*. Take their planner and their distiller; leave the optimizer cathedral.

---

## 6. Side-of-the-line summary (every recommendation)

| Recommendation | Path it touches | Verdict |
|---|---|---|
| Planner loop / replanning / agent contract (§3) | planning, decomposition, routing | ✅ ALLOWED — encouraged |
| Compression before synthesis (§2.3) | context management | ✅ ALLOWED |
| Code-as-action compute (§2.2) | tool execution | ✅ ALLOWED — sandbox + provenance unchanged |
| Perspective-guided decomposition (§2.8) | decomposition | ✅ ALLOWED |
| Distiller + fused retrieval memory (§4) | retrieval & planning inputs | ✅ ALLOWED **only** under guard C2 |
| Progress ledger / stuck detection (§2.5) | orchestration | ✅ ALLOWED |
| Research-tree decomposition (§2.4) | decomposition | ✅ ALLOWED |
| Prompt tuning of decomposer/planner (if ever) | prompts | ⚠️ CONDITIONAL — C1: ground-truth reward only, never gate passage; human-reviewed commits |
| SEPL-style automated propose/assess/**commit** on any component | self-modification | ❌ FORBIDDEN without human gate; never on CONFIDENCE/GATE |
| Anything (optimizer/RL/reflection) whose objective touches confidence scores, tiers, EvidenceRequirement strength, or thresholds | scoring | ❌ **ABSOLUTELY FORBIDDEN** |

## Appendix — verification log (all fetched 2026-08-23)

- Licenses read directly: Skywork MIT (`LICENSE@main`, © AgentOrchestra); smolagents Apache-2.0;
  gpt-researcher Apache-2.0; ag2ai/ag2 Apache-2.0; crewAI MIT; langgraph MIT; storm MIT;
  camel-ai/owl Apache-2.0 (`licenses/LICENSE`); open_deep_research MIT; mem0 Apache-2.0;
  graphiti Apache-2.0; letta Apache-2.0; **microsoft/autogen CC-BY-4.0 (full text fetched — FLAG)**;
  dzhng/deep-research MIT (GitHub API).
- Benchmarks: Skywork v1.0.0 README (GAIA 82.4 val / 83.39 test); HF blog open-deep-research
  (55.15 val; Magentic-One ~46; JSON-action ablation 33; OpenAI DR 67.36 val); OWL README (58.18 →
  69.09, dates in changelog); open_deep_research README (DRB RACE 0.4309–0.4943 w/ configs);
  deep_research_bench README (judge migration to GPT-5.5, May 2026; DRB II, Feb 2026);
  mem0 README (LoCoMo 92.5, LongMemEval 94.4, platform caveat).
- Source read in full: Skywork `src/agent/planning_agent.py`, `src/memory/general_memory_system.py`
  (@main), repo tree @main, README @v1.0.0.
- Could not verify from this environment: current GAIA leaderboard table (Gradio app, JS-gated);
  Magentic-One's own reported GAIA figure; graphiti benchmark numbers. Marked UNVERIFIED inline.
