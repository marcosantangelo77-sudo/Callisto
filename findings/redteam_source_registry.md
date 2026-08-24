# REDTEAM — source registry & query builders (rotating pass, 2026-08-24)

**Worktree:** `loop` · **Branch:** `redteam/rotating-0824-173254`
**Surface:** the source registry and query builders — `tools/sources/registry.py`,
`tools/sources/query_builder.py`, `tools/pipeline/retrieval.py`, plus the
engine seam that composes leaves into a parent (`tools/pipeline/engine.py`).
Explicitly named unattacked ground: *"the source registry and query builders
(what happens when a source lies, or returns 200 with zero results)"*.

**Method:** ADVERSARIAL INPUT + PROPERTY SWEEP (rotation requirement: not yet
used on this surface). The invariant under sweep: *a body that carries no
content beyond an echo of its own query must never become evidence, and a
document sharing only word-prefixes with a question must never satisfy
relevance.* Plus a differential-style check of one fix's MERGE STATUS, which
is where the biggest finding lives.

**Deliverable:** `tests/test_redteam_source_registry.py` — **7 fail on the
current branch**, 3 honest pins pass. Run:
`python3 -m pytest tests/test_redteam_source_registry.py -q`

Families hunted: #1 (verification layer that cannot fail), #3 (absence as
success), #2 (fix landed in one copy only), and #9 (internally consistent,
externally wrong) — see SR2.

---

## SR2 (CRITICAL, family #2/#9): the stance-propagation fix was NEVER merged

`dd4fb18` ("defect 1+3: parent stance from decisional leaves only; single-
source answers say so", branch `fix/stance-propagation`, also on
`origin/review/deep-audit-0824`) fixed exactly the defect that produced the
wrong-direction PROBABLE seal in PATTERNS family #9: parent stance taken from
`best_leaf = max(answered, key=confidence)`.

**It is not in master.** Verified:

```
git merge-base --is-ancestor dd4fb18 origin/master  -> NOT an ancestor
git show origin/master:tools/pipeline/engine.py | grep -n best_leaf
  1023: best_leaf = max(answered, key=lambda l: l.confidence)
  1026: parent_stance = best_leaf.stance          # unfixed on what ships
```

The current worktree branch (which carries `db08c13`, i.e. is at or ahead of
origin/master) still has the vulnerable selection. Every downstream consumer
of the pipeline inherits the C1 failure mode from
findings/redteam_answer_correctness.md: a 0.95 lookup leaf affirming its own
sub-question outvotes the 0.55 leaf that actually bears on the root claim.
This is family #2 with a twist — the two copies are two BRANCHES, and only
the unmerged one was corrected.

## SR1 (CRITICAL, families #1+#3): 200-with-zero-results becomes evidence AND an independent voice

`IterativeRetriever._fetch_one` checks only `last_record.status != 200`. The
relevance gate then judges the PARSED body on token coverage. An API that
answers HTTP 200 with an error object or a zero-hit list **echoing the query
words** — extremely common (`{"query": ..., "results": []}`, OpenAlex meta
blocks, quota-exhausted JSON errors) — passes both:

```
RelevanceGate().judge("unemployment rate in 2026", "",
    {"query": "unemployment rate 2026", "results": []}) -> (True, 1.0)
```

End-to-end through the retriever (fixture transport): all three echo/error
shapes are ADMITTED, their host enters `trace.independent_keys`, and the leaf
stops `"sufficient: 1 independent sources >= required 1"`. One lying or
broken endpoint therefore manufactures both the evidence and the independence
count it is scored on — absence treated as success (#3), and the gate that
was built to stop this (wave 4, "relevance gating at ingestion") is inert for
exactly the input class it was built for (#1). Tests:
`test_sr1_zero_result_echo_body_is_not_admitted_as_evidence[0..2]`.

Fix direction (for whoever owns retrieval): zero-result bodies and bodies whose
extractable text is dominated by the query's own tokens should be recorded as
nulls (with reason), never admitted; a host contributing only such responses
must not enter `independent_keys`.

## SR3 (HIGH, family #5-ish): prefix matching admits unrelated documents

`RelevanceGate.judge` matches question token t if any document word starts
with t (or vice versa). Property sweep over collision pairs: **7/8 admit** —

| question word | document about | admitted |
|---|---|---|
| gas | gastrointestinal | yes |
| coal | coalescing | yes |
| rate | ratification | yes |
| gold | golden retriever | yes |
| debt / oil / bond variants | ... | mostly yes |

A document can hit 100% coverage while being about something else entirely;
it then rides into evidence, counts a voice, and (per F-class history)
raises confidence through corroboration counting. The gate measures lexical
overlap, not agreement — the same shape as family #5. Tests:
`test_sr3_prefix_collision_*` (4 parametrized failures).

## SR4 (MEDIUM, family #4): honest-gap table keyed by a name that no longer exists

`query_builder._HONEST_GAPS` keys `'sec_fts'`; the registered adapter is
`'sec_fulltext'` (memory even flagged the spec-name drift before). Result:
`build_plan('sec_fulltext', ...)` returns `PlanResult(False, reason="unknown
source 'sec_fulltext'")` instead of the deliberate-gap message; the gap report
and the planner disagree about which source is deliberately unplannable. A
label standing in for evidence (#4) — here a label drifting makes BOTH the
honest refusal AND any future fix invisible. Test:
`test_sr4_honest_gap_table_matches_registered_adapter_name`.

## SR5 (LOW, pinned honest-negative): >4000-char primary fetches hash truncated

Engine builds `Evidence(content=f.body[:4000])`; the ledger recorded the FULL
body. `assign_source_class` hashes the truncated string → no observation →
INFERRED. Direction is safe (demotion), but long primary documents silently
lose PRIMARY class unless the model echoes a fetched URL (which then re-opens
the F4 laundering hole as compensation). Pinned in
`test_sr5_long_primary_body_hashes_truncated_vs_full`.

## Honest negatives (attacked, held)

- Independence family collapse: `independence_key` normalises spelling
  correctly across openalex/semanticscholar and legacy `semantic_scholar`;
  could not manufacture a false independent voice there.
- Registry selection on real questions improved vs MORNING_REPORT: all six
  probe questions now select plausible sources; strict `min_score=0.99` still
  binds (diagnostic floor does NOT bypass caller strictness).
- `estimate_gain` duplicate-voice skip logic: could not make it skip a
  genuinely new voice nor keep a useless duplicate.
- `_fetch_one` rejects non-200 statuses when the transport reports them.

## Reproduce

```
python3 -m pytest tests/test_redteam_source_registry.py -q   # 7 failed, 3 passed
git show origin/master:tools/pipeline/engine.py | sed -n '1023,1026p'
```
