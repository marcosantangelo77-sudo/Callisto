# NEXT — the queue after the build pass

Design decisions from the 2026-08-22 session, recorded so they survive the
conversation. Ordered. Read BUILD_MANDATE.md first for what Callisto is for.

---

## The core reframe: questions vs hypotheses

**A question is one-shot. A hypothesis recurs. The recurring one is where the
money is.**

- *"Will NVDA beat Q3 earnings?"* — a question. One shot, nothing accumulates.
- *"Companies whose inventory-to-sales ratio rises two quarters running miss
  earnings more often than the options market implies."* — a hypothesis.
  Testable across hundreds of past instances, deployable every earnings season,
  more trustworthy each quarter.

The lifecycle `draft → backtesting → paper_trading → live` only makes sense for
something that recurs — you cannot backtest a one-off. So the system's real job
is **converting questions into recurring hypotheses**, then running them forever.
Design intake with that as the goal.

---

## 1. RETRODICTION HARNESS — the accuracy test (highest value)

Ask questions whose answers are known now but were unknown at a past date, with
evidence acquisition hard-limited to sources published before that cutoff. Score
the conclusion against what actually happened.

Why it matters: thousands can be generated cheaply, in any domain, with feedback
in minutes rather than years. It tests the WHOLE pipeline — decomposition, source
selection, synthesis, calibration — not just one claim. It is the only honest way
to compare synthesis strategies: run reference-class-first against direct
reasoning over 500 retrodiction questions and compare Brier scores.

This answers "how do we confirm the research is accurate" without waiting.
Rank above the Polymarket adapter: Polymarket tests whether the ARCHITECTURE
generalises; retrodiction tests whether the RESEARCH IS ANY GOOD.

## 2. EDGE QUANTIFICATION as a lifecycle stage

The money-translation layer already exists and is provably correct (Kelly
derivation in findings/instance2.md). It just needs un-welding from
paper_trades.home_team — B1's OutcomeResolver does that.

Add a stage between sealed conclusion and position:

    calibrated probability → market-implied probability → edge
        → Kelly fraction → position, with implied price recorded at claim time

That last part is CLV, generalised. Identical math for a sports bet, a
Polymarket contract, an options position, a binary biotech event.

## 3. THE FOUR CHEAP WINS

- **Adversary role.** AGP has three roles; Sentinel vetoes but never attacks.
  Add a fourth agent whose only job is to falsify a conclusion before it seals,
  with its own scored track record. Nothing in the system is currently
  incentivised to be wrong.
- **Role-level model assignment.** Frontier for Architect (framing and
  decomposition — a bad decomposition dooms everything downstream) and Sentinel
  (catching a subtle flaw is harder than producing the conclusion). Local for
  Manager (search, collate, extract — 90% of token volume). The ProviderRouter
  already supports this; it needs task classes per role.
- **Reference-class first.** Before reasoning about specifics, find the base
  rate for the claim's class. Largest single accuracy gain in the forecasting
  literature. Also fixes the base-rate threshold problem from the other side.
- **Fermi decomposition with uncertainty propagation.** Break quantities into
  estimable factors, attach distributions, propagate. Makes the math honest
  rather than falsely precise, and auditable factor by factor.

## 4. SOURCE QUALITY HIERARCHY

Most AI deep research is search-engine-shaped — query the web, read summaries,
write prose. That is tiers 4-5. The differentiator is tiers 1-3.

1. **Primary structured data** — EDGAR, FRED, Census, ClinicalTrials.gov,
   patent DBs, exchange data. Authoritative, machine-readable, free, underused.
2. **Primary documents** — papers, filings, transcripts, regulatory dockets.
3. **Market prices** — aggregated belief of people with money at risk.
   Information-dense and underrated as evidence.
4. **Secondary analysis** — news, analyst notes. Good for pointers, weak as evidence.
5. **Model priors** — weakest.

The provenance ledger (agp/provenance.py) already makes this distinction
enforceable. Wire source-kind into it.

## 5. SYNTHESIS QUALITY

- **Structured extraction, not summarisation.** Numbers into a table with
  provenance per cell, then compute. Summarising destroys the numbers.
- **Source independence is checkable.** Three outlets rewriting one wire story
  is not corroboration. Track origin, not count.
- **Contradiction is the most informative moment.** Disagreement should raise a
  flag and lower a ceiling, never get averaged away.
- **Dissent logging.** When the adversary objects and is overruled, record it.
  When the claim resolves, check who was right — and calibrate the critic.

## 6. POLYMARKET AS SECOND DOMAIN

Fast resolution (minutes to days), dozens of genuinely different domains, real
prices (so CLV generalises directly), free public API. Breaks the tradeoff
between fast calibration feedback and domain generality.

It is a TEST, not an identity. If ToolRegistry / OutcomeResolver / schema seam
are right, adding it is a plugin. If it turns into a refactor, the seam is in
the wrong place — better to learn that with one new domain than five.

## 7. DEFERRED — real ideas, wrong time

- **Surprise-driven exploration** (prioritise where predictions were most wrong)
  — needs a corpus of RESOLVED claims. There are ~3,192 rejections and almost no
  resolutions. Revisit once retrodiction and Polymarket have produced a record.
- **Anomaly-first ingestion** — needs continuous multi-domain streams. Compute
  the owner does not have and does not want to buy.
- **Cross-domain analogy engine** — searching accumulated learnings for
  structural analogies across domains. Genuinely novel, needs several domains
  of accumulated learnings first. Keep the door open; build nothing now.

---

## THE DISCIPLINE

The eight gate-weakening mechanisms survived four months **because nobody ever
drove the system end to end and looked at the result.** Speculation did not
catch them. One honest end-to-end pass would have.

So: four cheap wins, retrodiction harness, one real question driven all the way
through — decomposition, evidence, model, artifact, sealed conclusion with a
checkable confidence. **Whatever breaks on that run is the real backlog**, and
it will be better ranked than any list written in advance, including this one.

Run early, run small, let reality prioritise.

---

## SCOPE GUARD — owner correction, 2026-08-22

**The system is not a work-automation tool.** It does not exist to produce lease
abstracts, estoppels, or proformas as deliverables. If the goal were a finished
DCF for its own sake, asking a state-of-the-art model directly would be better
and the owner would do that instead.

**The purpose is finding edges and alpha — things worth money.** Financial
artifacts are EVIDENCE in service of a claim, never the product:

  - not: "produce a DCF for this REIT"
  - but: "is this REIT mispriced, and here is the DCF, the implied cap rate
    against comps, the LTV and the DSCR that show why"

The artifact exists so the conclusion is checkable. That is the whole reason to
build modelling capability at all.

**Why EDGAR/XBRL still matters, stated correctly:** it is the evidence layer for
financial questions. Without it the system cannot answer them. That — not
deliverable generation, and not that the owner happens to be able to grade the
output — is the justification for building it.

**Test any proposed capability against this:** does it help find or verify an
edge? If it only produces a nicer document, it is out of scope, however useful
it might be to a person doing that work by hand.

---

## MULTI-MODEL ROLE ASSIGNMENT — first-class item

The goal is always local models running 24/7 at electricity cost. But the
ProviderRouter is deliberately provider-agnostic, and that unlocks something no
single model can do.

### Different models in different roles

The three AGP roles have genuinely different cognitive demands:

  Architect — frames the question, decides what evidence would settle it,
              chooses the decomposition. A bad decomposition dooms everything
              downstream and no execution quality recovers it. Highest payoff
              from capability.
  Manager   — runs searches, collates, extracts, normalises. ~90% of token
              volume. A resident 27B does this well at zero marginal cost.
  Sentinel  — adversarial review. Second-highest payoff: catching a subtle flaw
              is harder than producing the conclusion. A weak critic
              rubber-stamps.

Assign per role, not per system. Frontier for Architect and Sentinel, local for
Manager, is the default worth testing first.

### Cross-provider ensemble — the capability nothing else has

Claude as Architect, GPT as Manager, Grok as Sentinel. **When they disagree,
that disagreement is data, not noise.** A conclusion three models from different
training distributions converge on is meaningfully stronger than one confident
answer. Where they diverge, you have located genuine uncertainty rather than
model idiosyncrasy.

Feed disagreement into the confidence ceiling directly. No single model can
produce this signal at any capability level. This system is the only place
different models can argue with each other and have the outcome SCORED.

### Re-verification on model upgrade

When a stronger model becomes available, re-run previously sealed claims
through it. Three things fall out:

  1. **Claim audit** — a sealed conclusion the new model rejects is flagged for
     review. Cheap insurance against a weak model having sealed something wrong.
  2. **Model benchmark** — resolved claims are a held-out test set the owner
     OWNS. Scoring a new model against outcomes that were already settled
     measures it on real research, not on a public benchmark it may have
     trained on.
  3. **Ceiling revision** — if the model that sealed a claim is later shown
     weak on that claim class, the confidence ceiling for that class should
     drop retroactively.

This makes model strength a measurable, tracked property of the system rather
than a vendor claim.

### Why a frontier model does better INSIDE the harness

Six mechanisms, each addressing a known failure mode of standalone models:

  1. Forced decomposition — commits to sub-questions with evidence requirements
     before answering, so a failure is locatable.
  2. Provenance enforcement — cannot claim a source it did not fetch.
     Hallucinated citations are the most common research failure of every
     frontier model; here it is structurally impossible.
  3. Confidence ceilings tied to evidence class — cannot assert 90% on inferred
     evidence. Overconfidence becomes a check failure, not a tendency.
  4. An adversary that must try to break the conclusion before it seals.
  5. A scored track record, per claim class.
  6. Preregistration — commits to what would falsify before running.

**Caveat, kept honest:** a harness that constrains a strong model also costs
something. Frontier models are good at holistic synthesis, and a rigid pipeline
can produce a worse answer than free reasoning. The gain comes from the
VERIFICATION, not from the constraint. Design the harness to catch errors, not
to dictate how to think.

### Breadth, stated correctly

The goal is not "run every possible analysis" — forty analyses per question
buries the two that mattered. The goal is **find every piece of information
RELEVANT to the question and use it to prove the claim right or wrong.**
Decomposition determines which analyses bear on this question; the capability to
produce many model types sits available and is selected, not sprayed.

---

## SOURCE REGISTRY — the real capability (supersedes "build the finance domain")

**Correction of record.** An earlier brief justified building EDGAR/XBRL first
because the owner works in real estate finance and could personally grade a DCF.
That reasoning is wrong and the owner rejected it: he is not going to sit and
check DCFs, and manual verification was never the design.

**The correct framing:** local models are free to run 24/7, so inference cost
stops being the binding constraint and DATA ACCESS becomes it. The capability
worth building is not a finance module — it is a **source registry**, with EDGAR
as the first instance of the pattern.

Each source declares:
  - what kinds of question it can answer
  - its provenance class (tier 1-5, see §4 above)
  - its cost, rate limits, and terms
  - how its returns enter the provenance ledger

The decomposer then selects sources by relevance to the question at hand. That
is what "find every piece of information relevant to the question and use it to
prove the claim right or wrong" actually requires.

### Free, high-quality sources worth wiring

  SEC EDGAR / XBRL     every filing, tagged financial facts        tier 1
  FRED                 ~800k macro series                          tier 1
  BLS, Census          employment, CPI components, housing starts  tier 1
  Treasury FiscalData  rates, auctions, debt                       tier 1
  USPTO                patents                                     tier 1
  ClinicalTrials.gov   trial registrations, outcomes               tier 1
  PubMed, arXiv        papers                                      tier 2
  Kalshi, Polymarket   market-implied probabilities                tier 3
  GDELT                machine-coded global news events            tier 2
  Common Crawl         an actual web crawl, petabytes, free        tier 2-4
  Wikidata dumps       structured entity graph                     tier 2

Common Crawl and GDELT are the closest legitimate approach to "the whole
internet" — the crawl already exists and is given away, so there is no reason to
build one or to pay a vendor for access.

**Build the pattern, not the domain.** If adding FRED after EDGAR is more than a
config entry plus a thin adapter, the pattern is wrong and should be fixed
before a third source is added.

---

## RETRODICTION SCORING — score against MAGNITUDE, not just direction

**Owner's improvement, 2026-08-22.** The first design scored binary outcomes
("did NVDA beat consensus?"). That is a weak signal: companies beat roughly 75%
of the time, so a correct call carries little information.

Score against the market's implied distribution instead:

  - realised move vs the options-implied move at prediction time
  - IV crush, skew shift, term-structure response
  - for any event with a market: implied probability at claim time vs outcome

**This is CLV, generalised** — the same structure R5 is building into
tools/edge.py. The market's pre-event implied distribution is the benchmark; a
prediction either beat it or did not. Identical math for an earnings reaction, a
Kalshi contract, a biotech binary, and a sports line.

**Why it matters statistically:** continuous outcomes carry far more power per
observation than binary ones. A hundred magnitude observations is worth several
hundred yes/no observations. This attacks the sample-size wall directly — the
audit showed a true +3pt edge needs ~3,888 signals to clear the promotion gate
on win/loss alone. Scoring against magnitude collapses that requirement, because
each observation says how MUCH you were right, not merely whether.

**Consequence for the gate rebuild:** prefer continuous scoring wherever a
market price exists. Binary resolution is the fallback for claims with no
market, not the default.
