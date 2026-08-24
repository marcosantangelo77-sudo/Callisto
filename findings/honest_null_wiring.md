# Honest-null wiring: every conclusion now states which of the three it is

`tools/gaps.py` modelled the right distinction and was imported by nothing but
its own test. Meanwhile `tools/pipeline/synthesis.py::classify_null` carried a
second private copy of the membership rule that had already drifted (it matched
only the exact skip string `"no generic route"`). This change makes gaps.py
canonical, wires the verdict onto the sealed result, and adds a third kind for
"we have evidence but cannot prove it to our own bar".

The three-way split (`tools.gaps.NullKind`):

| kind                 | meaning                                            | owner response            |
|----------------------|----------------------------------------------------|---------------------------|
| `honest_null`        | searched competently, nothing there                | accept, move on           |
| `retrieval_failure`  | signal exists; we failed to FETCH it               | fix the fetcher           |
| `unprovable`         | evidence in hand; cannot PROVE it to our standard  | deliberate bar decision   |

**Gate rule preserved:** classification only. Nothing reads `gap_kind` when
scoring; no code path raises or lowers a confidence because of it. Pinned by
`tests/test_gap_verdict_wiring.py`.

## What changed

1. `tools/gaps.py`: new registry-free `classify_null_kind(trace)` — THE single
   membership rule. Any skipped source (any reason) is a retrieval failure;
   gate rejections with reasons are an honest null; mixed runs disclose
   partial coverage instead of laundering it into silence.
2. `tools/pipeline/synthesis.py::classify_null`: rewritten as a thin adapter
   delegating to `classify_null_kind`. The duplicated rule is gone.
3. `tools/pipeline/engine.py`: `LeafOutcome.gap_kind` / `.gap_explanation`
   populated per leaf; surfaced in `PipelineResult.summary_dict()["gap_kinds"]`
   AND printed inline in the parent conclusion text as `[GAP: <kind>]`, so a
   sealed result carries the verdict where users actually read.

## Worked examples

Real question: *"Does semiconductor export-control policy since 2022 reduce
foundry capacity concentration in East Asia?"* All three outputs below are
from actual calls to the shipped code (not hand-written).

### Kind 1 — HONEST NULL (`classify_null_kind` on a real trace)

Sources were queried; the relevance gate rejected what came back, with
reasons:

```
kind: honest_null
sources were queried and returned no relevant material (2 rejected at the
relevance gate): [openalex] covers 0% of query terms;
[federalregister] returned only tariff-notice titles, none about foundries
```

Reading: "the accessible literature is silent on this." Acceptable as a null.
Owner action: move on (or widen sources deliberately).

### Kind 2 — RETRIEVAL FAILURE (same question, different trace)

The source that would plausibly hold the data was never fetched; FRED was
rate-limited:

```
kind: retrieval_failure
this is a RETRIEVAL FAILURE, not a finding — do not read it as 'the
literature does not address this': source errors: fred: HTTP 429 too many
requests | no fetch route for: semiconductor_digest
```

Reading: "we could not look." Before this change this surfaced identically to
Kind 1 — that conflation is how a research engine becomes confidently wrong.
Owner action: fix key/rate-limit/route, re-run. Never cite this leaf's absence
as literature silence.

Mixed case (some sources answered-and-rejected, one errored) stays an honest
null on the evidence obtained but appends "NOTE some sources also errored,
coverage may be partial" — partial coverage is visible, never laundered.

### Kind 3 — UNPROVABLE (`LeafOutcome` after `_answer_leaf`)

Evidence WAS obtained (a trade-press fetch stating "export controls reduced
foundry expansion by an estimated 12%"), but the question declared its own bar:
PRIMARY class, ≥2 independent sources. One SECONDARY voice came back:

```
gap_kind: unprovable
gap_explanation: evidence was obtained but does not meet this question's
declared standard: best evidence class SECONDARY < required PRIMARY;
1 independent sources < required 2
confidence: 0.54
```

Note 0.54 is exactly what the pre-existing requirement ceiling produced — the
verdict describes the situation, it does not create it. Owner action:
deliberate decision about the bar (relax the requirement, or buy/collect
PRIMARY evidence), never silent acceptance of thin proof.

## Verification

- `tests/test_gap_verdict_wiring.py`: 9 tests — the conflation guard is
  asserted IN THE PROSE ("RETRIEVAL FAILURE … do not read") so the enum alone
  cannot satisfy the contract; skip-reason drift regression pinned.
- Full suite before vs after (isolated baseline worktree at parent commit):
  identical failure sets — **38 failed / 11161 passed both sides**, zero new
  failures. Two modules (`test_ml_classifier.py`, `test_ml_drift.py`) fail
  collection pre-existing on missing `joblib`; excluded symmetrically.
  Note: the branch's true baseline here is 38, not 51 — the 51 figure does not
  reproduce on `review/rotating-0823-184936`.
- Commits: `44c5fd3` (consolidation), `08f0d61` (wiring). Pushed to
  `review/rotating-0823-184936`.
