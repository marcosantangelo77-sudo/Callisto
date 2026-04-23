# tools/quant — quantitative microstructure engine

Callisto's pricing layer. Treats the betting market as a set of noisy,
lagged, vig-loaded estimates of an unobserved true probability, then
extracts a best estimate and flags actionable divergences.

This is **not** a modeling layer (yet). It is a *prices* layer: given
odds from every book, what's the fair probability, which books are
moving first, and where is there a meaningful gap between where a
soft book is quoting and where the sharp consensus actually sits?

## Module layout

| Module | Purpose |
|---|---|
| [`consensus_engine.py`](consensus_engine.py) | Devig each book, trim outliers, tier-weight the survivors, return a calibrated consensus fair probability with a standard error and a disagreement flag. |
| [`sharp_detection.py`](sharp_detection.py) | Scan a per-book odds time series for steam clusters, first-mover lead/follow events, reverse-line-movement, and limit-down signals. |
| [`edge_ranker.py`](edge_ranker.py) | Score every open market × outcome (consensus fair vs placement book fair), subtract detection-risk / staleness / limit / disagreement penalties, rank by effective edge, persist to `live_edge_surface`. |

## The mental model

Every real sports betting operator answers three questions before placing a bet:

1. **What's the true probability?** — not what DraftKings says (it's vigged), not what one book says (it's noisy), but what the market's sharpest participants collectively think. This is the consensus engine's job.
2. **Is the market moving right now, and in which direction?** — late arbitrage opportunities vanish in 30-90 seconds. Steam detection and first-mover tracking keep the system oriented to the current state, not the pre-game state.
3. **Is a specific soft book offering a line that disagrees with the consensus by enough to beat its detection algorithm + the book's limits + the line's staleness?** — the edge ranker integrates all three into one score per market.

## Why the details matter

**Devig method per book.** Pinnacle's overround is small and symmetric; multiplicative devig (proportional scaling) recovers the true fair cleanly. Retail books charge more vig on favorites than underdogs; multiplicative devig *overestimates* favorite probabilities there. The power method solves for the single exponent that normalises the probabilities and handles both cases. `consensus_engine._devig_one_book` applies the right method per book's tier automatically.

**Tier weights.** Not all 15 books are equal. Pinnacle moves first on most markets. Fanatics and Hard Rock are last. A simple arithmetic mean lets the slowest retail lines dominate the consensus, which defeats the purpose. The module assigns sharp tier = 0.60 weight, reference tier = 0.25, soft tier = 0.15, so the consensus leans toward the books with the most information. Override per-market via the `tier_weights` argument when your empirics say otherwise (e.g., Pinnacle isn't sharpest on UFC props; Betfair Exchange is).

**Outlier trimming.** A single book with a stale auto-price or a bad injury feed can swing an average by tens of basis points. Trimming at 2σ from the weighted median kills that contamination while keeping legitimately-fast books that moved first but correctly. The `disagreement` flag surfaces whenever we trim — the caller knows they're on a noisy market and can downweight the signal.

**Quadratic detection-risk penalty on soft books.** `penalty = 3 × edge² × softness`. A 1% edge on DK is invisible. A 7% edge on DK is a flashing red alarm. The quadratic shape means small edges eat small penalties and huge edges eat most of themselves — which is the shape you want for stealth. Sharp books get no penalty (they don't limit sharp players because they model sharp money as information, not a threat).

## Using it

```python
from tools.quant import (
    BookLine, MarketSnapshot,
    compute_consensus_fair_prob, score_edge, rank_edges,
    LineTick, scan_market_movement,
)

# 1. Consensus for one outcome
lines = [
    BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
    BookLine("draftkings", 0.524, paired_implied_prob=0.524),
    BookLine("fanduel", 0.520, paired_implied_prob=0.520),
    BookLine("fanatics", 0.540, paired_implied_prob=0.540),
]
consensus = compute_consensus_fair_prob(lines)
# → consensus.fair_prob ≈ 0.50
# → consensus.std_err, consensus.disagreement, consensus.outlier_books

# 2. Score one edge
snap = MarketSnapshot(
    sport="baseball_mlb", event_id="MLB_2026_04_18_NYY_BOS",
    market="h2h", outcome="Yankees",
    placement_line=BookLine("fanatics", 0.460, paired_implied_prob=0.560, limit=250),
    all_lines=lines,
)
edge = score_edge(snap)
# → edge.effective_edge, edge.decision ∈ {"recommended", "hold", "skip"}
# → edge.penalty_breakdown — {detection_risk, book_limit, staleness, ...}

# 3. Rank every open market
ranked = rank_edges([snap1, snap2, ...], top_n=25, min_recommend_edge=0.02)
# → first N are decision="recommended", sorted by effective_edge desc

# 4. Classify movement on a time series
ticks = [LineTick("pinnacle", "MLB_...", 0.500, ts_1), ...]
signals = scan_market_movement(ticks, public_pct_on_side=0.72)
# → [SharpSignal(kind="steam", ...), SharpSignal(kind="first_mover", ...), ...]
```

## Schema

`live_edge_surface` is populated by `persist_ranked_edges`. Latest ranking:

```sql
SELECT * FROM live_edge_surface
WHERE computed_at = (SELECT MAX(computed_at) FROM live_edge_surface)
  AND decision = 'recommended'
ORDER BY rank;
```

Persistence across snapshots ("this edge survived three scans in a row →
not a flicker") via:

```sql
SELECT event_id, market, outcome, placement_book,
       COUNT(*) AS snapshot_count, AVG(effective_edge) AS avg_edge
FROM live_edge_surface
WHERE computed_at > datetime('now', '-30 minutes')
  AND decision = 'recommended'
GROUP BY event_id, market, outcome, placement_book
HAVING snapshot_count >= 3
ORDER BY avg_edge DESC;
```

## Testing

```bash
pytest tests/test_consensus_engine.py tests/test_sharp_detection.py tests/test_edge_ranker.py -v
```

44 tests covering: devig primitives, consensus aggregation invariants (tier weights dominate, outliers trim, numerical floor, ESS math), sharp-detection primitives (steam clusters, first-mover windows, RLM direction, limit-down), edge ranker decision boundaries (recommended/hold/skip transitions, penalty curve shapes, rank ordering).

## References

Design grounded in Buchdahl's *Squares and Sharps, Suckers and Sharks* (2016) on devig method selection, Miller & Davidow's *The Logic of Sports Betting* (2019) on sharp-line identification and steam detection, and Kish's classical effective-sample-size formula for the weighted-mean standard error. The quadratic detection-risk penalty is adapted from Cover & Thomas, *Elements of Information Theory*, adjusted for bettor-operator adversarial payoff.

## What this module does NOT do (yet)

It's a *prices* layer. These are separate modules that should use this one as an input:

- **Modeling** (`tools/quant/models/`): sport-specific probability models (MLB pitcher K distribution from statcast, NBA shot-quality models, NHL xG, etc.). The consensus fair is the "market prior"; a model should either agree or disagree with it — and when it disagrees with strong evidence, that's a different, richer class of edge than the microstructural one this module catches.
- **Portfolio** (`tools/quant/portfolio.py`): accept K ranked edges, apply correlation-aware Kelly under bankroll + exposure + book-limit constraints, return a vector of stakes.
- **Execution routing** (`tools/quant/router.py`): given a decision to bet, pick the book / size / timing that maximises expected value net of detection and limit risks.
- **Footprint / reflexivity** (`tools/quant/footprint.py`): model how placing our own bet moves the line; discount the expected edge accordingly.

These land in subsequent commits.
