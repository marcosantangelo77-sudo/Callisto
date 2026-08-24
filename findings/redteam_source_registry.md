# Red Team: the source registry, query builders & independence families

**Date:** 2026-08-24 · **Branch:** `redteam/rotating-0824-190848`
**Surface:** source registry + query builders + independence families —
chosen because it is the only named unattacked ground left (findings/redteam_*
already covers calibration, checkpoint/resume ×4, concurrency, loop,
pipeline_wiring, provenance, retrieval relevance/starvation, seal, synthesis,
money path, artifacts, CLI persistence, mutation) and because MORNING_REPORT
names this layer as the live bottleneck ("source diversity", "relevance
gating at ingestion", query authoring).
**Method:** property-based sweep over a parameter space (name × URL × payload
mutations), plus one full-chain differential through the production
`IterativeRetriever` with fixture transports. Method rotated per standing
order: adversarial input, live-vs-resumed differential and seam analysis were
taken; mutation testing was taken last pass.

**Tests:** `tests/test_redteam_source_registry.py` — 10 failing expectation
tests (written failing first), 5 passing boundary pins. Verified they add no
other failures to the suite (stash-diff against baseline).

---

## THE HEADLINE

**A source that returns HTTP 200 with ZERO rows of data is admitted as
evidence, counted as an independent voice, satisfies
`min_independent_sources`, and uncaps the requirement ceiling — for any
question that names the current year.**

Two empty envelopes from two hosts produce `stop_reason = "sufficient: 2
independent sources >= required 2"` and `unmet_reasons(...) == []`, so the
SPECULATIVE cap does not apply and the leaf may seal up to the SECONDARY
ceiling (0.75) on literally nothing. Reproduced end-to-end through the real
`IterativeRetriever`; see `test_INV2`/`test_INV3`.

This is not a new family. It is the three worst known families compounded on
one seam:

- **#3 absence treated as success** — zero rows admitted as a hit;
- **#1 a check that cannot fail** — the structural route's "carries at least
  one numeric VALUE" requirement;
- **#9 internally consistent, externally wrong** — every gate then behaves
  correctly on evidence that should never have existed.

## Root cause chain (three defects, any one of which closed alone stops it)

### RC1 — fetch METADATA is judged as content (`tools/sources/*.py`)
Eight adapters (`worldbank`, `bls`, `bea`, `census`, `cftc`, `eia`, `fdic`,
`fred`) embed a `_fetch` provenance block INTO the returned payload:
`{"url", "sha256", "fetched_at"}`. The block's `fetched_at` is an ISO
timestamp of **fetch time**. The relevance gate judges the whole payload as
body text (`extract_text` keeps strings at any depth ≤ 6). Consequences:

1. *Token credit*: the question's year token ("2026") exactly matches the
   year inside `fetched_at`. A three-token question needs ONE match to clear
   the 25% coverage floor → admission by metadata alone.
2. *Structural route*: `fetched_at` supplies the in-window ISO date the D4
   numeric-window route requires.

Fix direction: strip `_fetch` (and any non-content envelope keys) BEFORE the
gate sees the payload — or gate on a body the adapter declares as content.

### RC2 — the value check is vacuous (`tools/pipeline/retrieval.py:120-128`)
`numeric_window_matches` requires "≥1 numeric value that is not part of a
date". The loop body is `return True` in BOTH branches of its first
iteration (the bare-year acceptance makes the year/non-year distinction dead
code). Any first number ends the scan with True — including the time-of-day
components (`19`, `09`, `12`) of the fetch timestamp itself. Requirement (d)
therefore reduces to requirement (b): once a date exists, a "value" always
exists. A check that cannot fail is not a check.

Fix direction: search for values OUTSIDE date stamps AND outside declared
metadata zones; require the number not to be a bare calendar year unless a
sibling field names it as a measurement; pin with `test_INV4*`.

### RC3 — error envelopes from sources other than BLS are invisible
(`tools/sources/query_builder.py:classify_fetch_failure`)
The D2 fix keyed envelope classification to `source_name == "bls"` only.
World Bank answers invalid parameters with HTTP 200 + a message array;
`worldbank.indicator` maps that to `{"total": 0, "rows": []}` and discards
the message entirely — so a parameter/auth failure reaches the gate as
ordinary empty data and reads downstream as "the literature says nothing"
(an honest-null misclassification) rather than "we fetched wrong". Same class
of defect the BLS fix closed, one copy over — family #2 again.

Fix direction: table-driven per-source envelope classifiers (or a shared
"did this payload carry results?" contract), tested across all 19 adapters.

## Secondary findings on the same surface

### S-A — the independence rule still has an unfixed third copy
`tools/sources/base.py:independence_family` (the copy declared next to
`INDEPENDENCE_FAMILIES`, presented as canonical) does RAW membership — no
normalisation, the exact defect PATTERNS.md records at `sources/base.py:339`
— and has ZERO production callers. Dead code guarding nothing; the first new
consumer inherits the bug (`test_INV9`: diverges under mere case change).
Meanwhile `independence_key`'s second loop (`if source_name in members`) is
unreachable given its own normalised first loop — residue of the same story.

### S-B — family membership escapes affix drift (property sweep, 12/24)
For family members {openalex, semanticscholar}, 12 of 24 plausible spelling
mutations escape the collapse and become standalone voices: `api.` prefix,
`-api` suffix, `.org`, accented characters, leading "the ", trailing digits.
Case/separator/whitespace drift is correctly normalised (`test_NEG_...` pins
that). Reachability today is low — production `source_name` always equals
`spec.name` — but checkpoint payloads and cross-run records replay
historical spellings verbatim, and the module's own stated rationale is that
"naming drift must not be able to manufacture independence".

### S-C — the structural route has NO topicality constraint
`numeric_window_matches` admits ANY structured numeric body whose dates fall
inside the asked window, regardless of subject (`test_INV6`: treasury-style
interest-rate rows admitted for an unemployment question). Documented as
deliberate in D4, but combined with RC1/RC2 the route currently needs no
topic signal whatsoever. Any fix should add a minimal topicality condition
(e.g. ≥1 topical token OR the admitting source was selected with score ≥
min_score for THIS question).

### S-D — label-string coupling in the gain gate (latent, family #4-lite)
`estimate_gain` detects the independence shortfall by substring:
`"independent sources <" in r` against `unmet_reasons()` prose, then
compares whole lists for equality (`indep_short == reasons`). Both work
today (order is stable, wording matches), but the gate's correctness hangs
on two strings in another module staying byte-compatible. Pin them with a
cross-module test when fixing.

## What I could NOT break (honest accounting)

- **Selection scoring**: the diagnostic-floor claim holds — a caller passing
  `min_score=0.99` gets no sub-0.99 inclusion; floor bypassing is genuinely
  prevented (the comment's own stated trap).
- **`ok_any ⇒ score 1.0`** invariant in `select_explained` held under sweep.
- **Canonical-spelling three-copy agreement** holds (`test_NEG_...`); the
  divergence is drift-only.
- **Past-year and no-year questions** reject the empty envelope — the hole
  is precisely current-year phrasing, which is the common live case ("what
  IS X?"), but worth stating exactly.
- **Planner robustness sweep** (all 18 planners × adversarial question
  strings): no crash, no unplannable-with-empty-queries state found;
  injection-safe by construction (core_query strips quotes/operators before
  interpolation into FDIC filters / SPARQL / USPTO query strings).

## Reachability in production

Requires a leaf whose question names the current year AND a selected source
returning a zero-row/error envelope not covered by RC3's BLS-only
classifier. World Bank invalid-code envelopes are the concrete instance
(the planner's honest-gap fix routes unknown concepts away, but explicit or
auto-resolved codes can still be wrong/stale). Every current-year descriptive
leaf in the harness runs through this gate.

## Reproducing

```
python3 -m pytest tests/test_redteam_source_registry.py -q
#   10 failed (expectations), 5 passed (boundary pins)
python3 - <<'EOF'
import sys; sys.path.insert(0,'.')
from tools.pipeline.retrieval import RelevanceGate, numeric_window_matches
env={"total":0,"rows":[],"_fetch":{"url":"u","sha256":"a"*64,
     "fetched_at":"2026-08-24T19:09:12Z"}}
print(RelevanceGate().judge("What is the US unemployment rate in 2026?","",env))
EOF
# -> (True, 0.333, 'content covers 33% ...')  admission on pure metadata
```

## Fix order (smallest safe diff first)

1. Strip `_fetch`/envelope keys from payloads before gating (kills RC1 both
   paths; also fixes INV5/INV6's free ride).
2. Make the value check falsifiable: exclude date-stamp and metadata-zone
   numbers; require a non-year number adjacent to a value-ish key, or drop
   the branch and require ≥2 distinct numbers with at least one decimal
   (INV4 pins).
3. Table-driven envelope classification across adapters (INV7), reusing the
   BLS pattern.
4. Delete or normalise-and-wire `base.independence_family` (INV9);
   normalise affixes or assert spec.name provenance at replay boundaries
   (INV8).
