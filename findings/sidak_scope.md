# What is "the family"? — Scoping the Šidák correction in the promotion gate

Status: ARGUED BEFORE IMPLEMENTED (findings first, per mandate).
Scope: `tools/hypothesis.py:1185` (`fwer_n = ... max(lifetime_n, active_n, 1)`).
Author: ox-alpha instance, 2026-08-23, worktree `review/ox-alpha-0823`.

---

## 1. First principles

### 1.1 What a multiple-comparisons correction actually promises

Šidák/Bonferroni corrections do not make individual tests more true. They bound
a *joint* error rate over a set of tests:

> P(at least one false positive among the family) ≤ α_family.

That promise is only meaningful relative to **who consumes the joint claim**.
A correction is scoped correctly when the family is exactly the set of tests
whose results are consumed together in a single inference. Test anything
smaller and you under-correct; test anything larger and you pay power for
comparisons that were never jointly interpreted.

So the question "what should the denominator be?" reduces to: **which set of
hypothesis tests does Callisto consume as one body of evidence, in one
decision?**

### 1.2 Is the lifetime pool one family? No.

The current code treats every hypothesis ever backtested (N=3,192; audit figure
4,594) as one family. That would be correct if the system's claim were:

> "Across its entire history, this engine will issue at most α_family worth of
> false promotions, ever."

Nobody consumes that claim. There is no reader of Callisto's output who cares
about the error rate integrated over all time. What consumers of a *promotion
decision* care about is: "of the hypotheses standing for promotion **now**,
how likely is the best-looking one to be noise?" That is a property of the
current decision cohort, not of dead hypotheses retired two years ago.

Worse, the lifetime denominator makes the engine's rigor a function of its own
activity: generating more hypotheses — including hypotheses that were
generated, tested, honestly rejected, and never compared against today's
candidates — permanently degrades today's ability to promote. That is a ratchet,
not an error rate. BUILD_MANDATE §"failure diagnosed" names the doom loop it
fed: pool flooding → denominator inflation → zero promotions → maintenance
routine lowers gates. Re-scoping the family breaks the causal chain at the
second link without touching any threshold.

There is also a statistical objection: FWER over a lifetime assumes the family
is fixed before the data are seen. Callisto's pool grows adaptively; the
lifetime N at decision time includes tests designed after earlier results were
known, so the "family" is not even well-defined (it depends on when you ask).
A family must be enumerable at the moment of inference. A rolling cohort is;
history-in-total is not.

### 1.3 The right family: the concurrent decision cohort

The unit of joint interpretation in Callisto is a **promotion decision round**:
the set of hypotheses that are simultaneously candidates for the same
transition (backtesting→paper_trading, paper_trading→live), evaluated against
the same data window, from which effectively the best ones get promoted. Two
hypotheses are in the same family iff their p-values compete for the same
promotion slots at the same time. Operationally that is:

> Family = DISTINCT hypothesis_ids with a completed backtest/paper-trade
> evaluation inside a rolling decision window (the existing
> `CALLISTO_FWER_LOOKBACK_DAYS` semantics, re-purposed explicitly as the
> *scoping window*), optionally intersected with the candidate's sport/domain
> where cross-sport error pooling has no consumer.

Why include a window rather than literally "active now": statuses are sticky
and lag reality; a hypothesis rejected last week still competes with this
week's batch in the sense that the same generator produced both from the same
data. A 90-day window captures the contemporaneous generation process while
letting genuinely stale families fall out. Window length is a *declared
parameter of the claim* ("α_family = 0.05 per 90-day epoch"), not a tuned knob:
it changes what is promised, and must be argued whenever it changes.

### 1.4 Why not per-sport, per-batch?

- **Per-generation batch**: batches are artifacts of scheduling, not of joint
  interpretation. Two hypotheses generated in different batches but promoted in
  the same round absolutely compete; two in the same batch decided months
  apart do not. Batch scoping under-corrects exactly when throughput spikes —
  the same condition that caused the original contamination.
- **Per-sport alone**: defensible (an NFL edge does not borrow error budget
  from a golf edge in any consumer's mind), but it is secondary to time. The
  dominant driver of family size is generation rate, not breadth. Per-sport is
  supported as an optional intersection (`sport` scope mode), not as the
  primary answer.
- **Active-only (pre-audit behavior)**: known-broken; status-based counts miss
  everything recently rejected and are gameable by status churn.

### 1.5 What error rate is controlled, and for whom — the honest statement

Under the recommended `window` scope:

- Claim controlled: P(≥1 false backtesting→paper_trading promotion per
  decision epoch) ≤ α_family = 0.05, where an epoch is
  `CALLISTO_FWER_LOOKBACK_DAYS` (default unchanged, 365d; recommend declaring
  90d in config so the claim matches how often the pool actually turns over).
- Who it is for: the operator reviewing promotion decisions on human timescales
  (days–weeks), and the downstream stage.
- What it deliberately does NOT control: lifetime false promotions. That is
  covered architecturally instead of statistically — see §1.6.

### 1.6 Keeping FWER discipline: hierarchy, not weakening

The pipeline is already a sequential testing design: backtest → paper_trading
→ live. Each transition replicates the previous evidence forward. Under the
re-scoped scheme the discipline moves to where the money is:

- backtesting→paper_trading: exploratory gate; FWER scoped to the epoch
  cohort. Expected cost: up to α_family ≈ 5% chance per epoch of one false
  promote into *paper* — no capital at risk.
- paper_trading→live: confirmatory gate. Same scoping rule, but the paper
  stage is an out-of-sample replication, so the effective false-live rate is
  approximately α_family × (false-paper survival rate), multiplicative down
  the hierarchy. This is Benjamini-style layered control done structurally.

This is the crucial anti-doom-loop property: nothing here lowers any number.
Base thresholds, adaptive thresholds, min_signals, CLV floors are untouched.
Only the *denominator definition* changes, and it changes on an argued claim
about what a family is — recorded in `promotion_audit.fwer_n` alongside a
`scope` tag so every historical decision can be re-derived.

## 2. Numbers: simulation against observed hypotheses

Data available in-repo: `data/reeval_no_lookahead.json` — 20 hypotheses with
no-lookahead re-evaluated p-values and sample sizes (the only per-hypothesis
p-value corpus in the tree; production DBs are empty stubs on this machine).
Pool composition from `hypothesis_export.json`: 356 active hypotheses (NBA
126, NFL 104, NCAAB 100, PGA 26); lifetime N=3,192 per ROADMAP §3.2.

Per-test Šidák threshold α_per = 1−(1−α_base)^(1/N):

| Scoping (N) | α_per @ base .25 (bt→pt) | α_per @ .05 (pt→live) |
|---|---|---|
| lifetime 3,192 (current) | 9.01e-05 | 1.61e-05 |
| lifetime 4,594 (audit fig) | 6.26e-05 | 1.13e-05 |
| active pool 356 | 8.08e-04 | 1.44e-04 |
| per-sport NBA 126 | 2.28e-03 | 4.07e-04 |
| rolling window ~120 | 2.39e-03 | 4.27e-04 |
| per batch 25 | 1.14e-02 | 2.05e-03 |

Promotions among the 20 observed hypotheses (gate = adaptive-n max_p then
Šidák; best observed p anywhere in the corpus is 0.00698, n=59):

| Scoping | bt→pt promotions (of 20) | pt→live promotions |
|---|---|---|
| lifetime 3,192 (status quo) | 0 | 0 |
| active 356 | 0 | 0 |
| per-sport / 90d window | 0 | 0 |
| per-batch 25 | 1 (b9d9e619, p=0.0070) | 0 |

Reading, honestly:

1. Even the most aggressive argued scoping promotes essentially nothing from
   this corpus, because the corpus contains no strong evidence — min p =
   7.0e-3 clears only a batch-sized family at the exploratory tier. **Re-scoping
   is not a promotion unlock; it is a reachability restoration.** A true 3%-edge
   hypothesis needs z≈3.78 (≈3,900 signals) under lifetime N vs z≈3.33
   (~2× fewer signals, ≈1,900) under a 120-family — from ROADMAP §3.2's own
   arithmetic. The gate becomes *reachable* by real edges while keeping the
   same α_family.
2. False-discovery accounting under global null: every scoping above bounds
   FWER at the declared α_family = 0.05 *per epoch*; the difference is only how
   often an epoch closes. Lifetime: 0.05 per system lifetime. Window: 0.05 per
   90 days ⇒ expected false bt→pt promotions ≈ 0.05 × 365/90 ≈ 0.20/year, all
   landing in paper trading, where the confirmatory stage prices them at
   roughly α × survival ≈ 1%/year reaching a live decision — versus 0 forever
   under lifetime, purchased with 0 true promotions ever. The cost of the
   rescoping, stated plainly: **up to ~0.2 additional false paper-promotions
   per year, each costing only compute and a 30-day paper slot; zero
   additional expected false LIVE promotions beyond the confirmatory tier's
   own α_family.**
3. Independence caveat, disclosed: Šidák assumes independence; correlated
   hypotheses (same market, same games) inflate the true joint error. The
   portfolio-overlap gate (`_compute_portfolio_overlap`, migration 009) is the
   existing guard for exactly this and stays mandatory. Scoping does not
   replace it.

## 3. Decision

Adopt `window` scoping (distinct hypothesis_ids evaluated within
`CALLISTO_FWER_LOOKBACK_DAYS`) as the default family definition; keep
`lifetime` and add `sport` as selectable scopes via
`CALLISTO_FWER_SCOPE` for reviewability. No floor reintroduced. No base
threshold touched. Every promotion decision logs `(scope, fwer_n)` to
`promotion_audit`.

What would falsify this design: if paper_trading→live stops functioning as an
independent replication (e.g., paper trades silently mirror backtest windows),
the layered argument collapses and the tighter lifetime control becomes
defensible again. Guarded by `min_days` in stage + canonical CLV checks; if
those regress, revisit this document.
