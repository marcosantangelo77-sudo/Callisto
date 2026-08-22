# AUDIT MANDATE — total scrutiny pass

**Audience:** Ox Alpha and every agent it spawns.
**Status:** standing orders. Supersedes nothing in `ROADMAP.md` — extends it.
**Read `ROADMAP.md` first.** This document says *how to work*; that one says
*what was already found*. Do not re-derive its findings; build past them.

---

## 0. THE MANDATE

Callisto is ~135,000 lines written across several model generations, most of it
by Opus 4.6, last touched 2026-04-23. It was built for one purpose: **to own the
means of production.** With hardware and electricity you can run intelligence
for free and point it at a goal. Sports betting is the proving ground — chosen
because ground truth arrives in hours, which is the only reason calibration can
be *measured* at all — not the point.

The owner's instruction, verbatim in intent: *everything needs to be questioned.*
Not audited for bugs. **Questioned.** For every unit of code: what is it for, does
it achieve that, and what would a better version look like? Spawn as many agents
as it takes. There is no budget ceiling and no complexity ceiling on what you may
*propose*.

Four standing rules, in tension on purpose — hold all four:

1. **Nothing is above scrutiny.** Not the AGP protocol, not the promotion gate,
   not the architecture, not prior audit conclusions, not this document. If the
   foundational abstraction is wrong, say so.
2. **No complexity ceiling on proposals.** If a rewrite of the backtest kernel,
   a real event bus, or a different memory substrate would make the system
   materially better, propose it fully — cost, migration, risk. Do not
   pre-censor an idea for being ambitious.
3. **Improve; do not deconstruct.** Every proposal must land incrementally, with
   the system working at every intermediate step. No big-bang rewrite. If a
   change cannot be staged behind a flag or landed in reviewable pieces, it is
   not ready, however good the idea.
4. **Quarantine, never delete.** Stale code goes to `attic/` with a note on what
   it did, why it was retired, and how to restore it. The owner is explicit:
   *"I don't really wanna remove things."* Dead weight should stop being
   maintained, not stop existing. Deleting is reserved for code proven to be
   both unreachable and duplicated elsewhere — and even then, prefer the attic.

Rules 2 and 3 resolve as: **propose freely, land incrementally.**

---

## 1. GROUND TRUTH — measured, not assumed

```
283 Python files, 135,446 lines, 951 test functions across 93 test files

  tools/                   104 files    85,509 lines   <- the mass
  ./ (root)                 18 files     9,228 lines
  tests/                    93 files    22,478 lines
  agp/                       2 files       453 lines   <- the protocol core
  hermes-function-calling/   7 files     1,034 lines

Largest single modules:
  tools/autonomous.py      7,955     api.py                4,685
  tools/backtest.py        4,211     tools/data_collector  3,156
  tools/hypothesis.py      2,848     tools/schema.py       1,981
  tools/line_monitor.py    1,958     orchestrator.py       1,896
```

**The organizing fact of this audit:**

> **33 modules over 500 lines — 57,129 lines, ~42% of the codebase — have no
> test file named after them.**

That set includes `bet_executor.py` (1,253), `kelly.py` (895), `clv_tracker.py`
(943), `backtest.py` (4,211), `autonomous.py` (7,955), `self_repair.py` (1,031),
`embeddings.py` (795), `orchestrator.py` (1,896) and `api.py` (4,685).

**The entire path from decision to real money — sizing, execution, and the
metric that guards promotion — is untested.** Meanwhile 22,478 lines of tests
exist and pass. They are testing something. Establishing *what* is a first-class
objective of this audit.

Caveat, and treat it as falsifiable: "no `test_X.py`" is not proof of zero
coverage — a module may be exercised indirectly. **Measure real per-module
coverage before concluding anything.** If the 42% figure is wrong, correct it
loudly; it is load-bearing for everything below.

### What you cannot see

- **The database.** `*.db`, `*.sqlite`, `memory/` are gitignored and live on a
  workstation you cannot reach. Every table the code reads is empty here:
  `hypotheses`, `backtest_events`, `game_results`, `player_stats`,
  `game_contexts`, `bets`, `paper_trades`, `bankroll`, `embeddings`,
  `ev_opportunities`.
- **Any local model.** Nothing runs end to end.

Therefore every finding carries a tag: **VERIFIED** (proven against code you ran
or read in full) or **INFERRED** (reasoned from reading). Anything requiring real
data or a live model is a **PROPOSAL**, never an unverified edit. Mislabelling an
INFERRED finding as VERIFIED is the most serious process failure available to you
— this codebase already contains a self-repair engine that records confidence-0.8
"learnings" about fixes that did nothing. Do not become that.

---

## 2. THE INTERROGATION PROTOCOL

Apply to every module, and to every non-trivial function inside it. This is the
core of the mandate — the owner asked specifically that once something is looked
at, its *objective* is established, then how it could be *better*, then what could
be *built on it*.

**Q1 — What is this for?**
State the purpose in one sentence *before* reading the docstring. Then read the
docstring. If they disagree, that is a finding: either the code drifted from its
stated intent or the intent was never true. Record both sentences.

**Q2 — Does it do that?**
Evidence, not reading comprehension. A test you wrote, an execution trace, a
proof. "It looks correct" is not an answer.

**Q3 — What does it assume that nothing checks?**
Units, timezones, sign conventions, devigged-vs-raw odds, fraction-vs-rate,
American-vs-decimal, UTC-vs-local, closed-vs-open intervals, None-vs-zero.
This codebase has already produced one confirmed unit bug guarding real money
(`MIN_CLV_RATE`). Assume there are more. Unit errors are the highest-frequency
defect class here and they are invisible to a passing test suite.

**Q4 — What happens when it is wrong?**
Classify the blast radius:
  - **LOUD** — raises, fails a test, returns an error. Cheap.
  - **SILENT** — returns a plausible wrong number that flows into a decision.
    **This is the dangerous class.** In a system that sizes bets, a silently
    wrong number costs money while every test stays green.
  - **ARMING** — makes a dormant dangerous path live.
Silent-wrong findings outrank loud-crash findings, always.

**Q5 — Is it reachable?**
Dead code, unreachable branches, config keys nothing reads, handlers never
registered, tiers never assigned. `ROADMAP.md` already documents VERIFIED-tier
being unreachable because PRIMARY is never assigned, and self-repair writing
config keys nothing consumes. Find the rest. Unreachable code that *looks*
live is worse than absent code, because it manufactures false confidence.

**Q6 — How would you build this today?**
No complexity ceiling. Ignore what exists. Describe the version you would write
now, with 2026 tooling and models, and say concretely what it buys — latency,
correctness, cost, clarity, capability. Then, separately, the migration path
that gets there incrementally. If the honest answer is "identically," say so;
that is a strong signal the code is good and should be recorded as such.

**Q7 — What would have to be true for this to be retired?**
Name the condition. If the condition already holds, propose the attic move with
a restore note. If nothing could ever retire it, that tells you it is load-bearing
and deserves tests before anything else touches it.

**Q8 — What would falsify your conclusion?**
Every finding states the evidence that would prove it wrong. A finding with no
falsifier is an opinion. An agent that cannot name one has not finished thinking.

An agent is explicitly permitted — and expected — to conclude **"this is correct,
well-built, and should not change."** A sweep that finds a problem everywhere it
looks is not being rigorous; it is confabulating. Report clean modules as clean.

---

## 3. HOW TO ORGANISE THE SWEEP

Spawn as many agents as the work needs. Two hard constraints and nothing else:

- **Exclusive file ownership.** No two agents may edit the same file. There is no
  locking; a silent overwrite is invisible and unrecoverable.
- **Branch per agent, off `master`.** Note: Callisto's default branch is
  `master`, not `main`. Commit in small, complete, reviewable units as you go —
  never one commit at the end. The audit branch `audit/2026-08-roadmap`
  (`e63ac00`) is pushed and has an open PR; build from there.

Recommended shape, not binding:

- **Wave 1 — cartography.** Before changing anything: real per-module coverage,
  the call graph, the import graph, dead-code detection, and a dependency map of
  which modules touch money, which touch the gate, and which touch neither.
  Publish it. Everything downstream is prioritised from this map.
- **Wave 2 — depth passes** on the priority tiers in §4, 3–6 agents at a time.
  Depth beats breadth. One agent that fully understands `backtest.py` is worth
  more than ten that skim.
- **Wave 3 — synthesis.** Cross-cutting concerns no single-module agent can see:
  unit consistency across boundaries, error-handling consistency, concurrency and
  the aiosqlite/WAL story, the trust-propagation graph.
- **Standing: an adversarial verifier.** Its only job is to re-derive claims
  against the repo, never to read reports credulously. It must re-run at least
  one measurement per report it accepts, and it is measured on findings it
  *rejects*, not findings it passes. Reports are evidence about agents, not
  evidence about the code.

---

## 4. PRIORITY ORDER

Derived from blast radius, not line count.

**TIER 0 — the money path. Untested, and one of them is live-adjacent.**
`bet_executor.py` (1,253) · `kelly.py` (895) · `clv_tracker.py` (943) ·
bankroll and staking wherever they live.
`ROADMAP.md` §0 flags the live-execution path as structurally dead and warns
that the naive one-line fix *arms untested sizing code*. **Do not arm it.**
The correct sequence is: characterization tests → unit audit → arithmetic proof
→ paper-only integration → and only then, with the owner's explicit consent, a
discussion about live. Write the tests that make the path *safe to arm later*.
Treat the whole tier as if it were live already.

**TIER 1 — the unattended loop.**
`autonomous.py` (7,955) · `self_repair.py` (1,031) · `orchestrator.py` (1,896).
This is what runs when nobody is watching. `self_repair.py` responds to "nothing
is passing the quality bar" by lowering the quality bar — three separate paths,
two of which write config keys nothing reads while recording confidence-0.8
successes. The governing principle to establish and enforce: **a maintenance
routine must never be permitted to weaken a gate.** Design the enforcement, don't
just patch the three call sites. Also: `autonomous.py` at 7,955 lines is a
monolith running unattended — Q6 applies hard here.

**TIER 2 — the gate.**
`hypothesis.py` (2,848) · `backtest.py` (4,211) · `hypothesis_generator.py` (1,684).
3,192 rejections, zero promotions. `ROADMAP.md` §3.2 establishes the Šidák
denominator is *lifetime* N, forcing α≈9e-05 — a ratchet that got permanently
harder with every hypothesis ever generated. Verify that arithmetic independently.
Then answer the design question behind it: what *should* the correction be scoped
to — per family, per time window, per sport? Multiple-comparison correction over
a lifetime population is a modelling decision, not a constant, and it deserves a
first-principles answer rather than a tuned number.

**TIER 3 — the epistemics. This is the moat; treat it as such.**
`agp/` (453) · confidence calibration · the seal · `knowledge_wiki.py` (1,350) ·
`hermes_memory.py`.
Prior-art research found nothing importable that does enforced confidence tiers,
promotion-gate architecture, or seal discipline. That is the differentiated asset.
It is also where `ROADMAP.md` reports the softest failures: the seal is an unkeyed
hash forgeable by anyone with DB write, the Sentinel vetoes nothing, VERIFIED tier
is unreachable, and the wiki/hermes loop is a trust escalator that promotes
yesterday's unverified INFERRED into today's 0.75-ceiling prior. **A moat made of
self-reported inputs gating self-reported confidence is not a moat.** The central
question for this tier: what would it take for a confidence tier to be *earned
against reality* rather than asserted? Answer that and the system becomes
genuinely hard to replicate. Only 453 lines carry the entire protocol — that ratio
is either elegance or under-specification, and you should determine which.

**TIER 4 — the data plane.**
`data_collector.py` (3,156) · `line_monitor.py` (1,958) · `odds_api_io.py` (1,518)
· `dk_scraper.py` (1,254) · `tci_scraper.py` (811) · 14 sources.
`ROADMAP.md` puts the scraper stack on the kill list in favour of The Odds API.
Pressure-test that: what breaks, what is lost, what does it cost, and what data
does the paid $800 API history hold that no vendor sells back? The scrapers may
be commodity; the accumulated *history* is not, and the two must not be conflated
when deciding what to retire.

**TIER 5 — serving and inference.**
`api.py` (4,685) · `inference.py` (849) · `hermes-function-calling/` (1,034) ·
`config/providers.yaml`.
Implement the ProviderRouter the placeholder config describes. Native tool calling
has moved into the models and the serving layer since Hermes was forked; vendor
~200 lines and swap the validator guts for `jsonschema` per the roadmap — but
verify that recommendation rather than inheriting it. Then the tiered-routing
question: which task classes genuinely need a frontier model, and which are grind
work a resident 27B does at zero marginal cost? Get that split wrong in either
direction and the system is either poor or expensive.

**TIER 6 — the test suite itself.**
93 files, 22,478 lines, 951 functions, passing — while 42% of the codebase has no
named test. Determine what they actually pin: real behaviour, or implementation
detail. `ROADMAP.md` reports the Šidák guard test computes the formula inside
itself, which tests arithmetic rather than the system. **A suite that locks in
wrong behaviour is worse than no suite**, because it converts every future fix
into a red test and trains you to change the test. Report the ratio of
behaviour-pinning to tautological tests, with examples of each.

---

## 5. NON-NEGOTIABLE

1. **Characterization tests before any numerical change.** Backtest windows, edge
   calculations, embedding normalisation, CLV, Brier, IC, Kelly fractions — pin
   current outputs on fixed inputs *first*, so drift is visible. Without the
   database this is often impossible; then the change is a PROPOSAL. Say so
   plainly rather than editing on faith.
2. **Never arm the live-execution path.** Not as a fix, not as a test, not as a
   demonstration. Making dormant money-moving code live is the owner's decision
   and requires explicit consent.
3. **No secrets, ever.** No API keys, credentials, DB contents, or personal data
   in commits, logs, comments, or reports.
4. **Never weaken a gate to make a metric look better** — and treat any code that
   does as a defect regardless of where it lives.
5. **Do not trust prior reports, including `ROADMAP.md` and this file.** Both were
   written without database access. Verify independently; correct loudly.

---

## 6. DELIVERABLE

**Per module** — a short dossier: the Q1 one-sentence purpose, VERIFIED/INFERRED
findings with blast-radius class, the Q6 "how I'd build it today" answer, the Q7
retirement condition, and the Q8 falsifier. Terse is fine. Empty is not.

**Overall** — update `ROADMAP.md` in place. It already carries the audit's spine;
extend it rather than starting a parallel document. It must end able to answer:

- **THE GOLD** — what is genuinely hard to replicate, with the evidence.
- **THE BACK BURNER** — what to stop maintaining, moved to `attic/`, each with a
  restore note. Not deleted.
- **THE MOAT, HONESTLY** — is the epistemic core real, or is it self-reported
  inputs gating self-reported confidence? If the latter, what makes it real?
- **THE PLAN** — ordered, with the first thing to do at the workstation with the
  database in front of you, and the five queries from §3.1 first.
- **THE AMBITIOUS COLUMN** — the Q6 answers worth acting on, each with an
  incremental migration path. This is where "no complexity ceiling" cashes out.

Commit and push everything to `marcosantangelo77-sudo`. SSH auth is configured
and silent — `git push` just works.

---

## 7. THE QUESTION BEHIND ALL OF IT

The owner has spent months on this and wants to know whether to spend more. Not
flattery — a verdict he can act on. Every finding should serve one of:

- **What here is real, and why can't it be downloaded?**
- **What is a worse version of something that already exists?**
- **What would this be if it were finished?**

Answer those three with evidence and the roadmap writes itself.
