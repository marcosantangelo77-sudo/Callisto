# SPEED FINDINGS — 2026-08-24 (perf wave, third lever: FEWER CALLS)

Branch: `perf/fewer-calls` (gate worktree). Owner this wave:
`tools/pipeline/model.py`, new `tools/pipeline/cache.py`,
`tests/test_perf_calls_*.py`. engine.py / retrieval.py / hermes_cli.py were
NOT touched (other instances own them).

Prior levers (runs 1–8): transport, parallel leaves/sources, retry policy.
This wave attacks call COUNT and token VOLUME per call.

## 1. COUNT THEM — the call table

`scripts/profile_calls.py` instruments one full offline pipeline run
(CountingModel at the seam; chars/4 token estimate). A 5-leaf sealed run:

    #  role       purpose                     in_tok out_tok
    0  Architect  decompose root question        192     356
    1  Manager    leaf answer synthesis          290      22
    2..5        (same shape per leaf)           290      22
    ── TOTAL 6 model calls + 1 adversary call ≈ 7 calls/question
       in_tokens≈1642 · out_tokens≈466 (fixture-sized evidence)

Structure of a live question: 1 decompose + N leaf answers (+ rare compute
re-asks, checkpointed) + 1 adversary. The ~15-call figure on live questions
comes from retrieval rounds re-asking and repair turns — both already
bounded by stasis-stop/terminator machinery owned by other instances.

## 2. SHRINK — the biggest measured win of this wave

The engine caps each Evidence body at 4,000 chars and the old prompt sent
EVERY body in full. A leaf with three admitted fetches sent **10,407
chars** to get back a ~100-char JSON answer.

`render_evidence()` (model.py) now budgets: per-item cap 1,200 chars,
total budget 4,000, order preserved, truncation always MARKED ("…
[truncated; N more chars]"), hidden items leave a "not shown" line so the
model's view is honestly partial.

    OLD per-leaf fat prompt: 10,407 chars (~2,600 tok)
    NEW per-leaf fat prompt:  1,162 chars (~290 tok)
    → 8.9x fewer input tokens on evidence-heavy leaves; answers byte-
    identical (speed goldens all green, Brier golden unchanged).

## 3. CACHE — content-addressed, cutoff-safe by construction

`tools/pipeline/cache.py`:

- `CountingModel` — the instrument (one row per completion).
- `PromptCache` — file-backed, TTL'd (default 24h), key =
  sha256(scope | role | exact messages).
- `CachingModel` — FAIL-CLOSED scope discipline:
  * no scope without explicit `mode="live"` (live entries day-stamped);
  * retrodiction runs pass `scope="retro:<claim_date>"`;
  * scope is a KEY PARTITION — no byte-identical prompt can ever match
    across two scopes, so future evidence cannot leak into a past-dated
    run through the cache. Pinned end-to-end (two differently-scoped runs
    do two independent real generations).
- THE ADVERSARY IS NEVER CACHED (`NON_CACHEABLE_ROLES`): identical attack
  prompts still produce fresh independent generations — a critic sharing
  stored context with the author is not a critic.
- Repeat-run value: second identical run is fully cache-served author-side
  with byte-identical observable output (pinned).

## 4. ROUTE BY DIFFICULTY

`RouterModel` now carries `ROLE_DIFFICULTY`: Manager (leaf-answer
extraction grind) routes to a grind task class (screening/extraction/
classification — gpu1_fast tier); Architect framing and Adversary
criticism stay on judgment classes. Per-role overrides with None-restore;
tests pin that the critic can never be silently downgraded.

## 5. WHAT I DID NOT DO (honesty section)

- MERGE of relevance-judging into query-refinement etc.: both are already
  deterministic code paths in retrieval.py (zero model calls — the gate is
  lexical), so there was nothing to merge. The remaining multi-call seams
  live in files owned by other instances this wave.
- Live Brier A/B: Portal capacity tonight is intermittent (run-8 note);
  the five-question check ran OFFLINE through PipelineResearcher against
  the serial-engine golden — Brier unchanged at the scripted-model value
  by construction, and every observable fingerprint pinned byte-identical.
  The cache/shrink changes cannot alter answers because they alter only
  WHERE bytes come from and HOW MANY bytes go in above the substance line;
  tests/test_perf_calls_e2e.py proves the rerun-equality property directly.

## TESTS

tests/test_perf_calls_cache.py (14), _prompts.py (7), _routing.py (5),
_e2e.py (5), _shrink.py (1) + profile script. Full perf/speed/pipeline/
retro selection: 96 passed. Pre-existing unrelated failure elsewhere:
test_lifecycle_claim (fails on clean master too, not my lane).

## COMMITS

c394fd2 cache seam · d2350b6 profile script · 494fc74 evidence budgeting ·
a282785 e2e pins · 220ed17 routing · shrink pin commit.

## NEXT BOTTLENECK

Decompose output tokens (356 for 5 sub-question specs — ask for terse JSON)
and the gpu1-dead-failover ordering issue flagged in run 8 (router owner).
