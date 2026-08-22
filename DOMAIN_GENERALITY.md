# DOMAIN GENERALITY — Seam Map

Instance 6 · 2026-08-22 · branch `audit/tier4-data`

**Question asked of every subsystem:** does this still make sense if the domain is
drug-discovery literature, supply-chain anomalies, or security research? Where the
answer is no, the general version is specified.

**Verdict up front.** The AGP session machinery (phases, evidence model,
confidence governance, seals) is genuinely domain-general and was built that way.
The coupling is concentrated in three places: (1) the orchestrator's hardcoded
betting tool layer (~600 of 1896 lines), (2) the hypothesis lifecycle's evidence
model (`won/lost/push`, American odds, CLV), and (3) a schema with no seam at all
(~35 sports tables interleaved with ~7 core ones in one `SCHEMA_SQL` blob). All
three are coupled-but-separable; nothing in the core loop is beyond repair.

Classification legend:
- **GENERAL** — domain-general today.
- **SEPARABLE** — coupled, but a defined interface decouples it without redesign.
- **SPORTS-SHAPED** — the logic itself encodes sports/betting semantics; needs
  rethinking, not just moving.

---

## 1. agp/ — research protocol & confidence rules

**Status: GENERAL (VERIFIED).**

- `agp/thresholds.py` (entire file, 54 lines): tier boundaries, source-class
  ceilings ("a model cannot self-report higher confidence than its best evidence
  warrants"), contradiction penalties, DB floor. Zero sports content. This file
  *is* the pattern the rest of the repo should follow: named constants, one place,
  imported by everyone.
- `agp/__init__.py:31-36` — the `Domain` enum is FINANCIAL / TECHNICAL / SIGNAL /
  SYNTHESIS / GENERAL. **There is no SPORTS value at all.** The domain taxonomy
  already treats betting as an application, not a domain.
- The AGP cycle steps (declare scope → assign domain → enumerate sources →
  collect primary evidence → contradiction check → synthesis + review → seal) speak
  only of scope/evidence/contradictions/confidence. Falsifier: grep `agp/` for
  `odds|bet|sport|line` — no hits in protocol logic.

The irony worth stating plainly: the most general code in the repo is the code
that enforces honesty, and per the owner's correction, that is exactly where the
value lives.

## 2. orchestrator.py — the research loop

### 2a. Phase machinery: GENERAL (VERIFIED)

`run_session()` (orchestrator.py:782-939) and its seven `_step_*` methods are
fully abstract. No phase knows what a game is.

### 2b. Tool layer: SPORTS-SHAPED as written, trivially SEPARABLE

This is the single most important coupling line in the repo:

- **orchestrator.py:1179** — `available_tools = [WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL] + ODDS_TOOLS`.
  Every session, regardless of domain, is offered 21 hardcoded betting tool
  schemas (L144-632). A drug-discovery query gets `get_odds(sport="basketball_ncaab")`
  in its prompt.
- **orchestrator.py:50-90** — ~40-line hardwired import block for
  odds_api/edge_scanner/parlay_scanner/clv_tracker/devig/sim/sizing/sgp. Not
  pluggable; imported at module load.
- **orchestrator.py:1261-1486** — `_execute_tool` is a ~225-line if/elif chain of
  which ~200 lines are betting dispatch. Only web_search and claude_code are general.

**Decoupling interface (ToolRegistry):**
```
Orchestrator(toolset="research-core")            # web_search + claude_code only
registry.register(DomainPlugin)                  # name, schemas, execute(), freshness_rules, classifier_keywords
available_tools = core + registry.tools_for(session.domain)
```
`POST /task` gains optional `domain`/`toolset` fields so callers constrain the
session's toolkit. Betting becomes `tools/domains/sports.py` — first among equals,
not the default payload.

### 2c. Freshness heuristic: SPORTS-SHAPED (VERIFIED)

`_SPORTS_FRESHNESS_PATTERN` / `_detect_freshness` (orchestrator.py:656-675) is a
hardcoded regex of NBA team names ("celtics|lakers|warriors…") controlling Brave
search freshness for *any* query. The general version: per-domain freshness rules
(`regex → freshness window`) supplied by the registered DomainPlugin. Trivial fix,
real correctness win — today a security-research query mentioning "Warriors"
(mascots, company names) gets mis-freshened.

### 2d. Task routing: SEPARABLE (VERIFIED)

`tools/task_classifier.py` buckets timeouts on betting phrasing: NEWS =
"injuries/weather/lineup", QUICK = "current odds/current score/who's winning",
HYPGEN = "find new edges". The *concept* (fast-path vs deep-path classification)
is general; the keyword lists belong in domain-plugin config.

## 3. Hypothesis lifecycle (tools/hypothesis.py)

### 3a. State machine: GENERAL machinery, coupled gate CONTENTS

`STAGE_ORDER = ["draft","backtesting","paper_trading","live","retired"]`
(L185, VERIFIED) with CAS transitions is an excellent abstraction for any
falsifiable claim — arguably the best idea in the repo. But every promotion gate
assumes the evidence stream is resolved bets:

- **L140-161 `PROMOTION_GATES`**: keys are `min_clv_rate`, `min_positive_edge_rate`,
  `max_brier`, `min_ic`, `max_drawdown`, `min_sortino`. CLV is meaningless without
  a closing line; "edge vs book implied prob" presumes a book.
- **L1555-1594**: pre-live Monte Carlo via `bankroll_sim` (`ruin_prob_30d`) —
  capital-shaped, not sports-shaped per se, but imported directly with no abstraction.
- **L1596-1662 regime-diversity gate**: buckets hypotheses by `sport|season_phase`
  via imports from `tools.market_regime`; queries `SELECT DISTINCT sport, game_date
  FROM paper_trades`. The underlying idea — "the sample must span ≥2 distinct
  context regimes before you trust it" — is fully general; the regime *key* must be
  a parameter.

**Decoupling interface (GateSpec):**
```
GateSpec = {min_n, p_threshold, calibration_max_brier, effect_min,
            regime_diversity: int, risk_sim: RiskSimulator | None}
```
supplied per transition, per hypothesis-domain. CLV/ruin/regime checks become an
optional `MarketPlugin` attached only when a market exists. Inline literals get
promoted to named constants (see 3c).

### 3b. Evidence model: SPORTS-SHAPED — the deepest coupling

There is **no resolver interface** (VERIFIED — falsifier: there is no abstract
base class or protocol anywhere between hypothesis evaluation and raw DB reads).
Ground truth is read directly:

- Outcome vocabulary is literally `"won"/"lost"/"push"`, hardcoded in ~6 places
  (L755, L1084, L1617, L2218, L2239-2251, L2499-2508).
- Expected rate = `book_implied_prob`; returns = `american_to_decimal(odds)-1`
  (L774-787). Scoring is tied to bet profit, not prediction-vs-outcome.
- `_get_paper_trades` (L2676-2714) partitions by `game_date, home_team,
  away_team`. Column names are literally teams.
- `_days_of_odds_data` (L2621-2637) checks data availability by querying
  `historical_odds_cache WHERE sport = ?`.

**Decoupling interface (OutcomeResolver / EvidenceRecord):**
```
EvidenceRecord = {event_id, predicted_prob, resolved_outcome, payoff, context_key, resolved_at}
OutcomeResolver(hypothesis) -> Iterator[EvidenceRecord]     # yields ground truth as it arrives
```
With this, `evaluate_significance`, Brier/IC/calibration-bins, collapse/dedup
logic, and `_count_unresolved` all operate on the record shape and never see a
team name. **What 'paper trading' means generally:** a forward-test on live,
not-yet-resolved predictions recorded *before* resolution — i.e. preregistration.
The general lifecycle rename: draft → backtesting → **forward_testing** →
**deployed** → retired, where "deployed" means the conclusion drives decisions
(bets, paper recommendations, triage flags), not necessarily capital. Ground
truth arriving = resolver emits `resolved_outcome`; betting is just the domain
where that happens within hours.

### 3c. Inline literals: SEPARABLE hygiene issue

- L1415 `p > 0.15 and n >= 30` — magic numbers inline while sibling tiers
  (L168-183) are named env-overridable constants. Same class of bug flagged in
  COORDINATION.md for AUTO_REJECT_*.
- `market_type == "h2h"` branches inside gate logic (L1348, L1380) — a sportsbook
  enum branching inside what should be generic calibration policy. Replace with a
  per-hypothesis calibration profile.
- Hit-rate floors of 0.45/0.55/0.70 (L1424, waiver rule copy-pasted 4× at
  L1354/1388/1457/1510) assume a ~50% base rate. A drug-safety signal with a 2%
  base rate would be mass-auto-rejected. **General form:** thresholds relative to
  the claim's expected base rate, not absolute.

### 3d. Genuinely general today (keep as-is)

`binomial_pvalue` (L343), `ttest_one_sample` (L362), `z_score` (L384),
`sharpe_ratio` (L394), `max_drawdown` (L404), `calibration_bins` (L427),
adaptive p-threshold (L259-297), Šidák FWER correction (L1122-1186).
These score any binary prediction against any resolved outcome. Extract into a
domain-general stats module unchanged. Note the calibration machinery is *already*
prediction-vs-outcome (Brier, IC, calibration bins); only the feeding records are
bet-shaped.

## 4. Schema (tools/schema.py) — no seam exists

**Status: SPORTS-SHAPED structure, SEPARABLE contents (VERIFIED).** One file, one
`SCHEMA_SQL` string, one `ensure_schema()`. ~55 tables:

| Class | Count | Examples |
|---|---|---|
| Domain-general | ~7 | embeddings(469), event_log(485), ingestion_runs(1355), schema_migrations(1436), system_improvements |
| Coupled-but-separable (calibration/lifecycle) | ~13 | books(99), markets(119), odds_snapshots_v2(137), clv_log(172), signals(248), hypotheses(277), backtest_runs(306), backtest_events(343), paper_trades(382), hypothesis_stats(422), historical_odds_cache(451) |
| Fundamentally sports-shaped | ~35 | game_results(1117), player_stats(604), nba/nfl/nhl/ncaa/golf player+event tables(710-1053), statcast_pitches(630), masters_* ×4(1195-1338), prop_snapshots(1291), regime_rules w/ hardcoded MLB/NFL/NBA/NHL boundaries(60-83,1159), bankroll_peak(1682) |

One golf tournament has four dedicated tables. That tells you how the schema grew.

**Where the seam breaks:** `hypotheses.sport NOT NULL` plus
`backtest_events.hypothesis_id` / `paper_trades.hypothesis_id` FKs mean the *core*
lifecycle tables cannot exist without betting columns. The pivot tables are both
the moat and the weld.

**Proposed seam:**
```
schema/core.py      : embeddings, event_log, ingestion_runs, migrations, wiki, tasks,
                      system_improvements +
                      predictions(id, claim, predicted_prob, created_at, domain_config JSON)
                      outcomes(prediction_id, resolved_outcome, payoff, context_key, resolved_at)
                      calibration_runs   # brier/ic/p/z — columns already exist in hypothesis_stats
plugins/sports/schema.py : everything from books through prop_snapshots
```
`hypotheses.sport/market_type/edge_threshold/kelly_*` move into a `domain_config`
JSON column or plugin extension table. Migration path: views over existing tables
first, physical split later — nothing needs to break to start.

## 5. Autonomous loop (tools/autonomous.py)

**Skeleton: GENERAL (VERIFIED).** Cycle counter, coprime-interval phase
scheduling (L1310-1316), budget deferral, pause/drain coordination, self-repair /
diagnose / watchdog / integrity / knowledge phases — none of this knows about
sports.

**Phases: mostly SPORTS-SHAPED, all SEPARABLE.**
- `_phase_live_execute` (5727) — real bet execution, Kelly sizing, drawdown
  kill-switch, Telegram approval. Deepest coupling; belongs entirely behind the
  plugin boundary.
- `_phase_paper_trade` (6370) — hard-depends on DK scraper + Odds API.
- `_phase_collect_data` (3157), `_phase_injury_prop_hypotheses` (3538),
  `_phase_backtest` (4477), `_phase_evaluate` (5023), narrative/regime/granger/
  refresh-signals phases — all odds-keyed.
- Structural tell (VERIFIED): the loop waits for `line_monitor` snapshots (244)
  and silently no-ops its paper/live phases when odds sources aren't importable.
  **Today there is no configuration in which the autonomy loop runs a
  domain-free research cycle.** That is the clearest single statement of how far
  the product drifted from the stated design.
- Hygiene violations: inline betting-table SQL in the loop body (pruning
  prop_snapshots/backtest_events at ~2655-2685; `ev_opportunities` writes noted at
  1524-1534) — the loop reaches past any abstraction straight into domain tables.

**Decoupling interface (DomainPlugin lifecycle hooks):**
```
collect_data() / generate_hypotheses() / resolve_outcomes() / evaluate() / execute_actions()
```
The skeleton calls hooks; the sports plugin implements them; a literature plugin
implements collect=ingest papers, resolve=retractions/replications/new trial data.

## 6. API surface (api.py)

**Core: GENERAL (VERIFIED).** `POST /task` (1277), `/task/{id}`, `/world/{domain}`
(1410 — genuinely generic semantic retrieval over the Domain enum), `/wiki/*`,
`/research/*`, `/embeddings/*`, `/health/*`, `/admin/*`, `/context/sync`.

**Annex: ~45 sports routes (VERIFIED)** — `/odds/*` (~18), `/bets/*` (record,
resolve, clv-report, bankroll), `/edges/live`, `/analysis/futures-efficiency`,
`/simulate/basketball|poisson|portfolio`, `/model/injury-impact`, `/data/injuries|
weather|referee`, `/boosts/*`, `/historical/*`, `/backtest/*`, `/orders/*`,
`/executor/*`. Cosmetic-abstraction problem: the Domain enum has no SPORTS value,
yet ~45 sports routes sit beside it as unversioned peers, and `POST /task` accepts
no domain/toolset field.

**Fix:** mount sports routes under an optionally-loaded `/sports/...` sub-router;
core stands alone. Cheap, non-breaking, and makes the architecture statement true
in the URL space.

---

## Ranked by capability unlocked

| Rank | Change | Unlock | Effort |
|---|---|---|---|
| 1 | **OutcomeResolver/EvidenceRecord** (§3b) | Calibration machinery scores ANY claim vs ANY resolved outcome — the actual product ("accurate, honestly earned conclusions") stops requiring a sportsbook. Everything downstream (gates, stats, schema pivot) unblocks. | Medium |
| 2 | **ToolRegistry / DomainPlugin** (§2b, §5) | Orchestrator can be *pointed* at any domain — currently impossible without editing source; even a pure-literature query ships 21 betting tools. Includes fixing the loop's hard wait on line_monitor. | Medium |
| 3 | **Schema seam** (§4) | Makes 1 and 2 durable; ends the `sport NOT NULL` weld on the core lifecycle. Views-first migration keeps it low-risk. | Large but incremental |
| 4 | **POST /task domain/toolset field + `/sports/` router split** (§6) | External callers can use Callisto as advertised; API surface stops claiming the product is a sportsbook. | Small |
| 5 | **Base-rate-relative thresholds + named constants** (§3c) | Correctness for any low-base-rate domain; kills the inline-literal class of bug COORDINATION.md already flagged once. | Small |
| 6 | **Freshness rules out of team-name regex** (§2c) | Real retrieval-correctness bug today, five-line fix pattern. | Tiny |

Items 4-6 can land independently and immediately; items 1-3 share a dependency
order (resolver defines the record shape the schema seam stores).

## One-line answer to the standing question

Drug discovery: AGP works today; the tool payload, evidence model, and schema do
not — and every blocker reduces to one missing abstraction (EvidenceRecord +
DomainPlugin) that sports betting then becomes merely the first plugin for, kept
because it remains the only domain where ground truth arrives fast enough to
measure calibration at all.
