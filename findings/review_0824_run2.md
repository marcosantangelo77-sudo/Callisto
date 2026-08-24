# REVIEW — 2026-08-24, run 2 (audit of the auditors)

Branch `review/ox-alpha-0824b` (= origin/master `96e09c9`). No production code
edited. Reproductions: `tests/test_review_0824_audit.py`, **17 tests, all
failing on this checkout for the documented reason** (the defects are live;
each becomes a fix-pin when its fix lands). Commit 486b2f3.

Scope this run: not new code — the CLAIMS. I read every
`findings/*.md` from the last three days, then read what was actually
committed, branch by branch. Method per PATTERNS.md: claim vs diff first,
then test-reaches-the-branch, half-landed rules, inert modules, open findings.

---

## PART 1 — DOES THE CLAIM MATCH THE DIFF?

### C1. improve_memory_wiki.md claims a landed fix; the fix is NOT on master. (WORST GAP FOUND)
The writeup reports two measured fixes committed as 4352ad1 on
`review/rotating-0823-155500`: (a) clamp_to_ceiling quantises downward,
verified by a 2,000-case property sweep; (b) the admission decision survives
to disk (source_class + provenance_seal persisted; wiki compile gates on it).
**Neither is merged.** On origin/master today:

```
clamp_to_ceiling(0.5497, "INFERRED") == 0.55   # still rounds UP (repro A01)
INSERT INTO hermes_learnings (key, value, learned_at, confidence, source)  # no provenance columns (A02)
SELECT key, value, confidence ... FROM hermes_learnings WHERE confidence >= 0.5  # no gate (A03)
```

Every learning written by master degrades to anonymous INFERRED and the wiki
compile door admits anything at conf ≥ 0.5 — the trust escalator the writeup
declared dead at both ends is fully alive on master. The narrative ran three
worktrees ahead of the tree.

### C2. source_repair_round2.md's "OK=17" tally is real ON ITS BRANCH and absent from master.
`fix/source-repair-round2` genuinely fixed eia (facet routes), census messaging,
federalregister conditions[term] (live-verified by me: bare `conditions=` →
HTTP 500, `conditions[term]=` → HTTP 200), gdelt backoff, probe name aliases.
Its suites pass on the branch (55/55 re-run in a clean worktree). But master's
health.py still probes unregistered names `cftc`/`sec_fts` (KeyError → BROKEN),
master's eia.py still hits the retired `/v2/seriesid/` route, and master's FR
adapter routes `query_term` through bare `conditions`. Live probe of master
right now: **BROKEN=6, DEGRADED=1, OK=12** — bea degraded, bls OK (the WAF
block cleared), census/eia/federalregister/sec_fulltext-broken-by-name/
semantic_scholar/cftc broken. The findings file reads like a done state of
the world; it is the state of one unmerged branch.

### C3. fed_pubmed_adapters.md: "10 tests pass" — true, and unreachable.
Both adapters exist only on `build/fed-pubmed-adapters` /
`fix/source-repair-round2`. Master has no `tools/sources/federalreserve.py`,
no `pubmed.py`, no `_plan_federalreserve`, no health probes. The claim sheet's
own "merged and verified end-to-end" language (in round2's notes) means
merged into *that branch*, not master. Accurate but easy to misread.

### C4. engine_merge.md holds up — checked, not trusted.
I re-ran its load-bearing claims on the merge tree: `record_gate_rejection`
is captured by `_FetchRecorder` and replayed into the real ledger (engine.py);
crossrun traces record for checkpoint-restored leaves as well as fresh ones;
scratch-ledger semantics preserved in `_fetch_leaf_sync`. The golden
regeneration rationale (cmefedfut = 21st adapter moves the diagnostic ceiling)
checks out against the code. This is the best-documented work in the batch.

### C5. estimate_wiring.md: wired, tested, and mutation-tested by me.
`agp/estimate.EstimateCeiling` IS called from engine `_answer_leaf`
(lazy import at engine.py:498). Mutation checks: breaking `with_ceiling`'s
rise-guard fails their tests (good); raising the sealed number +0.004 or the
ceiling +0.1 passes all 8 tests (the equality pin only uses proposals where
those perturbations are invisible — see Part 2); moving the 0.54 requirement
gate to 0.64 correctly fails. Net: real wiring, with one narrow spot (below).

### C6. information_gain.md: eval harness reproduces exactly (7→3 fetches, 0
changed conclusions — I re-ran it). Its 6 tests are honest: mutating
`duplicate_voice = False` fails 3 of them, including the audit-trail test.

### C7. prior_art_survey_salvaged.md: research-only, no diff to audit; its
license/benchmark claims carry live-verified metadata inline. Nothing to
contradict.

### C8. speed_branch_triage.md and STRANDED_WORK.md: ancestry claims verified
by rev-list containment at write time; nothing contradicted them. The triage
correctly refused to book unmeasured runs as perf wins — rare discipline.

---

## PART 2 — DO THE TESTS REACH THE INTERESTING BRANCH?

- **estimate_wiring sealed-number pin has a blind spot**: perturbing the
  sealed confidence by ≤0.004 or the class ceiling by +0.1 passes all 8
  tests, because every pinned proposal value sits where rounding/gating
  absorbs the change. The pin proves equality on six values, not the
  expression. Suggested hardening: property sweep over non-2dp proposals
  asserting `sealed == round(min(est,ceil),2)` exactly (the family-#6 rule:
  sweep > hand-picked).
- **information_gain**: reaches the branch (mutation-verified). Good.
- **cmefedfut branch tests** (11): cover `attach_from_derived` refusals only;
  never the `attach_market_implied` wrapper nor missing claim_date — exactly
  where the guard fails open (Part 3, A10).
- **redteam money-path suite**: 7 failing M-tests are defect-documenting
  repros that were never flipped to fix-pins after partial fixes (d638260
  fixed cap-binding but not duplicate-diversification). Red suites that stay
  red after partial fixes bury real signal (family #7).

## PART 3 — HALF-LANDED FIXES (grep for other copies; found these)

- **floor_conf family**: `clamp_parent_confidence` now floors, but
  `inherited_ceiling` still ends `round(min(score, cap), 4)` — rounds 0.74997
  UP onto the SECONDARY cap boundary, and `memory_epistemics.clamp_to_ceiling`
  (on master) still rounds up outright (C1). Same rule, three sites, two
  still wrong.
- **kelly rounding**: `kelly_fractional` was floored in d638260's pass;
  `kelly_full` still ends `round(fraction, 6)` — at edge=0.0055/+101 it
  returns 0.010946 vs exact 0.005455, nearly 2× the true stake (A05). The
  sweep exists in the repo; nobody pointed it at kelly_full.
- **kelly_portfolio correlation**: docstring says duplicates = one position;
  code pays 1.414× (A06). d638260's own notes fixed the adjacent cap bug and
  left the core — half-landed inside a single commit.
- **forecast-sign rule**: retro.py and calibration/instrument.py both moved
  to declared stance — this one landed twice, consistently. Credit where due.
- **membership rule**: `in_family` normalises; `base.py:independence_family`
  still does raw `spec_name in members` — `semantic_scholar` (underscore)
  falls through to itself while `semanticscholar` maps to scholarly-aggregator
  (verified by direct call). Two spellings, two verdicts, same rule.

## PART 4 — INERT / NOT WIRED

| module | status on master |
|---|---|
| `tools/calibration` package | **un-importable**: `__init__` imports `replay_chain` (never defined in instrument.py) and `bridge.py` (not shipped). Any consumer dies at import (A04). Broken since the underconfidence autosave; first flagged by run 8 on an unmerged branch; still live. |
| `agp.estimate.rescore` | zero production callers (diagnostic-only, self-declared — acceptable). |
| `synthesize()`/`confidence_from_agreement` layer | `confidence_from_agreement` is used inside synthesis.py group scoring, but the instrument's own note that the corroboration layer influences nothing downstream remains accurate until improve/synthesis-adoption merges. |
| cross-run memory | wired via `crossrun_store=None` default; NO production constructor passes a store (`callisto.py`, retro, scripts all omit it). Built, merged, inert. |
| verify_artifacts | genuinely wired pre-seal (engine.verify_artifact_gate) — the A6 fix is real. |
| stasis_stop | reachable via retrieval hook but `self.stasis_stop = None` default and no production constructor sets it — inert unless someone opts in. |

## PART 5 — WHAT IS STILL OPEN (audit script + manual verification)

findings-audit.sh flags 12 tags; verified against code:

| tag | verdict |
|---|---|
| F6a (self-review cap unknown-author) | **FIXED** — ensemble handles empty author_model conservatively |
| F6b (identity = spelling) | **OPEN** — alias-marker list covers proxy suffixes only; "Claude Sonnet 4" vs "claude-sonnet-4" read independent (A14) |
| F6c (empty panel = approval) | **PARTIAL** — backend failure fails closed; parsed-but-empty panel and all-junk panel approve (A15) |
| F2 redteam_confidence (clamp rounds up) | mostly fixed via floor_conf; residual: inherited_ceiling's round(,4) can land ON the cap boundary (cosmetic-to-minor) |
| F3 (relabel floors upward) | **FIXED** — probed: 0.05 stays 0.05 through demotion |
| F6b laundering variant | same as above |
| F6a laundering variant (unknown author) | **FIXED** |
| M3a (portfolio duplicates) | **OPEN** (A06) |
| M6 (crossed book CLV) | **OPEN** — crossed claim + healthy close yields signed CLV from corruption alone (A07) |
| F2 money (three clv writers, two units) | **OPEN** — order_reconciler.py:627 writes raw `(close−place)·10000` into the canonical devigged column the promotion gate reads |
| F10 (closing-line point guard) | **OPEN** — UPDATE predicate has event/market/team only (A08) |
| S6 (retro substring horoscope) | **FIXED** — declared stance in both copies |

Plus the run-11 items I re-confirmed still live: claims journal newest-entry
tamper (CRITICAL), prereg amended-default (A09), cmefedfut fail-open ×2
(A10), FR _rename dead code (A11), crossrun 'default' bucket (A12),
prefix-junk relevance gate (A13), kelly_full rounding (A05).

## BLUNT CALLS

1. The single biggest failure mode of the last three days is not bad code —
   it is **finished-looking writeups attached to unmerged branches**. Three
   separate agents documented CRITICAL/HIGH fixes that exist only on their
   branch, while master kept the bug. The findings directory currently
   describes a better system than the one on master.
2. The merge train lands features but not their fixes' dependencies: the
   estimate prototype merged without bridge.py, leaving tools.calibration
   un-importable on master — a measurement package killed by its own import
   line.
3. Money-path sizing still cannot be trusted: kelly_full doubles stakes at
   tiny edges, portfolio kelly rewards stacking the same bet, and canonical
   CLV is polluted by a raw-unit writer. None of this is novel — every item
   has a written repro sitting in findings/ — it simply was never merged,
   never flipped to a pin, or half-fixed.
4. What held up deserves saying: engine_merge's hunk-level reasoning, the
   speed triage's refusal of unmeasured claims, verify_artifacts wiring, the
   declared-stance forecast sign landing twice consistently, and the
   information-gain tests (which survived my mutation attempts).

Repros: `tests/test_review_0824_audit.py` (17 failing). Prior-run repros in
`tests/test_review_2026-08-24.py` re-verified: 10/10 still failing on
96e09c9.
