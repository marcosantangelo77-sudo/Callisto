# Instance 7 findings — deep research capability (DEEP_RESEARCH.md on this branch)

Full deliverable: `DEEP_RESEARCH.md` at repo root on audit/tier7-research.
Tests: tests/test_tier7_deepresearch.py (15 passing) pin every VERIFIED claim.

## [VERIFIED] orchestrator.py — no question decomposition exists anywhere
Blast radius: SILENT (capability gap, not a bug)
Evidence: only decomposition-adjacent step is _step_enumerate_sources (:1009);
task_classifier assigns budget buckets only; no sub_question/plan-tree code in
agp/ or orchestrator. Design (ResearchProgram tree, D0-D2) in DEEP_RESEARCH §1.
Falsifier: an orchestrator path emitting multiple AGP sessions as sub-questions per query.
For: unowned (design work, new code)

## [VERIFIED] orchestrator.py:1261 _execute_tool — no code-execution tool of any kind
Blast radius: SILENT (the owner's core ask — models/spreadsheets/math — has zero surface)
Evidence: dispatch is search/claude_code/odds/ev only; no exec/subprocess/repl anywhere.
Design: sandboxed run_python + sealed artifacts (S0-S3), §3.
Falsifier: name an artifact-returning or code-executing tool in the dispatch.
For: unowned (new code)

## [VERIFIED] tools/backtest.py:3809 — paper-trading resolution producer is status-gated to paper_trading
Blast radius: n/a (the generalization seam)
Evidence: generate_paper_trade_signal returns [] unless status=='paper_trading'.
The H0 proposal generalizes paper_trades behind a claim_type discriminator —
this is the single highest-leverage schema change for domain-generality (§2).
Falsifier: generalized paper-trade path failing existing sports tests after H0.
For: unowned (schema migration, needs characterization tests first)

## [VERIFIED] MERGE STATE — instance 4's keyed HMAC seal is NOT on master
Blast radius: SILENT (seal still forgeable here)
Evidence: this branch's agp/__init__.py:352 computes plain hashlib.sha256;
eb6151b (keyed HMAC) lives only on audit/tier3-epistemics. My first draft of
DEEP_RESEARCH.md claimed it was merged — corrected. Fetch-record provenance
sealing (E2) and artifact sealing (S1) depend on that merge landing.
Falsifier: agp/__init__.py using hmac on this branch.
For: whoever merges tiers (owner)

## Clean-bill items (what already generalises — §5 of DEEP_RESEARCH.md)
- agp/ core has ZERO domain vocabulary (asserted by test) — the moat is portable as-is.
- agp/thresholds ceilings are evidence-authority statements, domain-neutral.
- STAGE_ORDER lifecycle is the right abstraction for any falsifiable claim; only
  the column schema around it is betting-shaped.
- tools/search.py (SearXNG→Brave) is already domain-general — most portable module in tools/.
- Citation check is substring-only (re-verified _response_cites_urls) — E0 fix is
  small and benefits every domain.

## Priority order (full rationale in DEEP_RESEARCH.md)
H0/H1 paper-trade generalization > S0 compute sandbox > E0 citation grounding >
D0 decomposer > E1/E2 fetch+provenance > H3/S3 inheritance rule + model registry.
