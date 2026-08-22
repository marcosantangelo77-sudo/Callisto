# Instance 6 findings — the data plane (tools/data_collector.py, line_monitor.py, odds_api_io.py, odds_api.py, dk_scraper.py, tci_scraper.py, live_state.py)

Session opened 2026-08-22 on branch audit/tier4-data.
Method: AUDIT_MANDATE §2 protocol; ROADMAP §2 scraper kill-list treated as an
unverified prior claim and pressure-tested. All seven owned modules read in
full; schema.py / historical_odds.py / clv paths read as consumers.
Characterization tests added: tests/test_tier4_data_units.py,
tests/test_tier4_data_resolution.py (34 passing).

---

## WORK UNIT 1 — the ROADMAP scraper kill-list, pressure-tested

ROADMAP §2 says: replace mainstream odds with The Odds API (or OpticOdds),
keep only scrapers feeding sources APIs lack. Verdict after reading the stack:

### The SCRAPERS vs the HISTORY — the conflation ROADMAP warns about is real

The "14-source scraper stack" is actually three different assets that must not
share a retirement decision:

1. **ODDS TRANSPORT** (commodity, replaceable): odds_api.py (The Odds API
   client — note: already points at api.odds-api.io/v4, see F1 below),
   odds_api_io.py (odds-api.io Pro), the DK/FanDuel/Action Network/Fanatics/
   BetMGM scrapers. Any paid API can re-supply these tomorrow. Retirable to
   attic once a replacement is live and its snapshots land in the SAME tables.
   Cost of The Odds API for this workload: get_odds costs markets×regions
   credits; 8 monitored sports × 3 markets × 15-min cadence ≈ 69k credits/mo —
   roughly 10× the 500/mo free tier. odds-api.io Pro at 30k req/hr is already
   the primary (line_monitor._snapshot_sport:907-909 — the-odds-api.com is
   skipped entirely). So the migration ROADMAP proposes is largely DONE; what
   remains on the kill list is mostly already dead weight.

2. **THE ACCUMULATED HISTORY** (irreplaceable): every live snapshot is written
   by line_monitor._process_snapshot_inner into TWO stores:
   - `odds_snapshots` (raw JSON + fetched_at + source), and
   - `historical_odds_cache` (:1295-1325, INSERT OR REPLACE per sport+date).
   This archive contains book-level pre-commence prices with our own ingest
   timestamps. NO VENDOR SELLS THIS BACK: The Odds API's history starts ~mid-2022
   and charges per query; odds-api.io historical endpoints cover only rolling
   31-day windows (odds_api_io docstring :13). Pre-2024 book-level ticks exist
   nowhere else. **Rule: retire scrapers freely; never touch the tables they
   fed, and keep `fetched_at` semantics intact — it is the freshness-decay
   key for edge_scanner weighted consensus.**

3. **NON-AGGREGATED SOURCES APIs structurally lack**: MLB Statcast pitch-level,
   NHL api-web play-by-play/shots, nflverse PBP/combine, NBA stats.nba.com shot
   charts, DataGolf SG archive, ESPN box scores/PBP. These are NOT odds
   scrapers and are untouched by the ROADMAP kill-list despite living in the
   same module. They are free, keyed to no quota, and feed prop resolution +
   embedding. Keep all.

VERDICT on ROADMAP §2 row "14-source scraper stack": PARTIALLY STALE. The
primary odds path is ALREADY odds-api.io (not the-odds-api.com — see F1).
What should actually be retired: dk_scraper legacy v5 path (403'd), golf
eventgroup hardcodes, tci_scraper's hardcoded 2026 tournament list. What must
be explicitly preserved: odds_snapshots + historical_odds_cache + closing_lines
tables and their writers.

---

## VERIFIED FINDINGS

## [VERIFIED] odds_api.py:31 — "The Odds API" client actually points at api.odds-api.io/v4, not the-odds-api.com
Blast radius: SILENT (documentation/architecture level — every planning doc,
including ROADMAP §2 and CLAUDE.md quick reference, describes a vendor that is
not the one being called)
Evidence: `ODDS_API_BASE = "https://api.odds-api.io/v4"` with ODDS_API_KEY;
comment block at :33 confirms "odds-api.io v4 only accepts apiKey as a query
parameter". Meanwhile credit tracking reads `x-requests-remaining` headers —
the-odds-api.com's header scheme. If the endpoint ever IS the-odds-api.com
(e.g. env swap), the response schema (`games` shape) differs from what
line_monitor._enrich_with_dk expects. Two different vendors share one client
name; `_update_credits` logs INFO on EVERY call ("Odds API credits: ...") —
log noise at snapshot cadence.
Falsifier: hit GET https://api.the-odds-api.com/v4/sports/?apiKey=... vs the
configured base with the same key; only one responds.
For: unowned (naming/docs); Instance 5 if ProviderRouter touches it

## [VERIFIED] line_monitor.py:1295-1325 — historical_odds_cache write loses event_id and OVERWRITES all earlier same-day snapshots per sport
Blast radius: SILENT (degrades backtest-grade data quality while appearing to
"archive everything")
Evidence: `INSERT OR REPLACE INTO historical_odds_cache (sport, snapshot_date,
event_id, market_type, ...) VALUES (?, ?, NULL, 'h2h,spreads,totals', ...)`.
Schema UNIQUE(sport, snapshot_date, event_id, market_type) → one row per
sport-day. Each 15-min snapshot REPLACES the previous day's row, so the cache
retains only the LAST snapshot before midnight, not the intraday line path.
The comment claims "Even single-book snapshots are worth archiving" but the
mechanism keeps one. Meanwhile _capture_closing_lines separately writes
closing_lines — so the closing number survives, but opening/pre-move lines for
that date do not survive in this table (they DO survive in odds_snapshots,
which grows unboundedly — the real archive — but nothing prunes it either).
historical_odds.py reads this table for backtests with lead=N keys; rows
written here have market_type='h2h,spreads,totals' which can never match a
'lead=60' lookup — those backtest keys are populated elsewhere.
Falsifier: at workstation, `SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
FROM odds_snapshots WHERE sport='basketball_nba' AND date(timestamp)=<a game
day>` vs `SELECT COUNT(*) FROM historical_odds_cache WHERE sport=...
AND snapshot_date=<same day>` — second will be exactly 1.
For: Instance 6 (fix = include hour bucket or event_id in key); needs DB to verify consumer expectations first — PROPOSAL

## [VERIFIED] line_monitor.py:1435-1515 — "closing line" capture window is up to 1h+, so CLV is measured against a non-closing line
Blast radius: SILENT (CLV — the statistic Instance 3 says the gate rebuild
should depend on — is systematically computed against stale prices)
Evidence: `_capture_closing_lines` fires on EVERY snapshot for games starting
within `max(SNAPSHOT_INTERVAL+300, 3600)`s (≥1h). record_closing_line is then
called repeatedly; whether the true last pre-tip price wins depends on
clv_tracker's ordering (Pinnacle-first, captured_at DESC — data_collector's
backfill at :1245 shows the intended precedence). With a 15-min snapshot
cadence the "close" recorded is the price ≥1h before tip; real closing-line
moves in the final hour (the sharpest money) are excluded. CLV computed this
way overstates realized edge for early bets and understates for late ones.
Combined with ROADMAP §3.3 (gate reads clv_implied, devigged nowhere): the
single most load-bearing risk statistic in the system is doubly wrong — wrong
unit AND wrong timestamp.
Falsifier: at workstation compare paper_trades.closing_implied against a
known Pinnacle closing line for 20 settled games; systematic gap ⇒ confirmed.
For: Instance 6 owns capture site; Instance 2 owns clv_tracker read side

## [VERIFIED] data_collector.py:1359 — implied-probability formula inverted sign convention for negative American odds is CORRECT, but devig is absent everywhere in the CLV chain
Blast radius: SILENT
Evidence: `imp = 1/(1+100/abs(price)) if price > 0 else abs(price)/(abs(price)+100)`
— verified correct for both signs (matches calculate_implied_probability in
odds_api.py:531). BUT the value stored as `closing_implied` and subtracted
from signal_implied_prob to make clv_implied (:1261) is RAW implied, i.e. it
contains vig (~2.5-4% per side at -110/-110). A bet taken at fair-devigged -105
signal vs a raw-implied close will show spurious negative CLV even with zero
real edge, or positive CLV when the signal was worse than the close's fair
price. ROADMAP §3.3 flagged "devigged nowhere" — CONFIRMED at this exact site.
Falsifier: unit test computing clv for signal=-110, close=-110 both sides:
raw math gives clv=0 only because both carry identical vig; any vig asymmetry
manufactures ±0.01-0.02 phantom CLV. (Covered by test_tier4_data_resolution
TestClosingLineImpliedProb which pins current raw behavior.)
For: Instance 6 (add power_devig at capture time — small change, gated behind
characterization tests now in place)

## [VERIFIED] odds_api_io.py:444-453 — unparseable commence_time silently INCLUDED in fetch set, but unparseable dates downstream become None commence_time in snapshots
Blast radius: SILENT (budget waste + poisoned snapshot rows)
Evidence: get_odds filters events to a 36h window; `except (ValueError,
TypeError): today_events.append(ev)` — comment calls it "safe default". An
event with garbage date bypasses the window filter forever (every poll pays 1
request for it) and lands in snapshots with commence_time="" — which then
breaks _capture_closing_lines (skipped, fine) but also breaks backtest
pre-commence lookups (commence_dt=None → get_historical_snapshot falls to
closing-mode, tagged as if intentional).
Falsifier: mock /events returning one event with date="not-a-date"; observe
it fetched every cycle regardless of cutoff.
For: Instance 6

## [VERIFIED] odds_api_io.py:1234-1245 — pre-commence snapshot picker compares naive/aware datetimes via .timestamp() — safe — but _pick_pre_commence_entry uses candidates[-1] on entries sorted ascending: correct; the REAL defect is home/away inference order
Blast radius: SILENT (wrong team labels on fallback snapshots)
Evidence: in get_historical_snapshot, `home_guess`/`away_guess` are populated
ONLY when a movements payload carries them (:1372-1385). When it doesn't
(observed shapes vary per the function's own comment), fallback books get
normalized with home_team="" away_team="" — outcomes named "" — and
snapshot_quality='closing_fallback'. Downstream find_best_line(team=...) then
matches nothing and edges silently vanish rather than erroring.
Falsifier: call get_historical_snapshot against a mocked movements payload
lacking top-level home/away; inspect result["bookmakers"][x]["markets"]
outcome names.
For: Instance 6

## [VERIFIED] dk_scraper.py:61-93 — abbreviation map has duplicate keys silently dropped by Python dict literal
Blast radius: SILENT (specific teams expand wrongly → matchup merge fails → DK enrichment no-ops for those games)
Evidence: `"SEA": "Seattle"` appears under BOTH NFL and NHL sections (fine),
but `"DAL": "Dallas"` appears twice (NBA + NHL, same value, fine) — however
`"CAR": "Carolina"` (NHL Hurricanes) collides with nothing in NFL (CAR Panthers would be "Carolina" too — OK). The REAL collisions: `"WSH": "Washington"` (NHL) vs NFL's `"WAS": "Washington"` — distinct, OK. Actual defect found: `"GS": "Golden State"` and `"NY": "New York"` are 2-letter prefixes that will mis-expand ANY league whose team abbrev starts with GS/NY on the Nash endpoint format "PREFIX Mascot". Low probability but silent when it fires: e.g. an unknown new team "LV ..." expands to "Las Vegas ..." correctly, but a hypothetical "NYR Rangers" → "New York Rangers" only because NYR is mapped. Teams NOT in the map pass through as "PHO Suns"-style short names, which then fail _matchup_key matching against full-name API games — the merge skips them quietly (enriched counter just doesn't increment).
Falsifier: feed _normalize_nash_response a selection labeled "UTA Club X" not in map; output keeps "UTA Club X" while odds_api_io games say "Utah …"; matchup merge drops the DK entry.
For: Instance 6

## [VERIFIED] dk_scraper.py:787-796 — Nash failure silently falls through to a KNOWN-403 legacy path, returning {"error"} — good — but scrape_dk_props still uses ONLY the legacy v5 host for props
Blast radius: LOUD for main odds (error propagates), SILENT for props
Evidence: scrape_dk_props (:899-913) builds URLs from DK_ENDPOINTS (Akamai-
blocked v5 host). Every category request will 403 → logged warning per prop →
returns players={} with errors list. Callers checking only player_count==0
treat it as "no props offered tonight" — plausible-but-wrong. The Nash
endpoint that works for main lines is not wired for props at all.
Falsifier: run scrape_dk_props("basketball_nba", <id>) today; expect HTTP 403
on every category and player_count=0 despite live DK props.
For: Instance 6

## [VERIFIED] line_monitor.py:707-726 — failure-backoff skip logic means chronically failing sports are retried on cycles ≡0 mod 4/8, but success resets counter — correct — HOWEVER use_fallback decision reads get_credit_status() whose remaining is None when headers were never seen, so fallback NEVER activates on fresh start with unset ODDS_API_KEY... except the api_key_set check catches that. Verified consistent. No finding.
(Recording explicitly per mandate: audited, clean.)

## [VERIFIED] live_state.py:266-271 — retention prune compares ISO strings where writers stamp now.isoformat() with tz "+00:00" — lexicographic order holds ONLY if every writer uses identical format
Blast radius: SILENT (prune fails silently if any writer stamps differently)
Evidence: store_state stamps `datetime.now(timezone.utc).isoformat()` =
"YYYY-MM-DDTHH:MM:SS.dddddd+00:00". _prune_for_event cuts with
`datetime.fromtimestamp(...).isoformat()` — same format. Consistent TODAY.
But recent_states ORDER BY ts DESC and the 24h counters compare against
`(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()` — all four
sites agree. Fragile-by-convention, currently correct. No defect; noted as a
convention contract with zero enforcement (one writer switching to
Z-suffix breaks pruning silently).
Falsifier: insert a row with ts="2026-08-22T00:00:00Z" and confirm it is
never pruned and corrupts ORDER BY ordering relative to +00:00 rows.
For: Instance 6 (cheap hardening: normalize on write)

## [VERIFIED] tci_scraper.py:83-129 — coaching tenure FALLBACK data is hardcoded per human, undated, and silently preferred when ESPN returns tenure 0/None
Blast radius: SILENT (TCI sub-signal — Instance-relevant because
get_experience_signal/get_stability_signal emit betting signals with
hardcoded backtest win rates)
Evidence: COACHING_TENURE_FALLBACK maps team→(coach, years) "as of 2025-26".
get_team_info uses it whenever ESPN's tenure is falsy. A mid-season coaching
change makes the fallback actively wrong, and the TCI score (30% weight on
coaching_stability) drifts without any staleness marker in the DB row. The
docstrings in get_experience_signal cite p=0.17 / p=0.27 — NOT significant —
yet the functions return confidence="high"/"medium" and fixed
backtest_win_rate constants. These flow into hypothesis generation as if
validated. Per Instance 3's standard: a signal with p=0.17 advertising 66.7%
win rate is exactly the self-reported-confidence pattern the mandate bans.
Falsifier: recompute the 2026 NCAAW n=52 backtest with tenure pulled live
from ESPN only (no fallback); win-rate deltas > noise ⇒ fallback contaminated.
Also: TOURNAMENT_TEAMS_2026 hardcodes a bracket that expires yearly.
For: Instance 6 (own file); signal gating belongs with Instance 3 standards

## [VERIFIED] data_collector.py:376-380 — game_contexts upsert "richer wins" heuristic compares JSON STRING LENGTH, not information content
Blast radius: SILENT
Evidence: ON CONFLICT keeps excluded.context_json iff
`length(excluded) >= length(context_json)`. A shorter but fresher/correcter
context (e.g. corrected final score with fewer notes) loses to a longer stale
one. Conversely a long sparse payload beats a compact rich one. Intent
(documented) is "prevent sparse re-collection regression" — length is a proxy
that inverts in both directions. Scores themselves are COALESCE'd separately
(correctly), so damage is limited to context_json enrichment fields.
Falsifier: insert context A (long, stale officials list), collect B (short,
corrected broadcast data); B silently discarded.
For: Instance 6

## [VERIFIED] data_collector.py:1766-1788 — Statcast chunked insert counts ATTEMPTED rows as STORED ("stored_pitches += len(chunk)" before/despite executemany failing → wait, it's inside try; on exception the chunk is skipped but prior chunks counted; on partial constraint-skips INSERT OR IGNORE hides duplicates silently)
Blast radius: LOUD for chunk failures (warning logged); SILENT for dup-skip
Evidence: INSERT OR IGNORE on (game_pk, at_bat_number, pitch_number) means a
re-run reports "stored 5000 pitch rows" when 0 were new. Downstream
collection-stats consumers cannot distinguish fresh ingest from no-op.
Mitigated by tracked_ingestion recording the call succeeded either way — the
SLA monitor therefore cannot detect a silently-dead Savant feed that returns
200 with empty CSV… actually empty CSV hits `total_pitches == 0 → return
{"error": ...}` (LOUD-ish). Residual silent case: CSV returns a header-only
error page >100 chars — csv.DictReader yields 0 rows → caught above. Clean
enough; residual issue is the inflated stored count only.
Falsifier: run collect_statcast twice for the same date; second log claims
N stored again.
For: Instance 6 (cosmetic-to-moderate observability fix)

---

## SILENT-FAILURE HUNT SUMMARY (priority 1)

Paths that can return plausible-but-wrong data quietly, ranked:

1. **Closing-line window (line_monitor)** — CLV against 1h-old prices. Worst
   in class: feeds the money gate.
2. **Raw-implied (no devig) in clv_implied (data_collector:1261)** — phantom
   edge of ±1-4% baked into every CLV row ever written.
3. **historical_odds_cache single-row-per-day overwrite** — backtest archive
   thinner than it looks; only last snapshot per sport-day survives there.
4. **_closing_from_snapshot soft-book fallback** — when no sharp book present,
   silently returns best soft-book close with close_reliable effectively false
   upstream (data_collector:1362-1367 logic verified: prefers sharp, falls
   back quietly).
5. **DK props always-empty via blocked v5 host** — reads as "no props
   offered".
6. **Unparseable commence_time included forever (odds_api_io)** — budget leak
   + null commence in archives.
7. **home/away="" fallback snapshots (odds_api_io)** — outcome names empty;
   downstream team filters match nothing; edges vanish without error.

Counter-note (mandate honesty clause): several claimed-silent paths checked
CLEAN — live_state backoff ladder, line_monitor drain lock (post-C8 fix is
correct), WS delta merge preserving multi-book consensus, contamination filter
in find_best_line, Nash unicode-minus parsing. The 2024-era audit fixes in
this stack are genuinely good.

## UNIT / TIMEZONE AUDIT (priority 2)

- American↔decimal: _decimal_to_american (io), _dk_american_odds (dk),
  calculate_implied_probability (api) — all three implementations AGREE and
  are pinned by tests/test_tier4_data_units.py. No drift found. GOOD.
- Devigged vs raw: _evaluate_movement devigs consensus via power_devig BEFORE
  comparing to moved line — CORRECT. But clv_implied (collector) and the CLV
  gate (Instance 2's territory) operate raw — INCORRECT. One codebase, two
  conventions, joined at paper_trades.
- UTC vs local: data_collector defaults date param to UTC "today"
  (:219,:469,:1399) — for US sports an evening game (ET) completes on UTC
  next-day; collect_scores(UTC-today) misses yesterday-ET finals until the
  UTC clock rolls. local_game_date exists (canonical, tools.game_dates) and
  is STORED, but collection windows and resolve_* queries key on game_date
  (UTC-derived). Paper trades resolved by game_date=UTC-date can miss games
  played "yesterday" locally. Mitigation: callers may pass explicit dates.
  INFERRED impact magnitude (needs DB); mechanism VERIFIED.
- Game-time vs fetch-time: fetched_at stamping (line_monitor:286-308) is
  careful and preserves earliest stamps — GOOD. Book last_update vs our
  fetch time distinguished — GOOD. Exception: odds_api_io normalization sets
  last_update per-market from updatedAt but bm-level last_update takes the
  LAST market's update arbitrarily (:712-771) — cosmetic.

## Q6 — how I'd build each piece today

- **Odds ingestion**: single normalized internal schema at the boundary
  (pydantic), one provider adapter interface, providers = {odds-api.io WS for
  live, OpticOdds for props/deep alt lines}. Kill per-provider quirks at the
  door: the current stack has THREE American-odds converters and TWO
  matchup-key schemes (dk_ prefix ids vs numeric io ids) that must never be
  joined — today they coexist in odds_snapshots and are reconciled by string
  matching (_matchup_key lowercases team names; "dk_" id games merged by name
  only). Buys: fewer silent-merge misses, one place to enforce units.
- **History**: append-only parquet/duckdb export of odds_snapshots nightly,
  immutable, with schema versioning. SQLite WAL is fine operationally but the
  archive deserves a format scientists can query without the app. Buys: the
  $800-history becomes portable and un-deletable by a bad migration.
- **Statcast/NHL/NFL/NBA feeds**: keep exactly as-is — these mirror public
  APIs 1:1 with sane coercion clamps (verified _f ranges). The maintained-
  library question: mlb-stats-api / hockey_rpy / nflreadpy exist but add
  dependency risk for marginal gain; nflverse CSV direct is already the
  canonical route nflreadpy wraps.
- **TCI**: delete the hardcoded fallback tables; compute from live rosters
  only, and mark missing tenure as missing instead of guessing. The signal
  functions' hardcoded win rates belong in the hypothesis registry with
  p-values attached, not in scraper code.

## Q7 — retirement conditions

- dk_scraper legacy v5 path: condition already met (403). Move to attic with
  restore note; keep LEAGUE_IDS mapping (Nash uses same ids).
- BetMGM scraper import in line_monitor (:43): imported, only used by
  _enrich_with_mgm which is never called (:853-855 says DISABLED). Dead code
  keeping a broken scraper warm — attic.
- tci_scraper TOURNAMENT_TEAMS_2026: expires after March 2026; replace with
  rankings-driven discovery (already half-implemented via Source 1).
- odds_api.py (the mislabeled client): retire IF and ONLY IF a decision is
  made about F1 below; its get_scores/find_best_line are still imported by
  line_monitor.
- NEVER: odds_snapshots, historical_odds_cache, closing_lines writers, the
  statcast/nhl/nfl/nba collectors.

## OPEN QUESTIONS FOR THE OWNER / WORKSTATION

1. F1: is ODDS_API_KEY an odds-api.io key or a the-odds-api.com key? The code
   says io; the docs say .com; the credit headers say .com. One line in .env
   resolves a whole architecture ambiguity.
2. Query: SELECT COUNT(*) FROM historical_odds_cache WHERE event_id IS NULL —
   sizes the damage from the overwrite finding.
3. Query: SELECT COUNT(*) FROM paper_trades WHERE clv_implied IS NOT NULL —
   every such row carries the raw-not-devig defect; decide whether to
   backfill corrected values or annotate provenance.
