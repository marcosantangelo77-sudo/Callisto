# RED TEAM — domain plugins & routing seams (2026-08-25)

Worktree: `review-ox` on branch `review/ox-alpha-0824d`.
Repro tests: `tests/test_redteam_domain_plugins.py` — **7 tests, all
failing-before** (committed first at 187c674 so the failures are verifiable).
All fixtures, no sockets; nothing executed, no money path armed. No
confidence was raised anywhere except in demonstrating that the system
raises its own.

---

## Surface and method

**Surface: the domain plugins and routing layer** (`tools/domain_registry.py`,
`tools/domains/{finance,kalshi}/`, `tools/sources/plugin.py`, and their seams
into `orchestrator.py`, `agp/provenance.py`, and the pipeline's
selection→fetch→gate chain). Explicitly named unattacked ground ("domain
plugins and routing"); twelve prior surfaces were resume/checkpoint-heavy,
and this one had never been touched.

**Method: wiring audit + adversarial input at the seams.** The wiring audit
is PATTERNS family #1 applied structurally: for every mechanism, "what calls
it?" The adversarial-input half is family #3: feed each gate the input it
was not written for — an HTTP 200 whose body is an error envelope. I chose
this combination because mutation testing was just used last pass
(redteam_mutation.md) and property sweeps twice before that.

---

## R1 — CRITICAL: a 200-with-error-body seals at PROBABLE/AFFIRMS

Families 3 (absence-as-success) + 9 (looks exactly like success), recurring
one layer deeper than the known "200 with zero results" defect.

`RestSource._record` (tools/sources/base.py:299) mints **every** fetched body
PRIMARY regardless of status code or shape. `RelevanceGate.judge`
(tools/pipeline/retrieval.py:129) then scores token coverage over ALL strings
in the parsed payload — including the error message itself. Real API error
messages name the thing that failed, so they carry exactly the words the gate
is looking for.

End-to-end, demonstrated with a fixture transport serving
`{"error": "...market probability fed decision...", "results": []}`:

    sealed: True | confidence: 0.55 | tier: PROBABLE | stance: AFFIRMS

The leaf carried the error envelope as its sole PRIMARY evidence; the answer
model wrote prose on top of it; the adversary raised nothing because the
evidence text looks plausible; the seal verified. Every internal signal
passed while the only fetch of the run had *failed*.

Blast radius: LOUD (a confidently wrong sealed conclusion from a failed
fetch — the exact failure mode family 9 says is worse than a refusal).
Fix direction belongs to whoever owns tools/sources/base.py + retrieval.py:
skip ledger recording for non-200 statuses; treat envelopes carrying an
error key / zero results as errors, not empty pages; never score an
envelope's error field for relevance.
For: owners of tools/sources/base.py (unowned this round).

## R2 — HIGH (unit root of R1): non-200 bodies are PRIMARY bytes

Same file, isolated so it can't hide behind the pipeline: a transport
returning `(503, body)` still yields a *parsed* dict from `get_json()` and
the body sits in the ledger as PRIMARY (`is_primary_bytes → True`). Any
proxy that converts upstream failures into 200 HTML/error pages, any
injected transport, any interstitial poisons provenance silently.
Evidence: probe run + test_503_body_is_recorded_primary_and_parsed.

## R3 — HIGH (family 1, inert mechanism): three DomainPlugins built, tested, never registered

"Registration IS the extension point" (BUILD_MANDATE item 3). Registered in
production: sports, compute. Built, documented, unit-tested end to end, and
called by **nothing but their own tests**: `finance` (EDGAR statements /
DCF / anomalies), `kalshi` (market data / market_edge), and the
source-registry plugin (`source_registry_list/select`). `_default_registry()`
(orchestrator.py:663) stops after two registrations; grep confirms zero
production callers of those three `register_if_available`s.

Consequence: a FINANCIAL session receives no EDGAR tools; no session can
call kalshi_market_edge; the registry-listing tool that was supposed to stop
the model "guessing at sources" does not exist at runtime. This is W5/C1/A6
again: a mechanism that looks authoritative and is inert. Blast radius:
SILENT (capability absence, not corruption).

## R4 — MEDIUM: selection routes to sources the router cannot serve

The source registry registers 21 adapters including `kalshi`, `cmefedfut`,
and `sec_fulltext`. `query_builder.build_plan` has planners for none of them
("unknown source"). Selection can therefore rank a source into the round's
candidates whose only possible outcome is a skip recorded after the fact;
budget and independence accounting absorb the hole without surfacing it to
the conclusion. Fix direction: derive registry membership from planner
coverage, or declare honest gaps keyed by spec name (sec_fts already does).
Blast radius: SILENT.

## R5 — MEDIUM (family 4): a label standing in for settlement

`KalshiMarket.resolved_outcome()` returns the result string whenever it says
"yes"/"no", even while `status == "active"`. Only `is_settled()` requires
both. `KalshiAdapter.resolution()` — marketed in its docstring as ground
truth for claim resolution — reads unsettled contracts as resolved. A
premature 'yes' flowing into OutcomeResolver-style scoring fabricates ground
truth, which is the input calibration itself trusts. Blast radius: SILENT
until it arms scoring, then ARMING.

## What I could NOT break (attempted honestly)

- **Independence collapse via naming drift**: `semantic_scholar` vs
  `semanticscholar` now normalise correctly through both `in_family` and
  `independence_key`; the earlier fix has landed in all copies I probed.
- **Adversary asymmetry**: apply_verdict has no bonus path; parse failures
  fail closed with distinct reason strings. Held under direct probes.
- **estimate_gain duplicate-voice bypass**: a source whose key is already
  counted is skipped when the ONLY unmet reason is independence — correct.
  It is admitted when quant/class shortfalls also exist, which is defensible
  (a fresh fetch can carry numbers); noted, not filed as a defect.
- **Model-controlled requirements**: the decomposer sets its own
  min_independent_sources (down to 1). Lowering the bar is the decomposer's
  declared job and the ceiling machinery still caps at 0.54 on shortfall;
  flagged as design tension (family 8 adjacent) rather than a defect.

## Family statement

R1/R2/R5 are instances of EXISTING families (3, 9, 4) recurring in modules
not yet swept — consistent with PATTERNS.md's premise. R3 is a new sub-shape
worth adding to family 1: **an extension point nobody extends through** —
the registration call exists, is idempotent, is tested, and is never made
outside tests. Hunt it by diffing "plugins built" against "plugins present
in the production registry singleton."
