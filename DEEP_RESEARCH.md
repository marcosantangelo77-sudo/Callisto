# DEEP RESEARCH — Instance 7 capability-gap analysis

Instance 7, branch `audit/tier7-research`. Question: what does Callisto need so the
owner can ask *"Is Bitcoin a good buy? What is your 1/5/10-year price target?"* and
get back models, spreadsheets, data, and the math — for any domain — with every
conclusion earned against reality?

Method: AUDIT_MANDATE §2 protocol. Everything below is tagged VERIFIED (I read or ran
the code cited) or INFERRED (reasoned from reading; falsifier given). Peer findings
from origin/audit/tier3-epistemics:findings/instance4.md and
origin/audit/tier2-gate:findings/instance3.md were fetched and are load-bearing here;
both were independently spot-checked where I rely on them.

The headline, up front:

> **Callisto's epistemic machinery is domain-general in design but betting-coupled
> in plumbing at exactly three joints: hypothesis intake, evidence resolution, and
> artifact production. Fix those three joints and the vision works. The hardest of
> the three — the horizon problem — has a clean answer that reuses the existing
> lifecycle rather than replacing it: decompose long-horizon claims into dated,
> near-term falsifiable sub-claims whose resolutions arrive on schedule, and let the
> long-horizon claim inherit confidence only from scored descendants.**

---

## 1. INTAKE AND DECOMPOSITION

### What exists [VERIFIED]

A query enters through one of two doors:

- **`POST /task`** → task queue → `tools/task_classifier.py`. Classification is
  keyword-regex into five budget buckets: `QUICK / NEWS / HYPGEN / DEEP / DEFAULT`
  (`classify_query`, tools/task_classifier.py:151). This decides *how long* to spend,
  never *what to do*.
- **Direct session** → `orchestrator.py::run_session` (:782), which runs the AGP
  seven-step pipeline.

Inside the AGP pipeline there is exactly one decomposition-like step:
`_step_enumerate_sources` (orchestrator.py:1009) asks the model for a list of search
queries/sources for THE query as a whole. That is source enumeration, not question
decomposition. There is no code path anywhere in agp/, orchestrator.py, or tools/
that turns an arbitrary question into sub-questions with independent evidence
requirements. Verified by search: no `sub_question`, no plan tree, nothing between
"classify budget" and "collect evidence."

Meanwhile the *hypothesis* side of the house has real structure —
`tools/hypothesis_generator.py` generates betting theses, `create_hypothesis`
(tools/hypothesis.py) stores them with `model_config`, `edge_threshold`,
`devig_method` — but the intake schema (`schema.py` hypotheses table, sport/market/
threshold fields) is betting-shaped. A Bitcoin research program cannot be expressed
in it without violence to the fields.

**Gap verdict [VERIFIED]: decomposition does not exist. Intake exists twice, once
per half of the system, and neither half can represent the other.**

### Design: ResearchProgram — the missing first-class object

One new concept, landed incrementally:

```
ResearchProgram
  id, root_query, domain, created_at, status(active|suspended|concluded)
  questions: [ResearchQuestion]        # tree, depth ≤ 3
      text, kind (descriptive | causal | predictive), priority
      evidence_requirements: {min_source_class, min_independent_sources,
                              quant_required: bool}
      horizon: {claim_date, resolve_date}     # see §2
      children: [...]
      status: open | answered | falsified | unresolvable
  artifacts: [ArtifactRef]             # see §3
  lifecycle_link: hypothesis_id | None # optional bridge to existing gate (§2)
```

Landing order (each step works alone):

1. **D0 — Decomposer as an orchestrator step.** New `_step_decompose` between
   domain assignment and source enumeration: model call producing the
   `questions[]` tree from the root query, validated against a JSON schema.
   Stored in a new `research_questions` table. Nothing downstream changes yet —
   today's single-query evidence collection simply runs per leaf question instead
   of per root. Pure refactor of loop granularity; sealed sessions unchanged.
2. **D1 — Evidence requirements as gates, not prose.** Each question carries
   `min_source_class` etc., consumed by the EXISTING `_clamp_confidence`
   machinery (orchestrator.py:705, agp/thresholds.py MAX_CONFIDENCE_BY_SOURCE):
   a leaf question whose evidence never reached its required class cannot seal
   above a floor. This is instance 4's provenance-assigned source_class plugged
   into per-question requirements — no new enforcement concept needed, which is
   the point: reuse the moat.
3. **D2 — Program-level synthesis.** A final step synthesizes ACROSS sealed leaf
   sessions, with cross-question contradiction checks (reusing `add_contradiction`,
   agp/__init__.py:271). The root query's answer cites child session seals, so the
   answer inherits verifiable provenance rather than asserting it.

Falsifier for the gap claim: produce any orchestrator code path that emits more than
one AGP session for one user query, structured as sub-questions. It does not exist
today.

---

## 2. THE HORIZON PROBLEM — central, and solvable

### Why it is hard [VERIFIED]

Sports calibration works because every claim gets a resolution event within hours:
`resolve_from_game_results` (tools/backtest.py:835) scores predictions against
`game_results`; paper trades resolve against outcomes; Brier/IC/CLV all compute
because both prediction AND outcome exist. Instance 3 verified the promotion-gate
arithmetic depends entirely on resolved signal counts (n≈3,900 for a +3pt edge).

A 10-year Bitcoin price target resolves in 10 years. Until then:
- Brier score is undefined (no outcome).
- The Šidák denominator, p-values, IC — all undefined.
- If the system stamps the claim VERIFIED/CORROBORATED anyway, the seal becomes
  "hashed opinion" — exactly instance 4's finding that the model being governed
  labels its own evidence (orchestrator.py:1106, :1171 offer the model only
  SECONDARY/INFERRED self-labels). A long-horizon claim sealed today at 0.75 is
  indistinguishable from one earned against 4,000 resolved events. That
  indistinguishability is the actual defect.

### The bridge: dated sub-claims + instrumented inheritance

The general principle: **a claim's confidence may only be earned by resolving
things, and the only things that resolve are dated, binary-or-quantile sub-claims
with resolve_dates inside the scoring window.** So decompose forward:

For "BTC price target, 10 years":
1. **Mechanism decomposition.** The model must state the causal chain its target
   rests on (e.g., adoption S-curve, supply schedule, rate environment, flow
   assumptions) — each mechanism becomes a child question.
2. **Leading-indicator extraction.** Each mechanism yields dated observable
   sub-claims: "spot-ETF net inflows exceed $X cumulatively by 2027-Q2",
   "realized-vol regime stays under Y through 2026", "N corporate treasuries hold
   > Z BTC by date D". These are the analog of individual bets: short-horizon,
   falsifiable, each with its own prior probability stated AT ISSUE TIME.
3. **Quantitative commitment.** The target itself is stored as a calibrated
   distribution (quantiles P10/P50/P90 per horizon year), not a number — so when
   partial outcomes arrive it scores continuously, not pass/fail at year 10.
4. **Scored accumulation.** Each sub-claim enters the EXISTING lifecycle:
   draft → backtesting (against whatever historical analogue exists — e.g., did
   this indicator's past movement predict past BTC regimes?) → paper_trading
   (live tracking against reality, resolved on its resolve_date) → and its
   resolution feeds back.

**Paper trading generalises directly and needs almost nothing:** a paper trade is
just "prediction recorded now + outcome recorded later + scoring function." The
existing `paper_trades` table semantics (record signal, resolve later, compute CLV/
Brier) apply to any binary/quantile claim if we generalize three columns
(`sport`, `market_type`, odds-specific fields) behind a `claim_type` discriminator.
That is the single highest-leverage schema change in this whole document.

5. **Inheritance rule (the new epistemic primitive).** The parent claim's ceiling =
   f(resolved descendant track record), computed by the SAME calibration machinery
   used for sports: if the system's sub-claims about a domain have Brier 0.09 over
   n=40 resolved indicators, the 10-year target earns a tier consistent with that
   track record; if n=3, it stays SPECULATIVE regardless of how confident the model
   sounds. Concretely:

```
parent_tier_ceiling = tier_from_score(brier_calibration_bonus(track_record))
                      capped by best descendant source_class (instance 4's rules)
unresolved_fraction penalty: a target with 0 resolved descendants cannot
                      exceed TIER_SPECULATIVE (0.30–0.55 band) — ever.
```

This makes "the seal decays into hashed opinion" structurally impossible: an
unresolved claim is *not allowed* to look like a resolved one, because its tier
carries the count of things it has actually survived.

6. **Decay, honestly.** Sub-claims that pass their resolve_date unresolved get
   marked `stale`, and staleness penalizes the parent — mirroring how the existing
   cascade demotes on underperformance (tests/test_cascade_demotion.py exists).

### Landing order

- H0: generalize paper_trades behind `claim_type` (sports values unchanged; run the
  existing suite green — characterization tests first per mandate §5).
- H1: quantile-claim storage + continuous scoring (Brier on quantiles = pinball
  loss; add alongside, not instead of, binary Brier).
- H2: decomposer prompt section emitting leading-indicator sub-claims with
  resolve_dates; auto-register them as paper-tradable claims.
- H3: inheritance-rule function + its tests; surface "resolved descendants: N"
  next to every long-horizon conclusion in API responses.

Falsifiers: (a) show a long-horizon claim reaching CORROBORATED+ with zero resolved
descendants under the H3 rule — should be impossible by construction; (b) show the
generalized paper-trade path failing any existing sports test after H0 — would prove
the generalization broke the proving ground.

---

## 3. QUANTITATIVE ARTIFACTS — the product gap

### What exists [VERIFIED]

- Real math libraries: devig (three methods), ev.py, kelly.py, math_utils,
  kl_divergence, granger_causality, simulation/sim, bankroll_sim. All
  sports-framed but much of it is generic arithmetic.
- `tools/local_compute.py`: explicitly built so callers "run math locally instead of
  burning Claude tokens" — but only wraps the existing betting functions.
- Orchestrator tool dispatch (`_execute_tool`, orchestrator.py:1261): web_search,
  claude_code, odds tools, calculate_ev. **No code-execution tool of any kind.**
  No python REPL, no sandbox, no subprocess runner in the tool list.
- Synthesis (`_step_synthesize`, :1584) produces a SessionSummary: prose + numbers
  embedded in text. No file artifacts. Nothing like a returned spreadsheet exists.

**Gap verdict [VERIFIED]: the system cannot execute arbitrary code, cannot build a
model artifact, cannot return anything but prose+JSON. The owner's core ask —
spreadsheets and the math behind them — has zero supporting surface today.**

Falsifier: name any tool in `_execute_tool`'s dispatch that returns a downloadable
artifact or executes model-authored code. None exists.

### Design: the Compute Sandbox + Artifact Store

1. **S0 — sandboxed execution tool.** One new tool `run_python(code, inputs)`:
   - Execution: a hardened subprocess (`python -I`, no network, rlimits/seccomp on
     Linux, sandbox-exec profile on macOS; CPU+wall clock caps; tempdir workspace
     destroyed after). Not clever — a container if Docker exists, restricted
     subprocess otherwise. The threat model is prompt-injected model code reading
     env secrets; deny network + env scrubbing closes the realistic attack.
   - Determinism: pinned deps (numpy/pandas/matplotlib already implied by the
     repo's analysis modules — check requirements.txt at landing), seeded RNG,
     captured stdout + return value + generated files.
   - Every execution is logged as AGP Evidence with `source_class=SIGNAL` initially;
   the code AND its full output are sealed — reproducibility becomes part of the
   seal payload. This matters: **an answer with runnable, sealed math attached
   is re-runnable by anyone forever** — the artifact hashes make the *math*
   checkable (see SEAL_CONTRACT.md for exactly what the seal does and does not
   cover). Note the boundary: sealing makes the computation tamper-evidently
   reproducible; it does not verify that the model's prose conclusions are
   true, and it never did. The "earned against reality" standard still
   requires outcomes to resolve; a seal alone does not substitute.
2. **S1 — Artifact type.** `{id, kind: csv|xlsx|json|png|ipynb, sha256, code_ref,
   data_refs[], session_seal}`. Files land in a content-addressed store; API gains
   `GET /artifact/{id}`. Session summaries reference artifacts by id; the seal
   covers the hashes, so tampering breaks verification (reusing keyed-HMAC seal
   from instance 4's branch, commit eb6151b lineage).
3. **S2 — Spreadsheet emission.** For the flagship query shape ("models +
   spreadsheets"), a standard workbook layout: Assumptions sheet (every input,
   sourced), Model sheet (formulas live in xlsx cells where feasible so the owner
   can audit/torture them in Excel), Data sheet (raw pulls with URLs+dates),
   Scenarios sheet. Live formulas matter: a dead CSV of results is prose in
   disguise; an auditable formula chain IS "the math behind it."
4. **S3 — Model registry tie-in.** Long-lived quantitative models (e.g., the BTC
   valuation model) become themselves falsifiable entities registered like
   hypotheses — their predictions enter the §2 pipeline. The model that produces
   the answer is thus also under calibration. This closes the last self-report
   loop: today's models are ephemeral prompt outputs.

Landing: S0 standalone (biggest unlock per line of code), then S1+S2 together,
then S3.

---

## 4. DOMAIN-AGNOSTIC EVIDENCE ACQUISITION

### What exists [VERIFIED]

- `tools/search.py`: unified web search — SearXNG (self-hosted, free) primary,
  Brave API fallback. Domain-general ALREADY. This is quietly the most
  domain-general module in tools/.
- `tools/searxng.py`, `brave_search.py` — same.
- `tools/news_ingestion.py`, `news_loop.py`, `news_impact.py` — news pipeline,
  sports-flavored consumers but generic ingestion.
- Fourteen sports sources (odds_api_io, dk/fanduel/betmgm/fanatics/tci scrapers...)
  — per the scope correction, LOW priority; replaceable commodity.
- Orchestrator evidence collection prompts (orchestrator.py:1103, :1171) ask the
  model to fetch via web_search and label results SECONDARY/INFERRED.

### What a general research system needs, ordered by unlock

1. **E0 — URL-grounding of citations (closes instance 4's C1 hole generally).**
   `_response_cites_urls` (orchestrator.py:731) is substring matching; any
   fabricated URL buys +0.20 ceiling. The fix is domain-general and mechanical:
   dedupe claimed URLs against the URLs actually returned by `_run_searches_parallel`
   during the session; unfetched URLs earn INFERRED, not SECONDARY. Small diff,
   enormous honesty gain, benefits every domain including sports.
2. **E1 — Fetcher + reader tool.** `fetch_url(url) -> extracted_text` (httpx +
   readability/trafilatura-style extraction, robots-respecting, cached,
   content-hash stored as evidence). Today the model sees only search snippets;
   papers, filings, and docs require fetching the actual document. Add
   `source_kind` taxonomy: web | paper(arxiv/crossref DOI) | filing(SEC EDGAR —
   free, structured, stable APIs) | dataset | gov_stat (FRED, BLS, etc.). FRED
   and EDGAR alone cover most financial-domain research at $0 — aligned with the
   local-hardware ethos.
   [MERGE-STATE NOTE, VERIFIED: instance 4's keyed HMAC seal is on their branch,
   not merged here — this repo still has the unkeyed sha256 seal at
   agp/__init__.py:352. Fetch-record provenance sealing depends on that merge.]
3. **E2 — Provenance survives by construction, not declaration.** With instance 4
   making source_class provenance-assigned: the rule that makes it work at scale
   is **provenance is a property of the FETCH RECORD, not the model's label.**
   Every Evidence item carries `(url, fetch_hash, fetch_timestamp, extractor)`;
   source_class derives from the source_kind of the matched fetch record;
   evidence items with NO matching fetch record cannot exceed INFERRED. The
   keyed seal then covers the fetch records, making the whole chain auditable.
   This generalizes the sports case perfectly: a Pinnacle quote from odds_api_io
   is just a fetch record from a PRIMARY-grade source.
4. **E3 — Structured API adapters.** Same interface as `_execute_tool`'s odds
   tools: thin, per-provider, returning normalized dicts with fetch metadata.
   Sports proved the pattern; replicate for finance/data domains as demand
   appears. Do NOT build these speculatively — E0-E2 unlock most queries.

Falsifier for E2: construct a sealed session containing an Evidence item whose url
was never fetched by any tool call in that session; under the design it must seal
at INFERRED-ceiling or be refused.

---

## 5. WHAT ALREADY GENERALISES — credit where due

These need NO change for the vision; saying so precisely matters as much as the gaps:

1. **AGP core mechanics (agp/__init__.py)** [VERIFIED, matches instance 4's
   clean-bill]. Step sequencing, evidence filtering, seal-refusal gates,
   contradictions, keyed HMAC sealing (post-tier3). Zero domain vocabulary in the
   protocol itself — Domain enum is coarse but not betting-bound. Genuinely
   portable as-is.
2. **Confidence tiers + source-class ceilings (agp/thresholds.py)** [VERIFIED].
   The tier boundaries and ceilings are domain-neutral statements about evidence
   authority. The DB CHECK constraint enforcement (memory.py:40-49) likewise.
   Only the ceiling VALUES might someday want per-domain tuning — config, not
   redesign.
3. **The lifecycle abstraction draft→backtesting→paper_trading→live→retired**
   [INFERRED→design in §2]. The stage names are betting dialect, but the
   semantics — test on history, then track prospectively against reality, then
   act, then retire — is THE right abstraction for any falsifiable claim. With
   the H0 `claim_type` generalization it is fully domain-general. This is the
   strongest structural idea in the codebase.
4. **Promotion-gate CONCEPT** [VERIFIED as concept; implementation is
   betting-parameterized]. Šidák correction + Brier + IC + capital gates is
   domain-general statistics. The gate CONSTANTS (edge_threshold, min CLV) are
   betting-specific and belong in per-domain gate profiles — a config split,
   not a rewrite. Note instance 3's verified findings (inline literal at
   hypothesis.py:1415 killing true edges; adaptive-p-as-alpha confusion at :1172)
   must be fixed before ANY domain trusts the gate — the gate is currently
   unreachable for everyone, equally.
5. **Seal discipline** [VERIFIED, with a merge-state correction]. Canonical-JSON
   + tamper tests (test_agp_seal.py) are domain-general and already on master.
   CORRECTION to my own first draft: instance 4's keyed HMAC upgrade (commit
   eb6151b) lives on audit/tier3-epistemics and is NOT merged here — this
   branch's agp/__init__.py:352 still computes plain sha256, forgeable by
   anyone with DB write. The artifact-hashing design (§3-S1) must land AFTER
   the keyed seal merges; pinned by test_tier7_deepresearch.py's seal test,
   which self-falsifies when the HMAC lands.
6. **Demotion on underperformance** [VERIFIED — cascade demotion exists with
   tests]. Generalizes as-is once claims flow through the generalized lifecycle.
7. **Search stack (search.py/searxng.py)** [VERIFIED]. Already domain-general;
   the vision's evidence floor is mostly present.
8. **Provider/llocal-model routing seam (config/providers.yaml, inference.py)**
   [VERIFIED as a documented seam, ROADMAP Tier 5]. Hardware-scaling requirement
   from the scope correction lands here; the ProviderRouter work is owned by
   tier 5 and is prerequisite infrastructure for deep research at electricity
   cost, but is not a research-capability gap per se.

### What does NOT generalise and must not pretend to

- Hypothesis intake schema (sport/market_type columns) — §1/D0 bypasses, later
  migrates.
- All odds/scrape tooling — correctly deprioritized by the scope correction.
- `edge_confidence.py` scoring heuristics (book-count based) — betting-native;
  leave scoped to betting.

---

## ORDERED BY UNLOCK

1. **§2/H0-H1 — generalize paper_trades + quantile claims.** Unlocks measured
   calibration for every domain; converts the moat from betting-only to universal.
   Without it nothing else matters, because unscored conclusions are the failure
   mode instance 4 identified.
2. **§3/S0 — compute sandbox + sealed artifacts.** Unlocks the actual deliverable
   the owner asked for (models/spreadsheets/math) and makes answers mechanically
   checkable.
3. **§4/E0 — citation grounding vs. actual fetches.** Smallest diff, largest
   honesty delta; fixes the confirmed anti-moat for all domains.
4. **§1/D0-D1 — decomposer + per-question evidence requirements.** Turns single
   queries into research programs; prerequisite for §2's mechanism decomposition
   on complex queries.
5. **§4/E1-E2 — fetcher, source-kind taxonomy, fetch-record provenance.**
   Papers/filings/gov-data reach.
6. **§2/H3 + §3/S3 — inheritance rule and model registry.** The capstone: parents
   inherit only scored descendants; models themselves go under calibration.

Each item is independently landable on the working system behind flags, in the
order above, with the sports proving ground staying green at every step — which is
exactly what the proving ground is FOR.

— Instance 7
