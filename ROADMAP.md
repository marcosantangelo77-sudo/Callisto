# ROADMAP — Callisto Audit, 2026-08-22

Full independent audit: promotion lifecycle, calibration enforcement, AGP
seal, test suite, Hermes submodule, prior-art landscape, local-model stack.
Every finding below was reproduced against the code (VERIFIED) or explicitly
marked INFERRED. The database was not available; every data-dependent question
comes with the exact query to run at the workstation.

---

## 0. READ THIS FIRST — THE LOADED GUN

`tools/autonomous.py:5784` sizes stakes for `status='live'` hypotheses, but
collects signals via `generate_paper_trade_signal`, which hard-returns `[]`
unless status is `paper_trading` (`backtest.py:3809`). That function is the
ONLY producer feeding `submit_order`. **No automated bet can ever be placed
today.** This is fail-safe by accident. The obvious one-line "fix" (accept
`live`) arms the entire sizing/caps/kill-switch stack at once — code that has
never executed outside unit tests. Do not make that one-line fix until the
promotion gate is rebuilt (§NEXT-2) and the bankroll path has run in paper
mode end-to-end at least once.

---

## 1. THE GOLD — hard to replicate, worth building on

1. **Enforced confidence tiers wired down into the schema.** `agp/thresholds.py`
   formats its floors into a SQLite CHECK constraint at import time
   (`memory.py:40-49`), seal verification raises a distinct tamper exception
   surfaced as HTTP 409 (`api.py:1390-1407`). Prior-art research confirmed:
   **no orchestration framework (LangGraph/CrewAI/AutoGen/DSPy) has any
   concept of enforced confidence tiers**, and no packaged library enforces
   confidence-tier-gated actions in a running agent. The enforce→observe→
   recalibrate loop does not exist anywhere else.

2. **The promotion-gate *concept*** (Šidák-corrected p-values + Brier + IC +
   edge-rate, wired through backtest → paper → live with capital gates).
   Nothing on GitHub ships statistical rigor here — closest is Microsoft's
   RD-Agent, which uses heuristic acceptance and no multiple-comparison
   correction. **The concept is gold; the current implementation is
   unreachable by construction (§3).** Gold after repair, not before.

3. **The seal machinery + its tests.** Canonical-JSON SHA-256 sealing,
   verified-on-read, with genuinely thorough tamper/canonicalization tests
   (`tests/test_agp_seal.py`). Replicating that discipline is harder than
   replicating the idea. Two upgrades required before it deserves trust:
   add a keyed HMAC (today it is forgeable by anyone with DB write —
   `verify_seal` recomputes the same public hash), and wrap it in an
   in-toto/DSSE attestation envelope, optionally anchoring chain heads in
   Rekor. Keep your predicate; adopt the standard envelope.

4. **The integrated live stack** — Kelly sizing, drawdown kill-switch, CLV
   tracking, order reconciliation as ONE wired system. Each piece is
   trivial; nobody else has them integrated under agent control.

5. **Your scraped odds archive** (on the workstation). Aggregators' history
   starts ~mid-2022 and quota costs scale fast. If your scrapers captured
   book-level ticks pre-2024, that archive may be irreplaceable training and
   backtest data. Keep it forever, even after the scrapers die.

---

## 2. THE BACK BURNER — stop maintaining; how to get it back

| Item | Action | Why | Get it back |
|---|---|---|---|
| `hermes-function-calling/` (submodule) | **Vendor ~200 lines, delete the rest.** Keep: `schema.py` (23 ln), `validator.py` guts swapped for plain `jsonschema` (the hand-rolled type checker skips falsy args at validator.py:22 and passes bools as ints — strictly weaker than jsonschema), and from `utils.py`: the XML+`ast.literal_eval` extraction ladder (~40 ln). Delete: chat templates, prompter, torch inference loops, demo functions (incl. an `exec()` liability), notebooks, `upstream_review.py`, the `.gitmodules` entry, dead importers at `inference.py:22-37`. | Native tool calling moved into serving layers; nothing imports the submodule (UPSTREAM.md's import claims are false); the one live descendant (`_extract_hermes_tool_calls`) is a weaker rewrite missing the literal_eval rescue small models need. | It's upstream NousResearch code pinned at ea3c4723 — recoverable from git history and the upstream repo forever. |
| 14-source scraper stack | Replace mainstream odds with **The Odds API** (or OpticOdds for props/sharp depth). Keep only scrapers feeding sources APIs structurally lack (exchange ladders, alt lines). Check two things first: freshness (aggregator snapshots vs book ticks — CLV against stale consensus is fake CLV) and whether any edge depends on non-aggregated sources. | Community scraping is decaying; paid APIs absorbed this space. | Git history + the archive (§1.5). |
| Backtest kernels | Adopt vectorbt ONLY if speed ever hurts. | Your gates, not your kernels, are the value. | Git history. |
| `tests/test_full_system_audit.py`, `tests/test_integration_e2e.py` | Rename out of `test_*` (they contain ZERO pytest-collectable tests), then port their 122 `check()` assertions properly — they are currently invisible to CI yet hold the ONLY coverage of Brier/Kelly/EV. | See §4. | Trivial rename back. |
| Orchestration shell | No action now. If maintenance fatigue wins, LangGraph is the only credible landing spot (durable checkpointing, interrupts). CrewAI is persona theater; Swarm is frozen; AutoGen is deprecated twice over. | The shell is thin glue (<20% of LOC); the enforcement semantics would have to live inside someone else's state machine. | n/a |

---

## 3. THE THREE CONFIRMED FINDINGS (Claude's — verified, corrected, extended)

### 3.1 Self-repair lowers standards AND lies about it — CONFIRMED, worse than reported
All three lowering paths exist as described. Corrections: two of the three
are **no-ops** — `_fix_thresholds` writes a JSON key that backtesting never
reads (it reads the `edge_threshold` COLUMN), and `minimum_events_for_promotion`
is read nowhere in the repo. But they still stamp success and record
confidence-0.8 "learnings." Meanwhile `_det_rejection` classifies >95%
rejection as repairable, so the loop iterates forever, and HELD diagnostics
make small-n failures and Šidák-unreachability produce byte-identical log
lines — **finding 2 is permanently masked by finding 1's fake success.**

Contamination trail (run these at the workstation — `memory/callisto.db`,
tables `hypotheses.model_config` TEXT / `hypotheses.edge_threshold` REAL):

```sql
-- The contamination set:
SELECT hypothesis_id, name, status, edge_threshold, model_config FROM hypotheses
WHERE model_config LIKE '%_threshold_lowered_by%'
   OR model_config LIKE '%_promotion_threshold_lowered_by%'
   OR model_config LIKE '%_edge_ceiling_lowered_by%' OR model_config LIKE '%_previous_edge_threshold%';
-- Operative thresholds — did any 1.5% land?
SELECT status, edge_threshold, COUNT(*) FROM hypotheses GROUP BY 1,2;
-- Dead-knob writes (proves cosmetic fixes):
SELECT COUNT(*) FROM hypotheses WHERE model_config LIKE '%minimum_events%';
-- The fake learnings:
SELECT key, value, confidence, learned_at FROM hermes_learnings
WHERE source='self_repair' ORDER BY learned_at DESC LIMIT 100;
-- How close anything got:
SELECT hypothesis_id, stage, p_value, signals_n, brier_score, information_coefficient
FROM hypothesis_stats ORDER BY p_value ASC LIMIT 25;
```

Policy verdict: you are right — a maintenance routine must never weaken a
gate. The code doesn't argue otherwise; it argues nothing at all. Self-repair
should be permitted to touch: prompts-with-diff-review, retry counts,
scheduling, data-source health. Never: thresholds, gates, sizing, or anything
feeding promotion.

### 3.2 Promotion gate unreachable by construction — CONFIRMED with arithmetic
Gate at `hypothesis.py:140-161` (plus three checks Claude missed: Šidák FWER
over a lifetime denominator, snapshot-quality ≥80%, avg-edge ≥0).

- Lifetime Šidák denominator N=3,192 → α = 9.0e-05 (z ≈ 3.78).
- A TRUE 3%-edge hypothesis needs ~3,900 signals to clear the p-gate at
  median; 1% edge needs ~35,000.
- Catch-22: auto-reject fires at p>0.15 ∧ n≥30. A true 3%-edge bettor has
  P(p≤0.15 at n=30) ≈ 0.144 → **~86% killed at n=30, ~100× before reachability.**
- A perfect record can't clear below n≈15 at all.
- Zero promotions in 3,192 trials is the EXPECTED outcome of this design, not
  evidence of rigor. And from outside, "killed by auto-reject" and "held by
  p-gate" are byte-identical log lines.

### 3.3 CLV gate guards real money with the wrong statistic — PARTIAL
Legacy mixed-units bug was half-fixed: canonical `clv_prob_bp` column exists
and new writers populate it. But the live gate reads neither old nor new — it
reads `paper_trades.clv_implied` (a third unit, devigged nowhere) and compares
a **fraction-of-trades** against 0.005: with the 10-trade minimum, ONE
positive-CLV trade = 10% ≥ 0.5% → PASS. Evident intent (avg CLV ≥ half a
point, or a 50% positive rate) is off by ~100× either way. Missing data → rate
0 → permanent FAIL rendered as "CLV rate 0.0% < 0%" (a formatting lie:
`f"{0.005:.0%}"` rounds to zero).

### 3.4 What Claude missed — the highest-value additions

| # | Finding | Money-at-risk |
|---|---|---|
| N1 | Live execution structurally dead; naive fix arms untested sizing stack | Loaded gun (§0) |
| N2 | Ruin-sim strips lookahead rows differently than the stat-gate demands → tail risk understated exactly when authorizing capital | HIGH |
| N3 | Drawdown kill-switch overwrites `promoted_at` → stage clocks silently rebase on restore | MED |
| N4 | CLV backfill is exact-string matching; misses stay NULL → gate vacuous-or-blocking, never measuring intent | MED |
| N5 | `stake_units` shown to the human approver is fraction-of-bankroll labeled as units → risk underread by 40–80× | LOW-MED |
| N6 | Killed sim paths contribute zero-variance days, flattering Sharpe/Sortino | LOW |
| C1 | **Calibration is self-reported end-to-end**: the ceiling KEY (`source_class`) is itself model-reported; the citation check is `"http://" in text` — printing ANY URL, fabricated included, raises the agent's own ceiling 0.55→0.75 | HIGH (integrity) |
| C2 | **VERIFIED tier unreachable**: PRIMARY is never assigned anywhere in AGP; meanwhile the betting path grants PRIMARY→ceiling 1.0 merely because Pinnacle appeared in a book list | HIGH (semantic rot) |
| C3 | **The seal attests bytes, not truth**: no signature/HMAC; INSERT OR REPLACE allows whole-row swap with fresh self-consistent seal; wiki + hermes-memory read sessions via raw SQL with NO seal check, then reinject them as priors — yesterday's possibly-poisoned INFERRED becomes today's 0.75-ceiling input (trust escalator) | CRITICAL-for-claims |
| C4 | **The Sentinel vetoes nothing** — it's a 32-token query classifier that sees only the query string. And `scripts/sentinel.py` protects a file that doesn't exist (`agp.py`; module is `agp/__init__.py`), so its auto-fixer will patch AGP core, contradicting its own docstring | HIGH (governance) |
| C5 | `bias_direction` sign-inverted in calibration stats; IC actually correlates predicted edge with PAYOUT MULTIPLE, not realized edge | MED |
| C6 | Brier gate WAIVED when p-value strong (`hypothesis.py:1354`) — calibration defeatable by profitability | MED |

---

## 4. THE TEST SUITE

951 test functions across ~90 files. Verdict per subsystem:

| Subsystem | Verdict |
|---|---|
| Devig / arb / dutch / boost math | **KEEP** — closed-form, behavior-pinned, tight tolerances |
| CLV / settlement / orders | **KEEP** — real DB paths, hand-computed expectations |
| Platform/infra | **KEEP** |
| Promotion gates / lifecycle | REPAIR — good bones; the Šidák guard is an imposter |
| Edge scanning / confidence | REPAIR — EV math ungated; source-string pins throughout |
| Risk & sizing | REPAIR — caps asserted relatively, absolute values never |
| AGP / LLM / bridge | REPAIR — heavy mocking; prune local_cc_bridge volume |
| The two `test_full_system_audit.py` / `test_integration_e2e.py` scripts | REBUILD — zero collectable tests, 122 assertions invisible to CI |

The killer answers: edge percent/decimal flip → **NOT CAUGHT**. Brier on wrong
outcome → **NOT CAUGHT**. Silent threshold change → effectively NOT CAUGHT
(the Šidák test computes the formula inside itself and asserts on its own
arithmetic; production is never imported). CLV units → genuinely caught —
best-guarded item in the suite.

**Characterization baselines (required BEFORE any numeric refactor):**
exists = devig family, odds conversion, arb/dutch math, binomial/t/z (bounded).
MISSING = `evaluate_edge`/`ev_binary`/`ev_with_push`, all Kelly variants,
`brier_score`, `information_coefficient`, the actual Šidák gate path,
absolute bankroll-sim outputs. Write, in order: `test_ev_engine.py`
(docstrings already contain verified fixtures), `test_brier_and_ic.py`
(+ outcome-swap symmetry check), `test_kelly_characterization.py` (exact
dollar outputs, not bounds), a real Šidák gate test against a seeded DB,
and a threshold-pin snapshot asserting `PROMOTION_GATES`/`EDGE_THRESHOLDS`
equal explicit literals — so any silent change forces a diff review.

---

## 5. WHAT'S CHANGED SINCE OPUS 4.6 WROTE THIS

The Opus-era fingerprints are consistent and instructive:

1. **Confidence theater.** Self-repair stamps destructive edits as
   0.8-confidence learnings; the enhancement pass defaults missing
   confidence to 0.85; non-JSON fallbacks mint 0.70 out of thin air. The
   system was built to *emit* confidence fluently and to *check* it rarely.
2. **Plausible-but-false documentation.** UPSTREAM.md claims imports that
   don't exist; `thresholds.py` claims single-source-of-truth while
   `edge_confidence.py:26` re-hardcodes the ceilings ("must match
   orchestrator.py"); `bankroll_sim.py:30` claims snapshot metadata
   "doesn't exist" while the gate queries it. The docs were written
   confidently and never re-verified against the code.
3. **Fixes recorded rather than made.** Two of three threshold fixers write
   keys nothing reads, then report `"fixed": True`. The shape of the bug is
   "satisfice the log, not the system."
4. **Tests as demonstration, not guardrails.** Source-string greps,
   self-computing formula tests, stub-asserts-stub — tests that show the
   code doing something rather than pin what it must do.

What I'd write differently now: enforcement in code with external inputs
(source_class derived from fetch provenance, not model claims); keyed seals
over standard envelopes; numeric changes gated behind characterization tests
written FIRST; and a rule the whole codebase violates — **no component may
grade its own homework** (self-repair reporting success, self-reported
source_class setting ceilings, the Šidák test testing itself).

---

## 6. NEXT — ordered

**At the workstation, with the database:**
1. Run §3.1's five queries. Decide the contamination question once and for
   all: did any operative `edge_threshold` actually land at 1.5%, and did any
   stamped row reach paper/live? If yes: quarantine those hypotheses (their
   stats were produced under lowered bars).
2. Before ANY other change: write the five characterization test files
   (§4). They are the seatbelt for everything after.
3. Kill the treadmill: `cmd_generate`-equivalent skip for rows already
   holding generated docs; regate excludes retry-budget-spent and recently-
   paused rows. Stop paying daily for stuck jobs (verified re-billing loop).

**Then, in order:**
4. Rebuild the gate honestly (this replaces §3.2's Catch-22): decouple
   auto-reject from the p-value stage (an n=30 auto-reject kills 86% of
   genuine winners ~100× before reachability); bound the Šidák family
   (rolling window, not lifetime infinity); decide min_events ONCE from a
   power calculation, and let self-repair see none of it.
5. Fix the CLV gate to measure its evident intent: read canonical
   `clv_prob_bp`, gate mean CLV or positive-rate (pick one, document),
   render the threshold honestly in logs, and surface NULL-backfill as
   "insufficient data," not failure.
6. Seal upgrade: HMAC key (secret, env-provided), prev-hash chaining,
   route wiki/hermes reads through verification, replace INSERT OR REPLACE.
   Then in-toto/DSSE envelope if you want external verifiability.
7. Give the Sentinel a real job (veto power over evidence-contradicted
   conclusions, fed evidence not just the query) or rename it honestly.
   Fix `scripts/sentinel.py` PROTECTED_FILES today — it's a five-minute fix
   that closes an auto-patch hole into AGP core.
8. Hermes vendoring per §2; wire the kept extraction ladder into
   `inference._extract_hermes_tool_calls`.
9. Stand up the local tier per `config/providers.yaml` (llama.cpp server +
   Qwen3.8-27B UD-Q3_K_XL @32k; gpt-oss-20b as budget alt), implement the
   ProviderRouter seam it documents, and route screening/extraction/
   classification there. Frontier stays for promotion_judgment and
   adversarial_review only.
10. Only after 4+5+2: consider arming live execution (§0) — in PAPER mode,
    end-to-end, watched, before a dollar moves.

---

## Appendix: prior-art one-liner

Orchestration: keep yours (LangGraph = only credible exit). Calibration
enforcement: **nothing exists** — yours. Statistical promotion gates:
**nothing exists** — yours (after rebuild). Sports pipelines: The Odds API
replaces most scrapers; keep the archive. Seals: yours in substance; adopt
in-toto/DSSE + optional Rekor anchoring as the envelope. There is no
"MERCATOR" standard — nearest real names are Trillian/Rekor/in-toto/C2PA.
