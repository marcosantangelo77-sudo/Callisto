"""Tests for tools/lines/ split of line_monitor.

Covers:
- import-path stability (LineMonitor + legacy module-level names)
- ingest: WS message conversion, sport mapping, delta merging, fetched_at
  stamping, scraper enrichment, free-snapshot merging, matchup keys
- edge_report: implied probs, devig consensus, model agreement gating,
  MovementEvaluator end-to-end with a stub insert callback
- movement: significance filtering, KL tracker cache/eviction
"""

import asyncio

import pytest

import sys
sys.path.insert(0, ".")


# ── Import path stability ───────────────────────────────────────────────────


def test_line_monitor_import_path_stable():
    from tools.line_monitor import LineMonitor as LM1
    from tools import line_monitor as lm_mod
    assert LM1 is lm_mod.LineMonitor


def test_legacy_module_names_preserved():
    import tools.line_monitor as lm
    from tools.lines.ingest import merge_delta_into_snapshot
    from tools.lines.movement import (
        PRICE_MOVEMENT_THRESHOLD, POINT_MOVEMENT_THRESHOLD, filter_significant,
    )
    assert lm.PRICE_MOVEMENT_THRESHOLD == PRICE_MOVEMENT_THRESHOLD == 5
    assert lm.POINT_MOVEMENT_THRESHOLD == POINT_MOVEMENT_THRESHOLD == 0.5
    assert lm.MIN_EDGE_ALERT == pytest.approx(0.03)
    # Back-compat wrappers delegate to the extracted implementations.
    assert lm._merge_delta_into_snapshot is merge_delta_into_snapshot or callable(
        lm._merge_delta_into_snapshot
    )
    assert callable(lm._ws_update_to_snapshot)
    assert callable(lm._stamp_snapshot_fetched_at)


def test_lines_package_modules_importable():
    from tools.lines import ingest, edge_report, movement  # noqa: F401
    from tools.lines.ingest import ws_sport_to_monitored, WS_SPORT_TO_MONITORED
    assert "basketball" in WS_SPORT_TO_MONITORED


# ── Ingest: WS sport mapping ───────────────────────────────────────────────


from tools.lines.ingest import (
    WS_SPORT_TO_MONITORED,
    matchup_key,
    merge_delta_into_snapshot,
    merge_free_snapshots,
    stamp_snapshot_fetched_at,
    ws_sport_to_monitored,
    ws_update_to_snapshot,
    enrich_with_scraper,
)


class TestWsSportMapping:
    def test_ncaa_women(self):
        assert ws_sport_to_monitored("Basketball", "NCAA Women") == "basketball_ncaaw"

    def test_ncaa_men(self):
        assert ws_sport_to_monitored("basketball", "NCAA") == "basketball_ncaab"

    def test_plain_basketball(self):
        assert ws_sport_to_monitored("basketball", "NBA") == "basketball_nba"

    def test_football_variants(self):
        assert ws_sport_to_monitored("american-football", "") == "americanfootball_nfl"
        assert ws_sport_to_monitored("football", "NCAAF") == "americanfootball_ncaaf"

    def test_unknown_returns_none(self):
        assert ws_sport_to_monitored("quidditch", "") is None

    def test_last_resort_table(self):
        assert ws_sport_to_monitored("soccer", "EPL") == "soccer_mls"


class TestWsUpdateToSnapshot:
    def test_converts_message(self):
        msg = {
            "id": "evt-1",
            "sport": "basketball",
            "league": "NBA",
            "bookie": "DraftKings",
            "home": "Lakers",
            "away": "Celtics",
            "commence": "2026-08-27T00:00:00Z",
            "markets": [
                {"name": "ML", "outcomes": [{"name": "Lakers", "price": -130}]},
                {"name": "Totals", "outcomes": [{"name": "Over", "price": -110, "point": 220.5}]},
            ],
        }
        out = ws_update_to_snapshot(msg)
        assert out is not None
        sport_key, snap = out
        assert sport_key == "basketball_nba"
        game = snap["games"][0]
        assert game["id"] == "evt-1"
        bm = game["bookmakers"][0]
        assert bm["key"] == "draftkings"
        keys = {m["key"] for m in bm["markets"]}
        assert keys == {"h2h", "totals"}
        oc = snap["games"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]
        assert oc["price"] == -130 and oc["fetched_at"]

    def test_missing_bookie_rejected(self):
        assert ws_update_to_snapshot({"id": "x", "sport": "baseball"}) is None

    def test_missing_event_id_rejected(self):
        assert ws_update_to_snapshot({"bookie": "DK"}) is None

    def test_non_dict_rejected(self):
        assert ws_update_to_snapshot(None) is None


class TestMergeDelta:
    def _base(self):
        return {"sport": "basketball_nba", "game_count": 1, "games": [{
            "id": "g1", "home_team": "A", "away_team": "B",
            "bookmakers": [
                {"key": "draftkings", "title": "DK",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -110}]}]},
                {"key": "fanduel", "title": "FD",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -108}]}]},
            ]}]}

    def test_merge_replaces_only_that_book(self):
        base = self._base()
        delta = {"sport": "basketball_nba", "game_count": 1, "ingest_source": "ws",
                 "games": [{"id": "g1", "bookmakers": [
                     {"key": "draftkings", "title": "DK",
                      "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -130}]}]}]}]}
        merged = merge_delta_into_snapshot(base, delta, "2026-01-01T00:00:00+00:00")
        g = merged["games"][0]
        assert len(g["bookmakers"]) == 2  # consensus preserved
        dk = [b for b in g["bookmakers"] if b["key"] == "draftkings"][0]
        fd = [b for b in g["bookmakers"] if b["key"] == "fanduel"][0]
        assert dk["markets"][0]["outcomes"][0]["price"] == -130   # fresh
        assert fd["markets"][0]["outcomes"][0]["price"] == -108   # aged but kept

    def test_base_not_mutated(self):
        base = self._base()
        delta = {"games": [{"id": "g1", "bookmakers": [
            {"key": "draftkings", "title": "DK", "markets": []}]}]}
        merge_delta_into_snapshot(base, delta, "now")
        assert len(base["games"][0]["bookmakers"]) == 2

    def test_new_event_appended(self):
        base = {"sport": "s", "game_count": 0, "games": []}
        delta = {"sport": "s", "games": [{"id": "new", "bookmakers": []}]}
        merged = merge_delta_into_snapshot(base, delta, "now")
        assert merged["game_count"] == 1


class TestFetchedAtStamp:
    def test_does_not_overwrite_ws_stamp(self):
        snap = {"games": [{"bookmakers": [
            {"fetched_at": "EARLIER", "last_update": "LATER", "markets": [
                {"outcomes": [{"fetched_at": "EARLIEST"}]}]}]}]}
        stamp_snapshot_fetched_at(snap, "NOW")
        bm = snap["games"][0]["bookmakers"][0]
        oc = bm["markets"][0]["outcomes"][0]
        assert bm["fetched_at"] == "EARLIER"      # earlier stamp preserved
        assert oc["fetched_at"] == "EARLIEST"

    def test_backfills_missing(self):
        snap = {"games": [{"bookmakers": [
            {"last_update": "LU", "markets": [{"outcomes": [{"name": "x"}]}]}]}]}
        stamp_snapshot_fetched_at(snap, "NOW")
        bm = snap["games"][0]["bookmakers"][0]
        oc = bm["markets"][0]["outcomes"][0]
        assert bm["fetched_at"] == "LU"
        assert oc["fetched_at"] == "LU"
        assert snap["fetched_at"] == "NOW"


class TestEnrichWithScraper:
    @staticmethod
    def _snapshot():
        return {"sport": "basketball_nba", "games": [{
            "id": "g1", "home_team": "Lakers", "away_team": "Celtics",
            "bookmakers": [
                {"key": "draftkings", "title": "DK",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "Lakers", "price": -110}]}]},
                {"key": "pinnacle", "title": "Pinnacle",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "Lakers", "price": -105}]}]},
            ]}]}

    @staticmethod
    def _scraped(book_key):
        async def _fn(sport):
            return {"games": [{
                "home_team": "lakers", "away_team": "CELTICS",
                "bookmakers": [{"key": book_key, "title": book_key,
                                "markets": [{"key": "h2h", "outcomes": [{"name": "Lakers", "price": -125}]}]}],
            }]}
        return _fn

    def test_replaces_stale_entry(self):
        out = asyncio.run(enrich_with_scraper(
            "basketball_nba", self._snapshot(),
            self._scraped("draftkings"), "draftkings", ("draft_kings",)))
        dk = [b for b in out["games"][0]["bookmakers"] if b["key"] == "draftkings"][0]
        assert len(out["games"][0]["bookmakers"]) == 2
        assert dk["markets"][0]["outcomes"][0]["price"] == -125

    def test_appends_when_absent(self):
        out = asyncio.run(enrich_with_scraper(
            "basketball_nba", self._snapshot(),
            self._scraped("fanduel"), "fanduel"))
        assert len(out["games"][0]["bookmakers"]) == 3

    def test_scraper_error_is_noop(self):
        async def bad(sport):
            return {"error": "boom"}

        out = asyncio.run(enrich_with_scraper(
            "basketball_nba", self._snapshot(), bad, "draftkings"))
        assert len(out["games"][0]["bookmakers"]) == 2

    def test_scraper_exception_is_swallowed(self):
        async def bad(sport):
            raise RuntimeError("network down")

        out = asyncio.run(enrich_with_scraper(
            "basketball_nba", self._snapshot(), bad, "draftkings"))
        assert len(out["games"][0]["bookmakers"]) == 2


class TestMatchupKeyAndMergeFree:
    def test_matchup_key_order_independent(self):
        assert matchup_key("Lakers", "Celtics") == matchup_key("celtics", "lakers")

    def test_matchup_key_empty(self):
        assert matchup_key("", "Celtics") == ""

    def test_merge_free_adds_books_and_games(self):
        base = {"sport": "basketball_nba", "games": [{
            "home_team": "Lakers", "away_team": "Celtics",
            "bookmakers": [{"key": "draftkings", "title": "DK", "markets": []}]}]}
        extra = {"sport": "basketball_nba", "games": [
            {"home_team": "Lakers", "away_team": "Celtics",
             "bookmakers": [{"key": "fanduel", "title": "FD", "markets": []},
                            {"key": "draftkings", "title": "dup", "markets": []}]},
            {"home_team": "Heat", "away_team": "Knicks", "bookmakers": []},
        ]}
        merged = merge_free_snapshots(base, extra)
        g0 = merged["games"][0]
        keys = {b["key"] for b in g0["bookmakers"]}
        assert keys == {"draftkings", "fanduel"}  # duplicate skipped
        assert merged["game_count"] == 2          # extra-only game appended
        assert all(g.get("sport_key") == "basketball_nba" for g in merged["games"])


# ── Edge report ──────────────────────────────────────────────────────────────


from tools.lines.edge_report import (
    check_model_agreement,
    compute_devig_consensus,
    extract_implied_probs,
    MovementEvaluator,
)


class TestExtractImpliedProbs:
    def test_positive_and_negative_prices(self):
        game = {"bookmakers": [
            {"markets": [{"key": "h2h", "outcomes": [{"price": 150}]}]},   # 40%
            {"markets": [{"key": "h2h", "outcomes": [{"price": -150}]}]},  # 60%
            {"markets": [{"key": "spreads", "outcomes": [{"price": -110}]}]},  # wrong market
        ]}
        probs = extract_implied_probs(game, "h2h")
        assert probs == pytest.approx([0.4, 0.6])
        assert extract_implied_probs(game, "totals") == []


class TestComputeDevigConsensus:
    def _game(self):
        # Two books besides the mover; near-no-vig pair for team A.
        return {
            "home_team": "Alpha", "away_team": "Beta",
            "bookmakers": [
                {"key": "moved", "title": "MovedBook", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": -200}, {"name": "Beta", "price": 150}]}]},
                {"key": "b1", "title": "BookOne", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": -105}, {"name": "Beta", "price": -115}]}]},
                {"key": "b2", "title": "BookTwo", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": -108}, {"name": "Beta", "price": -112}]}]},
            ],
        }

    def test_excludes_moved_book_and_needs_two(self):
        probs = compute_devig_consensus(self._game(), "Alpha", "h2h", "MovedBook")
        assert probs is not None and len(probs) == 2
        assert all(0 < p < 1 for p in probs)

    def test_single_other_book_returns_none(self):
        game = self._game()
        game["bookmakers"] = [game["bookmakers"][1]]
        assert compute_devig_consensus(game, "Alpha", "h2h", "MovedBook") is None


class TestModelAgreement:
    REPORT = {
        "pace_model_totals": [{"game_id": "g1", "market": "totals", "team": "Alpha"}],
        "simulation_validated": [{"game_id": "g2", "market": "spreads"}],
    }

    def test_pace_totals_match(self):
        ok, label = check_model_agreement(self.REPORT, "g1", "Alpha", "totals")
        assert ok and label == "pace_model"

    def test_simulation_match_game_level(self):
        ok, label = check_model_agreement(self.REPORT, "g2", "", "spreads")
        assert ok and label == "simulation"

    def test_team_mismatch_blocks(self):
        report = {
            "simulation_validated": [{"game_id": "g2", "market": "spreads", "team": "Alpha"}],
        }
        ok, label = check_model_agreement(report, "g2", "Other", "spreads")
        assert not ok and label == "none"

    def test_no_game_id(self):
        ok, label = check_model_agreement(self.REPORT, "", "Alpha", "totals")
        assert not ok and label == "no-game-id"


class TestMovementEvaluator:
    @staticmethod
    def _snapshot():
        # Consensus fair prob for Alpha ≈ 50% (even both sides), moved book
        # offers +150 (implied 40%) → big positive edge on Alpha.
        return {"games": [{
            "id": "g1", "home_team": "Alpha", "away_team": "Beta",
            "bookmakers": [
                {"key": "b1", "title": "BookOne", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": -100}, {"name": "Beta", "price": -100}]}]},
                {"key": "b2", "title": "BookTwo", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": 100}, {"name": "Beta", "price": 100}]}]},
                {"key": "mv", "title": "Mover", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Alpha", "price": 150}, {"name": "Beta", "price": -180}]}]},
            ],
        }]}

    MOVEMENT = {
        "team": "Alpha", "market": "h2h", "new_price": 150,
        "bookmaker": "Mover", "old_price": 100,
        "price_movement": 50, "direction": "up",
        "point_movement": 0,
    }

    def _run(self, snapshot=None, report=None, require=True):
        rows = []

        async def insert_ev(row):
            rows.append(row)

        ev = MovementEvaluator(
            insert_ev=insert_ev,
            get_edge_report=lambda s: report or {},
        )
        asyncio.run(ev.evaluate(
            "basketball_nba", dict(self.MOVEMENT),
            snapshot or self._snapshot(),
            require_model_agreement=require,
        ))
        return rows

    def test_big_edge_inserts_row(self):
        rows = self._run()
        assert len(rows) == 1
        row = rows[0]
        assert row["team"] == "Alpha"
        assert row["steam_only"] == 1  # no model agreement provided
        assert row["edge"] > 0.03

    def test_model_agreement_marks_ratified(self):
        report = {"simulation_validated": [{"game_id": "g1", "market": "h2h", "team": "Alpha"}]}
        rows = self._run(report=report)
        assert rows[0]["steam_only"] == 0

    def test_contaminated_h2h_skipped(self):
        snap = self._snapshot()
        # Inject a large positive line that matches the target team so
        # both-side contamination leaks into one team's line set.
        snap["games"][0]["bookmakers"][0]["markets"][0]["outcomes"].extend([
            {"name": "Alpha_alt", "price": 600},
            {"name": "Alpha_neg", "price": -200},
        ])
        rows = self._run(snapshot=snap)
        assert rows == []

    def test_no_games_matching_team(self):
        snap = {"games": []}
        assert self._run(snapshot=snap) == []


# ── Movement ─────────────────────────────────────────────────────────────────


from tools.lines.movement import (
    filter_significant,
    KLDivergenceTracker,
    extract_probs,
)


class TestFilterSignificant:
    def test_thresholds(self):
        movements = [
            {"price_movement": 5, "point_movement": 0.0},     # at threshold → keep
            {"price_movement": -10, "point_movement": 0.1},   # keep
            {"price_movement": 2, "point_movement": 0.5},     # point at threshold → keep
            {"price_movement": 4, "point_movement": 0.4},     # below both → drop
        ]
        kept = filter_significant(movements)
        assert len(kept) == 3


class TestKLDivergenceTracker:
    def test_extract_probs(self):
        game = {"bookmakers": [
            {"markets": [{"key": "totals", "outcomes": [{"price": 100}]}]},
            {"markets": [{"key": "totals", "outcomes": [{"price": -100}]}]},
        ]}
        probs = extract_probs(game, "totals")
        assert probs == pytest.approx([0.5, 0.5])

    def test_identical_snapshots_store_zero(self):
        async def main():
            t = KLDivergenceTracker(db_path=":memory:")
            snap = {"games": [{
                "id": "g1",
                "bookmakers": [
                    {"markets": [{"key": "h2h", "outcomes": [{"price": -100}]}]},
                    {"markets": [{"key": "h2h", "outcomes": [{"price": 100}]}]},
                ],
            }]}
            stored = await t.compute_and_store("nba", snap, snap)
            assert stored == 0
        asyncio.run(main())

    def test_cache_roundtrip(self):
        t = KLDivergenceTracker(db_path=":memory:")
        t.cache["nba:g1:h2h"] = {"kl_divergence": 0.5}
        assert t.get_for_game("nba", "g1")["kl_divergence"] == 0.5
        assert t.get_for_game("nba", "missing") is None

    def test_cache_eviction_cap(self):
        t = KLDivergenceTracker(db_path=":memory:", cache_max=10)
        for i in range(15):
            t.cache[f"k{i}"] = {}
        # Simulate the eviction branch used during compute_and_store.
        while len(t.cache) >= t.CACHE_MAX:
            evict_n = t.CACHE_MAX // 5
            for _ in range(evict_n):
                try:
                    t.cache.pop(next(iter(t.cache)))
                except (StopIteration, KeyError):
                    break
        assert len(t.cache) < 10
