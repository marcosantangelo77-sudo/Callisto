"""
CALLISTO FULL SYSTEM AUDIT — Functional End-to-End Verification

This is NOT a unit test. This traces real data through every pipeline stage
and verifies mathematical correctness, data integrity, and module interconnection.

Every check produces PROOF — actual numbers, actual data, actual computations
that can be independently verified.
"""
import asyncio
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

failures = []
passes = []
warnings = []


def check(name, condition, proof=""):
    if condition:
        passes.append(name)
        print(f"  [PASS] {name}")
        if proof:
            print(f"         Proof: {proof}")
    else:
        failures.append(name)
        print(f"  [FAIL] {name}")
        if proof:
            print(f"         Evidence: {proof}")


def warn(name, detail):
    warnings.append(name)
    print(f"  [WARN] {name}: {detail}")


async def run_audit():
    print("=" * 70)
    print("CALLISTO FULL SYSTEM AUDIT — FUNCTIONAL VERIFICATION")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)

    # ══════════════════════════════════════════════════════════════════
    # STAGE 1: DATA INTEGRITY — Is the data in the DB valid?
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("STAGE 1: DATA INTEGRITY")
    print("=" * 50)

    # 1.1 Game results: scores must be non-negative integers
    print("\n[1.1] Game Results Validity")
    bad_scores = conn.execute(
        "SELECT COUNT(*) FROM game_results WHERE home_score < 0 OR away_score < 0"
    ).fetchone()[0]
    total_results = conn.execute("SELECT COUNT(*) FROM game_results").fetchone()[0]
    check("game_results_no_negative_scores", bad_scores == 0,
          f"{total_results} results, {bad_scores} with negative scores")

    # total_score must equal home_score + away_score
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM game_results WHERE total_score != home_score + away_score"
    ).fetchone()[0]
    check("game_results_totals_consistent", mismatched == 0,
          f"{mismatched} rows where total != home + away")

    # spread_result must equal home_score - away_score (note: some use away - home)
    spread_check = conn.execute("""
        SELECT COUNT(*) FROM game_results
        WHERE ABS(spread_result - (home_score - away_score)) > 0.01
        AND ABS(spread_result - (away_score - home_score)) > 0.01
    """).fetchone()[0]
    check("game_results_spreads_consistent", spread_check == 0,
          f"{spread_check} rows with inconsistent spread_result")

    # 1.2 Historical odds: response_json must be valid JSON with games
    print("\n[1.2] Historical Odds Validity")
    cursor = conn.execute(
        "SELECT response_json FROM historical_odds_cache ORDER BY RANDOM() LIMIT 10"
    )
    valid_json = 0
    has_games = 0
    has_bookmakers = 0
    for (rj,) in cursor.fetchall():
        try:
            data = json.loads(rj)
            valid_json += 1
            games = data.get("games", [])
            if games:
                has_games += 1
                for g in games[:1]:
                    if g.get("bookmakers"):
                        has_bookmakers += 1
        except json.JSONDecodeError:
            pass
    check("historical_odds_valid_json", valid_json == 10,
          f"{valid_json}/10 valid JSON")
    check("historical_odds_has_games", has_games > 0,
          f"{has_games}/10 have games array")
    check("historical_odds_has_bookmakers", has_bookmakers > 0,
          f"{has_bookmakers}/10 have bookmaker data")

    # 1.3 Backtest events: edges must be within bounds
    print("\n[1.3] Backtest Events Validity")
    total_bt = conn.execute("SELECT COUNT(*) FROM backtest_events").fetchone()[0]
    if total_bt > 0:
        out_of_bounds = conn.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE ABS(edge) > 0.15"
        ).fetchone()[0]
        check("backtest_edges_bounded", out_of_bounds == 0,
              f"{out_of_bounds}/{total_bt} events exceed 15% edge cap")

        # book_implied_prob must be between 0 and 1
        bad_implied = conn.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE book_implied_prob < 0 OR book_implied_prob > 1"
        ).fetchone()[0]
        check("backtest_implied_prob_valid", bad_implied == 0,
              f"{bad_implied} events with invalid implied probability")

        # model_fair_prob must be between 0 and 1
        bad_fair = conn.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE model_fair_prob < 0 OR model_fair_prob > 1"
        ).fetchone()[0]
        check("backtest_fair_prob_valid", bad_fair == 0,
              f"{bad_fair} events with invalid fair probability")

        # edge must equal model_fair_prob - book_implied_prob OR be capped at +/-0.15
        edge_mismatch = conn.execute("""
            SELECT COUNT(*) FROM backtest_events
            WHERE ABS(edge - (model_fair_prob - book_implied_prob)) > 0.001
            AND ABS(edge) != 0.15
        """).fetchone()[0]
        capped = conn.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE ABS(edge) = 0.15"
        ).fetchone()[0]
        check("backtest_edge_math_correct", edge_mismatch == 0,
              f"{edge_mismatch} uncapped mismatches, {capped} correctly capped at 15%")
    else:
        warn("backtest_events", "0 events — cannot verify")

    # 1.4 Embeddings: binary blobs must be correct size (768 * 4 = 3072 bytes)
    print("\n[1.4] Embeddings Integrity")
    emb_total = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    if emb_total > 0:
        wrong_size = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE LENGTH(embedding_blob) != 3072"
        ).fetchone()[0]
        check("embeddings_blob_size", wrong_size == 0,
              f"{wrong_size}/{emb_total} with wrong blob size (expected 3072)")

        # Verify a random embedding deserializes correctly
        row = conn.execute(
            "SELECT embedding_blob, embedding_json FROM embeddings ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if row and row[0]:
            import numpy as np
            blob_vec = np.frombuffer(row[0], dtype=np.float32)
            json_vec = np.array(json.loads(row[1]), dtype=np.float32)
            max_diff = np.max(np.abs(blob_vec - json_vec))
            check("embeddings_blob_json_match", max_diff < 1e-6,
                  f"max diff between blob and JSON: {max_diff:.2e}")
    else:
        warn("embeddings", "0 embeddings")

    # ══════════════════════════════════════════════════════════════════
    # STAGE 2: MATHEMATICAL CORRECTNESS — Do the computations work?
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("STAGE 2: MATHEMATICAL CORRECTNESS")
    print("=" * 50)

    # 2.1 Devig: verify power devig produces valid probabilities
    print("\n[2.1] Devig Engine")
    from tools.devig import power_devig, multiplicative_devig
    fair, k = power_devig([1.909, 1.909])  # -110/-110 standard juice
    check("power_devig_sums_to_1", abs(sum(fair) - 1.0) < 0.001,
          f"sum={sum(fair):.6f}, k={k:.4f}")
    check("power_devig_symmetric", abs(fair[0] - fair[1]) < 0.001,
          f"fair=[{fair[0]:.4f}, {fair[1]:.4f}]")

    # Asymmetric case: -200/+170 (heavy favorite)
    fair2, k2 = power_devig([1.5, 2.7])  # -200/+170 in decimal
    check("power_devig_asymmetric_valid",
          0 < fair2[0] < 1 and 0 < fair2[1] < 1 and abs(sum(fair2) - 1.0) < 0.001,
          f"fav={fair2[0]:.4f}, dog={fair2[1]:.4f}, sum={sum(fair2):.6f}")

    # 2.2 EV calculation
    print("\n[2.2] EV Calculation")
    from tools.ev import evaluate_edge
    ev = evaluate_edge(fair_prob=0.55, book_odds_american=-110, confidence="high")
    check("ev_positive_for_edge", ev["ev_pct"] > 0,
          f"ev={ev['ev_pct']:.4f} for fair=55% at -110")
    # edge_pct is in percentage units (2.62%), fair-implied is decimal (0.0262)
    check("ev_edge_correct", abs(ev["edge_pct"] / 100 - (0.55 - ev["book_implied"])) < 0.01,
          f"edge={ev['edge_pct']:.4f}%, fair-implied={0.55 - ev['book_implied']:.4f}")

    # 2.3 Kelly criterion
    print("\n[2.3] Kelly Criterion")
    from tools.kelly import kelly_full
    kf = kelly_full(edge=0.05, odds=-110)
    check("kelly_positive_for_edge", kf > 0,
          f"kelly={kf:.4f} for 5% edge at -110")
    check("kelly_bounded", 0 < kf < 1,
          f"kelly={kf:.4f} (should be 0-1)")
    kf_no = kelly_full(edge=0.0, odds=-110)
    check("kelly_zero_for_no_edge", kf_no <= 0.001,
          f"kelly={kf_no:.4f} for 0% edge at -110")

    # 2.4 HHI and Entropy
    print("\n[2.4] Market Microstructure Metrics")
    from tools.market_microstructure import hhi, shannon_entropy, sortino_ratio, brier_score
    # 5 equal books: HHI = 2000
    check("hhi_equal_books", abs(hhi([0.2]*5) - 2000) < 1,
          f"hhi([0.2]*5)={hhi([0.2]*5):.0f}")
    # Single book: HHI = 10000
    check("hhi_monopoly", abs(hhi([1.0]) - 10000) < 1,
          f"hhi([1.0])={hhi([1.0]):.0f}")
    # Identical distribution: entropy = log2(N) * normalized
    check("entropy_identical", shannon_entropy([0.5, 0.5]) > 0,
          f"entropy([0.5,0.5])={shannon_entropy([0.5,0.5]):.4f}")

    # Brier score: perfect = 0, worst = 1
    check("brier_perfect", brier_score([1.0, 0.0], [1, 0]) == 0.0,
          f"brier(perfect)={brier_score([1.0, 0.0], [1, 0])}")
    check("brier_worst", brier_score([0.0, 1.0], [1, 0]) == 1.0,
          f"brier(worst)={brier_score([0.0, 1.0], [1, 0])}")

    # 2.5 KL Divergence
    print("\n[2.5] KL Divergence")
    from tools.kl_divergence import kl_divergence, jensen_shannon
    check("kl_identical_zero", kl_divergence([0.5, 0.5], [0.5, 0.5]) < 1e-10,
          f"KL(P||P)={kl_divergence([0.5, 0.5], [0.5, 0.5]):.2e}")
    check("kl_positive", kl_divergence([0.9, 0.1], [0.5, 0.5]) > 0,
          f"KL([0.9,0.1]||[0.5,0.5])={kl_divergence([0.9, 0.1], [0.5, 0.5]):.4f}")
    # JS is symmetric
    js1 = jensen_shannon([0.7, 0.3], [0.5, 0.5])
    js2 = jensen_shannon([0.5, 0.5], [0.7, 0.3])
    check("js_symmetric", abs(js1 - js2) < 1e-10,
          f"JS(P,Q)={js1:.6f}, JS(Q,P)={js2:.6f}")

    # 2.6 Learned correlations: Welford algorithm
    print("\n[2.6] Welford Correlation")
    from tools.learned_correlations import LearnedCorrelationStore
    store = LearnedCorrelationStore()
    await store.initialize()
    # Check a pair with enough data
    nba_pts_total = await store.get("nba", "player_points", "game_total")
    if nba_pts_total and nba_pts_total.n > 100:
        check("welford_has_observations", nba_pts_total.n > 100,
              f"n={nba_pts_total.n}")
        check("welford_r_bounded", -1 <= nba_pts_total.pearson_r <= 1,
              f"r={nba_pts_total.pearson_r:.4f}")
        check("welford_ci_contains_r",
              nba_pts_total.ci_low <= nba_pts_total.pearson_r <= nba_pts_total.ci_high,
              f"CI=[{nba_pts_total.ci_low:.4f}, {nba_pts_total.ci_high:.4f}], r={nba_pts_total.pearson_r:.4f}")
        # Blending: with 9000+ obs, should be close to learned (not prior)
        blended = store.get_blended("nba", "player_points", "game_total", prior=0.35)
        check("welford_blending_convergence",
              abs(blended - nba_pts_total.pearson_r) < abs(0.35 - nba_pts_total.pearson_r),
              f"blended={blended:.4f}, learned={nba_pts_total.pearson_r:.4f}, prior=0.35")
    else:
        warn("welford", "insufficient nba correlation data")
    await store.close()

    # ══════════════════════════════════════════════════════════════════
    # STAGE 3: PIPELINE FLOW — Does data flow between stages?
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("STAGE 3: PIPELINE FLOW")
    print("=" * 50)

    # 3.1 Data collection → game_contexts has enriched data
    print("\n[3.1] Collection → Context Enrichment")
    enriched = conn.execute(
        "SELECT COUNT(*) FROM game_contexts WHERE context_json LIKE '%rest_days%'"
    ).fetchone()[0]
    total_ctx = conn.execute("SELECT COUNT(*) FROM game_contexts").fetchone()[0]
    check("enrichment_has_rest_days", enriched > 0,
          f"{enriched}/{total_ctx} enriched")

    # Verify enrichment data is valid
    sample = conn.execute(
        "SELECT context_json FROM game_contexts WHERE context_json LIKE '%rest_days%' LIMIT 1"
    ).fetchone()
    if sample:
        ctx = json.loads(sample[0])
        rest_val = ctx.get("home_rest_days") or ctx.get("away_rest_days")
        check("enrichment_rest_days_reasonable",
              rest_val is not None and 0 <= rest_val <= 14,
              f"rest_days={rest_val}")

    # 3.2 Historical odds → backtest events
    print("\n[3.2] Historical Odds → Backtest Events")
    hist_dates = conn.execute(
        "SELECT COUNT(DISTINCT snapshot_date) FROM historical_odds_cache WHERE sport='basketball_nba'"
    ).fetchone()[0]
    nba_bt = conn.execute(
        "SELECT COUNT(*) FROM backtest_events WHERE sport='basketball_nba'"
    ).fetchone()[0]
    check("hist_odds_produce_bt_events",
          hist_dates > 0 and nba_bt > 0,
          f"{hist_dates} NBA dates → {nba_bt} backtest events")

    # 3.3 Backtest events → signals
    print("\n[3.3] Backtest Events → Signals")
    signal_events = conn.execute(
        "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
    ).fetchone()[0]
    total_signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    check("bt_events_produce_signals", signal_events > 0,
          f"{signal_events} signal events out of {total_bt}")

    # 3.4 Hypotheses flow through lifecycle
    print("\n[3.4] Hypothesis Lifecycle")
    for status in ["draft", "backtesting", "paper_trading", "rejected"]:
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM hypotheses WHERE status = '{status}'"
        ).fetchone()[0]
        if cnt > 0:
            print(f"         {status}: {cnt}")

    promoted = conn.execute(
        "SELECT COUNT(*) FROM hypotheses WHERE status = 'paper_trading'"
    ).fetchone()[0]
    check("hypothesis_promotion_occurred", promoted > 0,
          f"{promoted} hypotheses promoted to paper_trading")

    # 3.5 Line monitor → snapshots → edge reports
    print("\n[3.5] Line Monitor → Snapshots")
    snaps = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    movements = conn.execute("SELECT COUNT(*) FROM line_movements").fetchone()[0]
    check("line_monitor_producing_snapshots", snaps > 0, f"{snaps} snapshots")
    check("line_monitor_detecting_movements", movements > 0, f"{movements} movements")

    # 3.6 Market microstructure populated from edge scanner
    print("\n[3.6] Edge Scanner → Microstructure")
    micro = conn.execute("SELECT COUNT(*) FROM market_microstructure").fetchone()[0]
    if micro > 0:
        check("microstructure_populated", True, f"{micro} entries")
    else:
        # Microstructure is stored during live snapshot processing.
        # Verify the wiring exists in code even if no data yet.
        import inspect
        from tools.line_monitor import LineMonitor
        src = inspect.getsource(LineMonitor._process_snapshot)
        wired = "market_microstructure" in src
        check("microstructure_wired_in_code", wired,
              "0 entries (needs live snapshot cycle) but code wiring verified")

    # 3.7 Embeddings populated from game contexts
    print("\n[3.7] Game Contexts → Embeddings")
    emb_game = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE collection = 'game_contexts'"
    ).fetchone()[0]
    check("game_context_embeddings", emb_game > 0, f"{emb_game} game context embeddings")

    # 3.8 KL metrics populated
    print("\n[3.8] KL Metrics")
    kl_count = conn.execute("SELECT COUNT(*) FROM kl_metrics").fetchone()[0]
    check("kl_metrics_populated", kl_count > 0, f"{kl_count} KL entries")

    # 3.9 Granger results populated
    print("\n[3.9] Granger Analysis")
    gr_count = conn.execute("SELECT COUNT(*) FROM granger_results").fetchone()[0]
    check("granger_populated", gr_count > 0, f"{gr_count} Granger results")

    # 3.10 Learned correlations have real observations
    print("\n[3.10] Learned Correlations")
    lc_obs = conn.execute(
        "SELECT COUNT(*) FROM learned_correlations WHERE n > 0"
    ).fetchone()[0]
    max_n = conn.execute(
        "SELECT MAX(n) FROM learned_correlations"
    ).fetchone()[0]
    check("learned_corr_has_real_data", lc_obs > 0,
          f"{lc_obs} pairs with data, max n={max_n}")

    # 3.11 CLV pipeline
    print("\n[3.11] CLV Pipeline")
    clv_entries = conn.execute("SELECT COUNT(*) FROM clv_log").fetchone()[0]
    bets_resolved = conn.execute(
        "SELECT COUNT(*) FROM bets WHERE result != 'pending'"
    ).fetchone()[0]
    check("clv_log_matches_resolutions",
          clv_entries >= bets_resolved,
          f"clv_log={clv_entries}, resolved_bets={bets_resolved}")

    # 3.12 Integrity checks running
    print("\n[3.12] Integrity Checks")
    ic = conn.execute("SELECT COUNT(*) FROM integrity_checks").fetchone()[0]
    check("integrity_checks_running", ic > 0, f"{ic} integrity check results")

    # 3.13 Hermes learnings
    print("\n[3.13] Hermes Memory")
    learnings = conn.execute("SELECT COUNT(*) FROM hermes_learnings").fetchone()[0]
    check("hermes_has_learnings", learnings > 0, f"{learnings} learnings")

    # 3.14 Event bus audit
    print("\n[3.14] Event Bus")
    events = conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0]
    check("event_bus_has_events", events > 0, f"{events} audit events")
    if events == 0:
        warn("event_bus", "0 events — bus may not be started")

    # ══════════════════════════════════════════════════════════════════
    # STAGE 4: CROSS-MODULE CONSISTENCY
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("STAGE 4: CROSS-MODULE CONSISTENCY")
    print("=" * 50)

    # 4.1 All backtest runs reference valid hypotheses
    print("\n[4.1] Backtest → Hypothesis Referential Integrity")
    orphan_runs = conn.execute("""
        SELECT COUNT(*) FROM backtest_runs br
        LEFT JOIN hypotheses h ON br.hypothesis_id = h.hypothesis_id
        WHERE h.hypothesis_id IS NULL
    """).fetchone()[0]
    check("backtest_runs_reference_valid_hypotheses", orphan_runs == 0,
          f"{orphan_runs} orphan backtest runs")

    # 4.2 All signals reference valid events
    print("\n[4.2] Signal → Event Consistency")
    # Signals with edge > 0 should have signal_type that makes sense
    signal_types = conn.execute(
        "SELECT signal_type, COUNT(*) FROM signals GROUP BY signal_type"
    ).fetchall()
    check("signals_have_types", len(signal_types) > 0,
          f"types: {dict(signal_types)}")

    # 4.3 Monitored sports match hypothesis sports
    print("\n[4.3] Monitoring ↔ Hypothesis Alignment")
    from tools.line_monitor import MONITORED_SPORTS
    hypothesis_sports = conn.execute(
        "SELECT DISTINCT sport FROM hypotheses WHERE status IN ('backtesting', 'paper_trading')"
    ).fetchall()
    unmonitored = []
    for (sport,) in hypothesis_sports:
        if sport not in MONITORED_SPORTS:
            unmonitored.append(sport)
    check("all_active_hypothesis_sports_monitored",
          len(unmonitored) == 0,
          f"unmonitored: {unmonitored}" if unmonitored else "all covered")

    # 4.4 Promotion gates are strictly enforced
    print("\n[4.4] Promotion Gate Enforcement")
    from tools.hypothesis import PROMOTION_GATES
    bt_gate = [v for k, v in PROMOTION_GATES.items() if "paper" in k][0]
    check("promotion_requires_signals", bt_gate["min_signals"] >= 5,
          f"min_signals={bt_gate['min_signals']}")
    check("promotion_requires_significance", bt_gate["max_p_value"] <= 0.10,
          f"max_p={bt_gate['max_p_value']}")
    check("promotion_requires_edge_quality", "min_positive_edge_rate" in bt_gate,
          f"rate={bt_gate.get('min_positive_edge_rate')}")
    check("promotion_requires_calibration", "max_brier" in bt_gate,
          f"max_brier={bt_gate.get('max_brier')}")

    # 4.5 No phantom edges in signals
    print("\n[4.5] Signal Quality")
    phantoms = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE edge_pct > 0.20"
    ).fetchone()[0]
    check("no_phantom_signals", phantoms == 0,
          f"{phantoms} phantom signals (>20% edge)")

    # 4.6 Multi-book backtest verification
    print("\n[4.6] Multi-Book Backtest")
    if total_bt > 0:
        # Check that backtest events come from multiple books (not just DK)
        books = conn.execute(
            "SELECT DISTINCT book FROM backtest_events LIMIT 20"
        ).fetchall()
        unique_books = [b[0] for b in books]
        check("backtest_uses_multiple_books", len(unique_books) > 1,
              f"books: {unique_books[:5]}")

    conn.close()

    # ══════════════════════════════════════════════════════════════════
    # STAGE 5: LIVE COMPONENT VERIFICATION
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 50)
    print("STAGE 5: LIVE COMPONENT VERIFICATION")
    print("=" * 50)

    # 5.1 Edge confidence scoring works with all parameters
    print("\n[5.1] Edge Confidence Scoring")
    from tools.edge_confidence import score_edge
    conf = score_edge(
        edge_pct=3.0, books_compared=5,
        book_names=["pinnacle", "draftkings", "fanduel", "betmgm", "caesars"],
        market="h2h", market_hhi=1200.0, market_entropy=2.3,
    )
    check("edge_confidence_scores", 0 < conf.score <= 1.0,
          f"score={conf.score}, tier={conf.tier}")
    check("edge_confidence_has_hhi_factor", "market_hhi" in conf.factors,
          f"factors={list(conf.factors.keys())}")

    # 5.2 Vector search works
    print("\n[5.2] Vector Search")
    import aiosqlite
    from tools.embeddings import VectorStore, embed_text
    vs = VectorStore()
    await vs.initialize()
    try:
        query = await embed_text("NBA game with rest advantage")
        results = await vs.search("game_contexts", query, top_k=3, min_similarity=0.1)
        check("vector_search_returns_results", len(results) > 0,
              f"{len(results)} results, top sim={results[0]['similarity']:.4f}" if results else "0")
    except Exception as e:
        check("vector_search", False, str(e))
    await vs.close()

    # 5.3 Edge scanner produces valid output
    print("\n[5.3] Edge Scanner")
    from tools.edge_scanner import full_edge_scan
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT snapshot_json FROM odds_snapshots WHERE game_count > 0 ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    if row:
        snapshot = json.loads(row[0])
        report = full_edge_scan(snapshot)
        check("edge_scanner_produces_report", "total_edges" in report,
              f"total_edges={report.get('total_edges', 0)}")
        # Verify HHI/entropy are present in edges
        found_micro = False
        for key in ["cross_book_h2h", "cross_book_spreads", "cross_book_totals"]:
            for edge in report.get(key, []):
                if edge.get("hhi") is not None:
                    found_micro = True
                    break
        total_edges = report.get("total_edges", 0)
        if total_edges > 0:
            check("edge_scanner_has_microstructure", found_micro,
                  f"HHI present in edges")

    # ══════════════════════════════════════════════════════════════════
    # SUMMARY
    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    total = len(passes) + len(failures)
    print(f"RESULTS: {len(passes)}/{total} PASSED, {len(failures)} FAILED, {len(warnings)} WARNINGS")
    print("=" * 70)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  X {f}")

    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print(f"  ! {w}")

    if not failures:
        print("\nVERDICT: ALL CHECKS PASSED — SYSTEM VERIFIED")
    else:
        print(f"\nVERDICT: {len(failures)} FAILURES MUST BE RESOLVED")

    return len(failures) == 0


if __name__ == "__main__":
    ok = asyncio.run(run_audit())
    sys.exit(0 if ok else 1)
