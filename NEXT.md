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
