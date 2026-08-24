# BUILD MANDATE — construction pass

**This supersedes AUDIT_MANDATE.md for this phase.** That document asked
"what is broken here." It did its job: 2,212 lines of findings, 259 tests,
five structural fixes, all merged to master. The diagnosis is done.

**This pass asks a different question: what should this become?**

The constraint inverts. The audit rewarded caution — measure before changing,
prove it broken before touching it, no complexity ceiling on *proposals* but
land nothing unverified. That was correct for a codebase nobody had opened in
four months with an unattended loop rewriting its own evidence.

That loop is closed now. The characterization tests exist precisely so you can
move faster than the audit was allowed to. **Build.**

---

## 1. WHAT CALLISTO IS FOR

Callisto is a **deep research agent orchestrator**. It is not a sports betting
system. Betting was the proving ground, chosen because ground truth arrives in
hours, which is the only reason confidence calibration can be *measured*.

The owner wants to sit at his workstation and ask it anything:

> *"Is Bitcoin a good buy right now? What's your price target at 1, 5, 10 years?"*
> *"Will the Bills win the Super Bowl?"*
> — and questions about history, materials science, quantum research, supply
> chains, security, anything.

And get back **models, data, graphs, and the math** — not a paragraph with a
citation. An answer whose reasoning can be re-run and checked.

Four properties define the target. Every design decision should serve them:

**1. Any question, any domain.** Edges exist in places nobody thinks to look;
that is what makes them edges. The system must not be structurally incapable of
looking anywhere. If a design only works for one domain, it is the wrong design.

**2. Any evidence it needs.** If answering requires SEC filings, FRED series,
arXiv preprints, a scraped index, a public API, or code the system writes and
runs itself — it should be able to get there. Acquisition is a capability, not
a fixed list of fourteen sports endpoints.

**3. Verifiable, not voluminous.** Not a data dump. Not prose. **Evidence a
human can check**: a model with its inputs, a chart with its series, a
computation with its code, a claim with the fetched bytes it rests on. If a
conclusion cannot be re-derived from what it ships, it is not finished.

**4. It knows how accurate it is.** This is the product. A conclusion that
states its own confidence, where that confidence is *earned* against resolved
outcomes rather than asserted, is the thing nobody else has. Everything that
makes a conclusion more honestly earned is high value; anything that lets the
system flatter itself is a defect, however convenient.

### Compute model

Local models do the volume — screening, extraction, classification, the grind
that would cost a fortune per call. A frontier model routes in for the small
number of hard judgments. That routing already exists (`ProviderRouter`,
`config/providers.yaml`): task_class → tier → endpoint, capability-matched,
with health, failover, concurrency limits and a cost ledger.

The frontier tier is provider-agnostic on purpose — Claude Code, OpenRouter,
Nous, anything OpenAI-compatible. **Never hardcode a provider or a model.**

Hardware must stay plug-and-play. Today one RTX 5060 Ti, 16 GB. Tomorrow 3090s,
5090s, DGX Spark. Adding compute is a config entry and must remain one.
Everything must still degrade to the single 16 GB box.

---

## 2. WHAT IS ALREADY TRUE — do not re-derive this

All merged to master (`cb950d0`). Read `ROADMAP.md`, `ARCHITECTURE_MAP.md`,
`COVERAGE_MAP.md`, `DOMAIN_GENERALITY.md`, `DEEP_RESEARCH.md`, and
`findings/instance*.md` before proposing anything.

**The good news, verified:**
- `agp/` has **zero** domain vocabulary. The `Domain` enum is FINANCIAL,
  TECHNICAL, SIGNAL, SYNTHESIS, GENERAL — sports is not even an option. The
  protocol core was built domain-general from the start.
- The lifecycle `draft → backtesting → paper_trading → live → retired` is a
  strong abstraction for **any falsifiable claim**.
- Kelly sizing is provably correct (derivation in `findings/instance2.md`).
- `clv_tracker`'s canonical writers are unit-consistent and devigged.
- The seal is now HMAC-keyed; provenance is now ledger-assigned; VERIFIED tier
  is reachable for the first time.
  [BOUNDARY, 2026-08-24: the seal attests process integrity and internal
  consistency of the sealed snapshot — not that its conclusions are true.
  Preregistration is built but NOT on the engine's live path. Authoritative
  contract: SEAL_CONTRACT.md.]

**The failure that was diagnosed** — do not reintroduce any part of it:
every loop start lowered edge thresholds, then rewrote historical
`signal_generated` to match ("unblocks stalled promotions"), flooding the
hypothesis pool, which inflated the Šidák denominator, which made the gate
mathematically unreachable, which self-repair read as "zero promotion" and
answered by lowering gates further. Eight distinct mechanisms, now guarded.

**The consequence that shapes this pass:** cheap hypothesis generation makes
correcting against a cumulative pool catastrophic. At N=3,192, α=9e-05. At a
million hypotheses, α=5e-08 — nothing could ever promote. **The system must get
better as the models get better, not worse.** Correct within coherent families,
not across the lifetime pool.

---

## 3. THE BUILD QUEUE

Ordered by capability unlocked. Each lands independently; sports must stay green
throughout.

1. **OutcomeResolver** — the lifecycle's outcomes are literally `won/lost/push`
   read off `paper_trades.home_team`. Abstract it so any resolvable claim can
   enter the lifecycle. "Paper trading" generalises to preregistered
   forward-testing; "live" to a deployed conclusion. **This is the single
   highest-value change in the repo** — it is what turns a betting engine into
   a research engine.

2. **Compute sandbox + artifact store** — there is no code-execution surface at
   all today. Sandboxed `run_python` whose code *and output* are sealed as
   evidence; content-addressed artifacts; charts and spreadsheets as
   first-class outputs. This is property 3: verifiable, not voluminous.

3. **ToolRegistry / DomainPlugin** — `orchestrator.py:1179` hands every session
   21 betting tools regardless of domain. Tools become domain-scoped and
   registered, so a Bitcoin question gets financial tools and a materials
   question gets literature tools.

4. **Citation grounding** — dedupe citations against actual fetched results.
   Small diff, closes the last self-labelling hole.

5. **ResearchProgram decomposer** — nothing turns a question into sub-questions
   with their own evidence requirements. A question tree, each leaf sealed by
   the existing AGP machinery.

6. **The inheritance rule** — a parent claim's confidence ceiling is a function
   of its *resolved descendants'* track record; zero resolved descendants caps
   it at SPECULATIVE forever. This is how a ten-year target gets honest
   confidence today. It is the bridge from fast-feedback sports to everything
   else, and it is the most conceptually important item here.

7. **Fetcher + source taxonomy** — EDGAR, FRED, arXiv and friends at $0.
   Provenance is a property of the fetch record, never of the model's label.

8. **Schema seam** — `schema/core` + `plugins/sports`. `hypotheses.sport NOT
   NULL` welds the core lifecycle to betting in three places.

9. **Base-rate-relative thresholds** — the 0.45 hit-rate floor is reasonable at
   a 50% base rate and mass-rejects true positives anywhere base rates are 1-5%.
   This decides whether the engine works outside betting at all.

10. **The gate rebuild** — pool into families, invert the order so paper trading
    does the replication it is already free to do, gate on CLV rather than win
    rate. The CLV wiring is nearly free: the correct devigged column already
    exists, the gate just reads the wrong one.

---

## 4. RULES — fewer than the audit had

1. **Sports stays green.** Every change lands with the existing suite passing.
   The betting application is the regression test for the general engine.
2. **Characterization before numerical edits** — still holds. 259 tests exist
   to catch drift; use them, extend them.
3. **Never arm the live execution path.** Unchanged, non-negotiable.
4. **Nothing automated may weaken a gate.** Automated actors may raise a
   threshold, never lower one. Eight mechanisms violated this; do not add a ninth.
5. **Exclusive file ownership, branch per instance.** No two instances edit one
   file. It held perfectly across eight branches tonight — zero source conflicts.
6. **Land incrementally.** The system works at every intermediate step. No
   big-bang rewrite. Quarantine to `attic/` rather than deleting.
7. **Ship the artifact.** A design document is not a deliverable this pass.
   Working code with tests is.

## 5. THE STANDARD

Ambition is now the requirement rather than the risk. Where the audit asked you
to prove something broken before touching it, this pass asks you to build the
version that should exist — and then prove it works.

Ask of every design: **does this still make sense if the question is about
protein folding, or a supply chain, or a security vulnerability?** If not,
generalise it or drop it.
