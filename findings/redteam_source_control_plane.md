# RED TEAM — source control plane: health coverage, voice identity, reachability

**Date:** 2026-08-24 · **Branch:** `redteam/source-control-plane`
Repros: `tests/test_redteam_source_control_plane.py` — **5 fail against
current code**, 3 honest-negative pins pass.
`python3 -m pytest tests/test_redteam_source_control_plane.py -q`

## Surface choice, stated

The rotation list names two unattacked grounds: *the source registry and
query builders* and *the source registry and independence families*. While
mapping prior passes I found an **unmerged concurrent branch**
(`redteam/source-registry` @c4b5942) that had claimed the fetch/admission
seam of exactly that ground — seven defects there (zero-result admission,
BLS-only envelope chokepoint, unshared limiter, redirect laundering,
resolution precedence x2, near-tie order, dead independence rule). This
pass therefore takes the **control plane above its seam**: the tables and
counters that decide what MAY run and what COUNTS —

- `tools/sources/health.py` — which sources the health layer actually watches,
- `tools/pipeline/retrieval.py::retrieve` — which registered sources can ever
  be selected (the hardcoded `max_tier=3` call),
- `trace.independent_keys` — what counts as an independent voice.

Nothing here duplicates the concurrent branch's repros; where the two passes
touch the same module I say so explicitly.

## Method, stated

Property/coverage sweeps over the control plane's own bookkeeping, chosen
because adversarial input / differential / seam / property-sweep / mutation /
corrupt-one-field are all now spent elsewhere: **state the invariant the
table claims, enumerate the full space (every registered source × every
probe key × every planner route), and check the table against the world.**
Hand-written cases are how the drift below survived — each individual piece
looks fine; only whole-set reconciliation exposes it. One differential pin
locks a coincidence two independence call sites silently rely on.

Families hunted (per PATTERNS.md): **1** (verification that never runs —
twice), **3** (absence treated as success), **5** (structural property in
place of actual agreement).

---

## CONFIRMED DEFECTS

### H1 · HIGH — the health probe table has drifted off the registry; four sources unchecked, three probes name ghosts

`health.PROBES` was written against **module filenames**; registration uses
**spec names**. Reconciling the two sets today:

    registered but UNPROBED : cftc_cot, cmefedfut, sec_fulltext, semanticscholar
    probe keys w/o a source : cftc, sec_fts, semantic_scholar

Only `semantic_scholar`→`semanticscholar` survives, by luck of `_build`'s
underscore-stripping alias rule. `run_all(names=None)` iterates `sorted(PROBES)`,
so:

- **cmefedfut produces no row at all** in the default health report — family 3:
  the summary line reads green over a source nobody checks;
- `cftc_cot` and `sec_fulltext` — two of the five sources whose historical
  live-API defects motivated this very module's docstring — have **no working
  probe**: their keys raise KeyError inside `_build` (family 1: a check that
  cannot run);
- `main()` exits nonzero forever because two phantom keys permanently report
  BROKEN regardless of live health — alarm fatigue that trains operators to
  ignore the tool (the D4 failure mode from the CLI pass, inverted).

Family 2 is visible inside the defect: the same name-mapping rule exists
three times (`_build` alias loop, probe decorator strings, registration list)
and two of the copies disagree. Nothing reconciles probe coverage against
registration at startup or test time — the missing check is itself missing.

Tests: `test_h1_probe_table_covers_registry_exactly`,
`test_h2_every_probe_key_resolves_through_build`,
`test_h3_default_run_all_reports_on_every_registered_source`.

Fix shape: derive probe keys from the registry (one source of truth); assert
`set(PROBES) == set(registry.names())` at import/test time so the next added
adapter cannot ship without a probe or under a stale name.

### V1 · HIGH — byte-identical content from two hosts counts as TWO independent voices

`trace.independent_keys` is keyed on declared identity only
(`independence_key(spec.name, spec.base_url)`); the content hash — computed
for every admitted fetch — is never consulted by the counting rule. Two
unrelated adapters serving identical bytes (a mirror pair, a live page and
its wayback snapshot, one upstream republished) each add a key:

    independent_keys = ['alpha.example', 'beta.example']
    stop_reason      = 'sufficient: 2 independent sources >= required 2'

Zero corroboration exists — a document corroborated by ITSELF satisfies
`min_independent_sources=2`. Family 5 verbatim: structural multiplicity
standing in for agreement, with the one signal that would catch it (equal
`content_sha256`) sitting unused in the trace. Distinct from the concurrent
branch's S1 (metadata-echo empties): V1 holds even when BOTH bodies are real,
relevant, and identical.

Test: `test_v1_identical_bytes_from_two_hosts_are_one_independent_voice`.

Fix shape: a voice is `(independence_key, content_sha256)` — or subtract
duplicate-content keys before sufficiency. Either direction errs safe.

### R1 · MEDIUM — gdelt is registered, planned, probed… and unreachable by the retriever

The retriever hardcodes `registry.select(translated, max_tier=3)`
(retrieval.py:572). `gdelt` declares `tier=4`. It has a complete query-builder
plan (`_plan_gdelt`) and a health probe, yet **no question can ever select
it** — the machinery exists, looks authoritative, and never executes (family 1
at the selection seam). The registry advertises 21 sources; the pipeline can
reach 20, and nothing anywhere says so. Any future tier-4/5 adapter inherits
the same silent hole.

Test: `test_r1_every_planned_source_is_selectable_at_retriever_ceiling`.

Fix shape: either the retriever takes its ceiling from the leaf's declared
requirements (tier ceilings are a provenance policy, not a retrieval constant),
or registration warns when a spec's tier exceeds every caller's ceiling.
Related drift noted en route: `_HONEST_GAPS` is keyed `"sec_fts"` while the
registered name is `sec_fulltext`, so SEC's "deliberate gap" message renders
as `unknown source 'sec_fulltext'` — same disease as H1, third table over.

---

## HONEST NEGATIVES (pins, passing)

- **Planned URLs stay on the declared host** (all registered adapters × a
  question battery): `trace.independent_keys` are computed from
  `spec.base_url` while `why.independence_from_fetches` recomputes from
  `f.url`. Those two agree ONLY while no planned fetch leaves its host.
  True today everywhere; pinned as
  `test_pin_planned_fetch_urls_stay_on_declared_host`. Known way to break
  it: `wayback.fetch_snapshot()` fetches `web.archive.org` against base_url
  `archive.org` — if a snapshot fetch ever enters the fetch list, the trace
  and the audit disagree about the same bytes. Latent, not live.
- **Family collapse agrees across all consumers** — `independence_key`,
  `in_family`, and `why.independence_from_fetches` all collapse
  openalex/semanticscholar including spelling drift
  (`Semantic-Scholar`, `semantic_scholar`). The original why.py defect stays
  dead. Pin: `test_pin_family_collapse_agrees_across_all_consumers`.
- **A purely empty body is still rejected** by the relevance gate — whatever
  fix lands for the concurrent branch's S1 must keep true emptiness out.
  Pin: `test_purely_empty_body_is_still_rejected`.

---

## Relation to other passes

- Concurrent UNMERGED `redteam/source-registry` (@c4b5942) attacks the seam
  beneath this surface; its S2 fix plan ("move envelope shapes from
  health.py's probe table into the classifier") should be coordinated with
  H1 — the probe table needs rebuilding against registry names anyway, so
  land both from one corrected mapping.
- CLI pass D4 (doctor cannot fail) is the display-side twin of H1: doctor
  reports theatre about providers/DB, health reports theatre about sources.
- Retrieval starvation pass fixed BLS envelopes at the same chokepoint the
  concurrent branch generalises; R1 shows the selection layer can also
  starve a healthy source — starvation by ceiling instead of by gate.

No live API was contacted; all transports injected, urlopen untouched (no
redirect test needed network). No confidence score raised anywhere. The
test-file commit was captured by the autosave daemon as 9deab52 before this
findings commit; content identical to this branch's checkout.
