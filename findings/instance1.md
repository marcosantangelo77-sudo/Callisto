# Instance 1 findings — the unattended loop (tools/autonomous.py, tools/self_repair.py, orchestrator.py)

Session opened 2026-08-22 on branch audit/tier1-loop.
Method: START_HERE brief; AUDIT_MANDATE §2 protocol; ROADMAP treated as unverified prior claims.
Peer status at open: instance 3 (gate) substantially done and read; instances 2/4 not started.

---

## WORK UNIT 1 — self_repair.py gate-lowering paths, re-verified against current code

## [VERIFIED] self_repair.py:991 — ROADMAP §3.1 is WRONG that two of three lowering paths are no-ops: `_fix_finding_edge_ceiling` writes the OPERATIVE column
Blast radius: SILENT (this one actually moves the gate)
Evidence: ROADMAP claims "`_fix_thresholds` writes a JSON key nothing reads" and groups
the edge-ceiling path with it as cosmetic. Re-derived: backtest.py reads
`h["edge_threshold"]` — the COLUMN — at backtest.py:196 and :3819, and gates every
signal on `edge >= edge_threshold` at :2520/:2708/:2866. self_repair.py:991 executes
`UPDATE hypotheses SET edge_threshold = 0.015 ...` on every draft/backtesting row above
2%. That is an operative gate change made by a maintenance routine, stamped
confidence-0.8 into hermes learnings (:1021). Only `_fix_thresholds` (:556 writes the
model_config JSON key, which backtest never reads) and
`_fix_finding_promotion_thresholds` (:953 writes `minimum_events_for_promotion`, which
is read NOWHERE in the repo — verified by repo-wide grep) are no-ops.
Falsifier: find a code path reading model_config["edge_threshold"] or
minimum_events_for_promotion; or show backtest signal gating reads something else.
For: me (enforcement design, WORK UNIT 3)

## [VERIFIED] Repo-wide grep confirms `minimum_events` and `minimum_events_for_promotion` are written by self-repair and read by nothing
Blast radius: SILENT (false success telemetry)
Evidence: grep across the whole worktree: only hits are self_repair.py:898-968 and the
ROADMAP itself. Both `_fix_finding_low_sample` and `_fix_finding_promotion_thresholds`
report `"fixed": True` and record confidence-0.8/learnings for keys no consumer exists
for. Q5 finding: unreachable-by-consumption code manufacturing false confidence.
Falsifier: any reader of those keys appearing in a future commit.
For: me

## [VERIFIED] self_repair.py:318-347 + :570-591 — `_fix_premature_rejection` un-rejects hypotheses, weakening a *decision* gate, not just a numeric knob
Blast radius: LOUD-ish (moves rows rejected→draft wholesale)
Evidence: detector flags ALL rejected hypotheses with zero backtest_events whose sport
appears in historical_odds_cache (no check of WHY rejected — could be duplicate-filter,
thesis-quality, or dedup rejection); fixer bulk-updates status='rejected' → 'draft'
without recording which mechanism rejected them or honoring retry budgets. Combined
with the >95% rejection-rate trigger (_det_rejection :288-303 classifies near-total
rejection as repairable), this is a standing pressure to recycle everything through
draft until something passes. ROADMAP §3.1 noted the treadmill; the requeue path is the
concrete ratchet-release.
Falsifier: show rejections-with-0-events are always data-availability failures (e.g.
rejection reason logged and checked); none is.
For: me

## [VERIFIED] self_repair.py:676-707 — keyword classifier routes "zero promotion" findings to threshold-lowering
Blast radius: ARMING (depends on Claude deep-work output text)
Evidence: `_FINDING_PATTERNS` maps ["zero promotion","no promotions","promotion
threshold",...] → strategy `promotion_thresholds_strict`, handled by
`_fix_finding_promotion_thresholds`; ["edge ceiling","max edge","threshold too high"]
→ `_fix_finding_edge_ceiling` (operative, see above). Any free-text Claude finding
containing these phrases mechanically triggers gate-weakening. Given instance 3's
verified result that zero promotions is the DESIGNED outcome of the Šidák gate, this
classifier guarantees the loop responds to correct gate behavior by attacking the gate.
Falsifier: demonstrate a path where "zero promotion" text does not reach the lowering
handler (e.g. severity filter) — there is none; all severities route identically (:716-733).
For: me

## [VERIFIED] self_repair.py:443-462 — `_try_betmgm_subdomains` monkeypatches a module global and can leak the patched value
Blast radius: SILENT
Evidence: sets `mod._BASE_URL` then restores in a loop; if an await inside the try
raises between set and restore… actually restore runs before each next iteration and
after loop exit via `mod._BASE_URL = orig` placement — but on exception INSIDE
`scrape_betmgm_odds` the restore at :459 still runs (inside the for). Real leak: if
`asyncio.wait_for` raises TimeoutError the except catches it and restore already ran…
re-checked: restore is the last statement in the try, so a timeout skips it and the
except at :457 swallows without restoring. **The patched subdomain persists** until the
next successful call. Also betmgm is commented out of SCRAPERS (:28) so this handler is
currently unreachable (Q5) — dead code with a latent state-corruption bug.
Falsifier: trace showing wait_for timeout reaches line 459.
For: me

---
## WORK UNIT 2 — orchestrator.py confidence pipeline (read 757-1896 + helpers)

## [VERIFIED] orchestrator.py:731-742 — ROADMAP C1's `"http://" in text` citation check is still the live gate for SECONDARY tier
Blast radius: HIGH (integrity — self-reported grounding)
Evidence: `_response_cites_urls` returns True iff "http://" or "https://" appears
anywhere in the response text (:742). `_step_escalate_to_claude` uses it at :1762 to
choose SECONDARY (0.75 ceiling) vs INFERRED (0.55) for Claude's synthesis, and the
same flag sets `summary.confidence_score` (:1783-1786). Printing ANY URL — including
one fabricated inside the conclusion text itself — upgrades the session's own output.
The docstring even claims this measures being "grounded" / "web-corroborated"; it
measures string content only. No fetch, no verification the URL resolves or supports
the claim. Q1/Q2 disagreement confirmed against current code.
Falsifier: show a code path that validates cited URLs (fetch/DNS/match-to-evidence)
before :1763. There is none between parse and Evidence construction.
For: Instance 4 (epistemics) — I do not own the fix; noting it crosses my file.

## [VERIFIED] orchestrator.py:1630-1632 — non-JSON Claude response mints confidence 0.70 from thin air
Blast radius: SILENT
Evidence: when Claude responds but not in JSON, code takes raw text and hardcodes
confidence=0.70, which _clamp_confidence may keep if any SECONDARY-ish evidence
exists. ROADMAP §5 predicted this shape ("non-JSON fallbacks mint 0.70"); verified it
is still present. The value has no evidentiary derivation.
Falsifier: send a non-JSON response with empty evidence; observe 0.70 clamped only by
source-class ceiling, not reduced toward DB_CONFIDENCE_FLOOR.
For: me (could tighten), pending characterization test

## [VERIFIED] orchestrator.py:1849-1861 — Manager can adjust confidence DOWN but its objections have no veto
Blast radius: SILENT (governance theater, same class as C4)
Evidence: `_step_manager_review` applies adjusted_confidence only if lower (:1853),
records objections (:1857-1861), and proceeds to seal regardless of objections. An
"approved": false response changes nothing in the control flow — the session still
seals and stores. The Manager reviews nothing; it decorates.
Falsifier: a parsed {"approved": false} with objections that blocks sealing — no such
branch exists.
For: Instance 4 (protocol design); noted here because the code lives in my file

## [VERIFIED] orchestrator.py:910-939 — seal-refusal path correctly refuses to store; behaves as designed
Blast radius: n/a — CLEAN FINDING
Evidence: AGPSealRefused → returns stored=False/sealed=False with reason; no SPECULATIVE
row written (:912-926). Session registry cleaned in finally (:947-952). This is correct,
well-built code and should not change.
Falsifier: a session dict with sealed=true and error="seal_refused".
For: unowned (recorded as gold per mandate Q8 permission)

---
## WORK UNIT 3 — autonomous.py startup sequence: the biggest gate ratchet in the tier (FOUND + FIXED)

## [VERIFIED] autonomous.py:1609-1709 — `_migrate_edge_thresholds` lowered EVERY hypothesis's operative gate to 0.3% on every loop start
Blast radius: SILENT, continuous, and the largest single gate-weakening mechanism in the codebase
Evidence: called from ResearchLoop.start (:1472) — i.e., on every unattended loop
boot. Four passes end at `UPDATE hypotheses SET edge_threshold = 0.003 WHERE
edge_threshold > 0.003` over all draft/backtesting rows. This is the operative
COLUMN that gates every signal in backtest.py. ROADMAP §3.1 documented self-repair's
1.5% writes as the scandal; this routine is strictly worse (0.3%, runs forever,
no marker written, no hermes learning, just a log line). Nobody flagged it because
it lives in the loop's own startup, not in self_repair.
Falsifier: run a loop start with hypotheses above 0.3% and find them unchanged.
FIX APPLIED (my file): gated behind CALLISTO_ALLOW_THRESHOLD_MIGRATION=1; without
the flag it logs what it WOULD have done and changes nothing.
For: me (fixed); workstation should check how many rows are already pinned at 0.003

## [VERIFIED] autonomous.py:1711-1754 — `_retroactive_signal_update` rewrote HISTORICAL EVIDENCE to match the lowered gate
Blast radius: CRITICAL for epistemics (evidence tampering by a maintenance routine)
Evidence: after migration, re-flags backtest_events.signal_generated=1 wherever
`edge >= new threshold`. The stats/p-values/promotion gates then consume these
events. This means past performance records were silently regenerated under the
new 0.3% bar — hypothesis statistics are NOT computed under a consistent standard
over time. Any calibration claim built on backtest_events is contaminated by however
many times this ran. Same fix applied: opt-in gated, default no-op.
Falsifier: a DB where signal_generated flags change without an operator action.
For: me (fixed); contamination query belongs in ROADMAP §3.1 set:
  SELECT COUNT(*) FROM backtest_events WHERE signal_generated=1 AND edge < 0.005;

## [VERIFIED] autonomous.py:1756-1801, :1803-1839 — two startup requeues un-reject hypotheses and lower gates
Blast radius: SILENT
Evidence: `_requeue_threshold_rejections` bulk-moves rejected→backtesting AND sets
edge_threshold=0.015; `_requeue_prop_rejections` moves rejected→draft and sets 0.003.
Both run on every start. Combined with self_repair's `_fix_premature_rejection`,
there were FOUR independent un-reject mechanisms. All now operator-gated.
Falsifier: rejected rows moving to draft/backtesting on loop start without env flag.
For: me (fixed)

## [VERIFIED] The complete gate-weakening surface of Tier 1 (post-fix inventory)
Blast radius: n/a — summary finding
Evidence: exhaustive sweep of my three files found SEVEN distinct automated
gate-weakening mechanisms, of which ROADMAP knew about three:
  1. self_repair._fix_thresholds (JSON key, was no-op) — REFUSED now
  2. self_repair._fix_finding_promotion_thresholds (dead knob) — REFUSED now
  3. self_repair._fix_finding_edge_ceiling (OPERATIVE column write) — REFUSED now
  4. self_repair._fix_premature_rejection (rejected→draft) — OPT-IN GATED
  5. autonomous._phase_interpret_backtests modify-path (operative column,
     unbounded, LLM-controlled) — DIRECTION-GUARD now (may raise, never lower)
  6. autonomous._migrate_edge_thresholds (startup ratchet to 0.3%) — OPT-IN GATED
  7. autonomous._retroactive_signal_update (evidence rewrite) — OPT-IN GATED
Enforcement principle landed structurally: refusers + direction guards +
operator opt-in flags + static regression tests in both test_tier1_loop_* files.
Falsifier: any remaining code path in autonomous.py/self_repair.py/orchestrator.py
that lowers edge_threshold, weakens promotion requirements, or un-rejects without
an explicit operator flag.
For: me

## [VERIFIED] autonomous.py:5727+ vs backtest.py:3809 — ROADMAP §0 loaded-gun claim CONFIRMED unchanged
Blast radius: LOUD but fail-safe-by-accident (do not "fix")
Evidence: `_phase_live_execute` lists hypotheses with status='live' (:5790) and feeds
them to `generate_paper_trade_signal`, which hard-returns [] unless status ==
'paper_trading' (backtest.py:3809). No automated bet can be placed today. Per
mandate §5.2 I have NOT armed this path and no test of mine touches it with live
semantics. Note the phase also has sound layers worth keeping when it is ever
legitimately armed: drawdown kill-switch before execution, portfolio sizing once
per cycle with caps, regime-safe calendar gate.
Falsifier: one submitted order from a 'live'-status hypothesis under current code.
For: Instance 2 (money path owns arming decision)

## [VERIFIED] autonomous.py:2303-2348 (pre-fix) — the deferred work-queue drain bypassed the threshold direction guard — FOUND AND CLOSED
Blast radius: SILENT (would have silently re-opened hole #5)
Evidence: `_process_drained_item` replays deferred `interpret_backtests` actions
when Claude was unavailable earlier. It contained the OLD unguarded modify code
(`UPDATE hypotheses SET edge_threshold = ?` with no clamp, no current-value read,
no direction check) even after the guard landed in `_phase_interpret_backtests`.
This is the structural lesson of the tier: a policy enforced at one call site is
not enforcement; the same JSON contract is consumed in two places and each needs
the guard. Fixed identically; static test now pins BOTH sites.
Falsifier: any third consumer of the Claude "modify" JSON appearing without
MIN_EDGE_THRESHOLD_FLOOR clamping + refusal logging.
For: me

## [VERIFIED] autonomous.py:7475-7663 — `_phase_system_improvement`: "self-improvement" is advisory-only and correctly so
Blast radius: n/a — CLEAN FINDING with one caveat
Evidence: suggestions go to a system_improvements table and NOTHING reads them for
execution (repo-wide grep: only INSERTs and the phase's own SELECT of recent
suggestions). status/implemented_at columns exist but no code path ever sets or
queries them — the table is a suggestion box, not an actuator (Q5: the
"implemented" lifecycle is unreachable). This is the RIGHT safety posture for an
automated self-improvement claim; it just isn't labeled honestly ("This is how the
system learns to improve itself over time" overstates a log).
Caveat worth noting: its prompt explicitly asks Claude to diagnose "why 0% promotion"
and invites "if the bottleneck is evaluation criteria (too strict), say so" — i.e.
the loop systematically solicits gate-blaming diagnoses. Post-fix this can only
produce advice, which is fine; but a future contributor wiring these suggestions
to executors would re-arm the treadmill. The gate-policy refusers are the defense.
Falsifier: any consumer that applies stored suggestions automatically.
For: unowned (design note)

---
