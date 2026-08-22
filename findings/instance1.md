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
