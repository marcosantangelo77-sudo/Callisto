# RED TEAM — source registry & query builders (rotating pass, 2026-08-24)

Worktree: `loop/`, branch `redteam/rotating-0824-175908`.
Repros: `tests/test_redteam_source_registry.py` — 11 strict-xfail canaries +
21 passing pins. No production code was edited (red-team role). No network
was touched; everything ran against the pure planner and a stub transport.

## Surface choice

**Source registry + query builders** — unattacked ground: none of the twelve
prior passes (`calibration … synthesis`) covered it, four of which were
resume/checkpoint variants. The morning report itself names this layer as
the bottleneck ("retrieval is the weak link"), so defects here sit on the
critical path of every live run.

**Method choice:** property-based sweep + adversarial input — mutation
testing was just used (redteam_mutation.md), and this seam is exactly where
random-input sweeps pay: `build_plan` is a pure function from strings to
plans, so "never raises, never emits a malformed plan" is sweepable.
4,000 generated questions × 19 planners found the headline defect in one run.

---

## S1 (CRITICAL) — wikidata planner raises TypeError; one leaf kills the run

`_plan_wikidata_concept` builds `matched = [(Q-id, hint-string)]` then sorts
with `key=lambda p: -p[1]` — negating a STRING. Any question containing at
least one hint word (`company`, `drug`, `country`, `person`, …) crashes:

```
TypeError: bad operand type for unary -: 'str'   (query_builder.py:706)
```

Confirmed END-TO-END: with the real registry, the question
"graph queries who held office when, companies" selects wikidata, and
`IterativeRetriever.retrieve()` propagates the TypeError out of its
routability loop (retrieval.py:502 — no try/except). The engine gathers
leaf fetches with `return_exceptions=True` then `raise oc` in leaf order,
so **one leaf's entity question aborts the entire multi-leaf run** before
any answer is assembled.

The sweep found it in 1,329 of 4,000 generated cases — this planner has
likely never served a real entity question.

Family 1 adjacency: the module's own docstring promises "unknown sources get
an honest `PlanResult(plannable=False)` rather than a guessed call"; here an
*ordinary known* source neither plans nor refuses — it detonates.

## S2 (HIGH) — registry/planner name drift (FAMILY 2, three instances)

The fix-history repeats itself:

- `_HONEST_GAPS` keys `"sec_fts"` but the spec's name is `"sec_fulltext"`
  (sec_fts.py:27) → the deliberate SEC deferral note can never fire; SEC is
  skipped as "unknown source", a message that misdescribes a registered source.
- `cmefedfut` and `kalshi` are registered, selectable (they appear for
  "unemployment rate among companies"), and have NO planner and NO honest-gap
  entry → every selection round records them as skipped "unknown source".
- (Pre-existing, already documented in-code: `semantic_scholar` vs
  `semanticscholar` needed a legacy alias after I2.)

Same shape as D2's stage-name blindness and F6b's model-spelling
independence: a string key decides behavior, two spellings disagree, nobody
pins them together.

## S3 (MEDIUM) — census window direction unpinned (FAMILY 6)

`_plan_census` takes years in arrival order: "housing starts 2023 to 2021"
emits `start=2023-01, end=2021-12`. Property sweep over random year pairs:
919/2000 inverted windows. An inverted window either errors at fetch time or
returns whatever the API does with a backwards range — never what the question
asked. Same family as the round-up bug: ask which direction the error moves;
here it silently manufactures an empty dataset that reads as "no data".

## S4 (MEDIUM) — treasury filter pins the FIRST date, drops the range

`re.search(r"(20\d{2})-(\d{2})-\d{2}")` takes the first date:
"national debt 2019 through 2024-06-30" → `record_date:gte:2024-06-30`,
silently discarding everything before mid-2024. The question's window is
halved with no note on the plan. A gte-only filter must use the EARLIEST
date mentioned (or both bounds).

## S5 (MEDIUM) — FDIC proper-noun capture queries people as banks

"Compare Janet Yellen statements regarding Bank of America" →
`filters="NAME:Compare Janet Yellen"` — longest-proper-noun wins, bank or
not. The fetch will return zero institutions, which the gate treats as an
honest null rather than a query-authoring failure (family 3: absence read
as answer).

## S6 (LOW) — wayback bare-domain regex fabricates URLs from prose

"compare e.g. 3.5.org" → `url=https://5.org`. The bare-domain fallback will
fetch any `<token>.<tld>` fragment of prose. Low severity (wayback is
auxiliary) but it is a fabricated-fetch vector.

## S7 (LOW) — five dead validator regexes (FAMILY 1 residue)

`_FRED_ID_RE`, `_BLS_ID_RE`, `_CIK_RE`, `_WB_INDICATOR_RE`,
`_TREASURY_DATASET_RE` are compiled with docstring claims ("exact series ids
pass through untouched") and referenced by nothing. The id-passthrough path
in `_resolve` uses its own ad-hoc token rules instead. Dead guards next to
live guessing — the exact residue family 1 leaves behind.

## S8/S9 — sweep infrastructure and resolution semantics

- S8 pin: `build_plan` must be TOTAL over adversarial inputs (unicode,
  NULs, 10 KB strings, embedded quotes, multiple dates/Q-ids/URLs). Today it
  fails via S1; the pin holds the contract for all other planners.
- S9 canary: `_resolve`'s exact-token passthrough auto-resolves "is GDP
  higher than CPI" to series GDP, bypassing the candidate/disambiguation path
  the docstring promises ("A wrong series id produces confident nonsense").
  The word GDP is prose, not a supplied id; resolution should demand stronger
  evidence (e.g. "Fetch GDP" / quoted id) before skipping disambiguation.

## Probed and NOT guilty (reporting honestly)

- Relevance gate on empty bodies: `[]`, `{}`, `None`, zero-result envelopes
  all correctly rejected (cov 0.0). Family 3 did NOT recur at the gate.
- Planner↔adapter signature drift: every PlannedQuery emitted across probe
  questions binds cleanly against its adapter class method. The W5 drift
  guard works.
- BEA year windows always monotone (later year last); no defect.
- FDIC filter charset guard (`_VALUE_OK`) matches the adapter's own
  `_VALUE_RE`; the duplicated-rule copies currently agree (family 2 risk
  noted for future edits).

## Summary table

| # | Defect | Family | Severity | Status |
|---|--------|--------|----------|--------|
| S1 | wikidata planner TypeError; aborts whole engine run | 1-adj | CRITICAL | repro failing (xfail strict) |
| S2 | sec_fulltext/cmefedfut/kalshi unknown to authoring; honest-gap key drift | 2 | HIGH | repro failing |
| S3 | census start>end windows, 46% of year pairs | 6 | MEDIUM | repro failing |
| S4 | treasury gte filter pins first date, drops range | 3-adj | MEDIUM | repro failing |
| S5 | FDIC queries person names as institutions | 4 | MEDIUM | repro failing |
| S6 | wayback fabricates URLs from prose fragments | 9-adj | LOW | repro failing |
| S7 | five dead id-validator regexes | 1 | LOW | repro failing |
| S9 | bare concept word auto-resolves as series id | 5-adj | MEDIUM | repro failing |

Fix ordering suggestion: S1 first (one-line sort-key fix + try/except
hardening in retrieve()'s routability loop so ANY planner crash degrades to
an honest skip, never a dead run), then S2 (name table single-sourced from
registry specs).
