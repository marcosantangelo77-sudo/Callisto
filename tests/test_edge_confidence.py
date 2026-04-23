"""Tests for AGP-compliant edge confidence scoring."""

import pytest
from tools.edge_confidence import score_edge, score_parlay, EdgeConfidence


class TestSourceClassDetection:
    def test_sharp_book_gets_primary(self):
        conf = score_edge(3.0, 4, ["Pinnacle", "FanDuel", "DraftKings", "BetMGM"], "h2h")
        assert conf.source_class == "PRIMARY"
        assert conf.ceiling == 1.0

    def test_lowvig_gets_primary(self):
        conf = score_edge(3.0, 3, ["LowVig.ag", "FanDuel", "DraftKings"], "h2h")
        assert conf.source_class == "PRIMARY"

    def test_soft_books_only_gets_secondary(self):
        conf = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert conf.source_class == "SECONDARY"
        assert conf.ceiling == 0.75

    def test_single_book_gets_signal(self):
        conf = score_edge(3.0, 1, ["DraftKings"], "h2h")
        assert conf.source_class == "SIGNAL"
        assert conf.ceiling == 0.55

    def test_no_books_gets_inferred(self):
        conf = score_edge(3.0, 0, [], "h2h")
        assert conf.source_class == "INFERRED"
        assert conf.ceiling == 0.55


class TestCeilingEnforcement:
    def test_secondary_cannot_exceed_075(self):
        # Even with massive edge and many books, no sharp = capped at 0.75
        conf = score_edge(10.0, 8, ["FanDuel", "DraftKings", "BetMGM", "BetRivers",
                                     "Bovada", "MyBookie", "PointsBet", "Caesars"], "h2h")
        assert conf.score <= 0.75
        assert conf.source_class == "SECONDARY"

    def test_signal_cannot_exceed_055(self):
        conf = score_edge(10.0, 1, ["DraftKings"], "h2h")
        assert conf.score <= 0.55

    def test_primary_can_reach_verified(self):
        conf = score_edge(5.0, 6, ["Pinnacle", "FanDuel", "DraftKings",
                                    "BetMGM", "BetRivers", "Bovada"],
                          "h2h", cross_method_confirmed=True)
        assert conf.score >= 0.90
        assert conf.tier == "VERIFIED"


class TestEdgeMagnitude:
    def test_strong_edge_scores_higher(self):
        strong = score_edge(5.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        weak = score_edge(1.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert strong.score > weak.score

    def test_sub_noise_edge_is_unverified_or_speculative(self):
        conf = score_edge(0.3, 2, ["FanDuel", "DraftKings"], "h2h")
        assert conf.tier in ("UNVERIFIED", "SPECULATIVE")

    def test_noise_floor(self):
        conf = score_edge(0.1, 2, ["FanDuel", "DraftKings"], "h2h")
        assert conf.score < 0.30


class TestMarketEfficiency:
    def test_prop_market_boosts_confidence(self):
        prop = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "player_points")
        main = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert prop.score >= main.score  # Props less efficient = edge more believable

    def test_alternate_market_highest_boost(self):
        # Use lower edge so raw scores stay below SECONDARY ceiling (0.75)
        alt = score_edge(1.5, 3, ["FanDuel", "DraftKings", "BetMGM"], "alternate_spreads")
        main = score_edge(1.5, 3, ["FanDuel", "DraftKings", "BetMGM"], "h2h")
        assert alt.score > main.score


class TestLiveAndTime:
    def test_live_penalty(self):
        pre = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h", is_live=False)
        live = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h", is_live=True)
        assert live.score < pre.score

    def test_near_tipoff_bonus(self):
        # Use lower edge so raw scores stay below SECONDARY ceiling
        early = score_edge(1.5, 3, ["FanDuel", "DraftKings", "BetMGM"],
                           "h2h", hours_to_game=48)
        late = score_edge(1.5, 3, ["FanDuel", "DraftKings", "BetMGM"],
                          "h2h", hours_to_game=0.25)
        assert late.score > early.score


class TestParlay:
    def test_parlay_limited_by_weakest_leg(self):
        strong = score_edge(5.0, 5, ["Pinnacle", "FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        weak = score_edge(1.0, 2, ["FanDuel", "DraftKings"], "h2h")
        parlay = score_parlay([strong, weak])
        assert parlay.score <= weak.score + 0.15  # Geo mean can lift slightly above min

    def test_uniform_legs_stay_near_leg_score(self):
        leg = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        parlay = score_parlay([leg, leg, leg])
        assert abs(parlay.score - leg.score) < 0.05

    def test_empty_parlay(self):
        parlay = score_parlay([])
        assert parlay.tier == "UNVERIFIED"
        assert parlay.score == 0.0

    def test_parlay_ceiling_from_weakest(self):
        primary_leg = score_edge(3.0, 3, ["Pinnacle", "FanDuel", "DraftKings"], "h2h")
        secondary_leg = score_edge(3.0, 3, ["FanDuel", "DraftKings", "BetMGM"], "h2h")
        parlay = score_parlay([primary_leg, secondary_leg])
        assert parlay.ceiling == 0.75  # SECONDARY ceiling

    def test_parlay_has_leg_scores(self):
        legs = [
            score_edge(3.0, 3, ["FanDuel", "DraftKings", "BetMGM"], "h2h"),
            score_edge(2.0, 3, ["FanDuel", "DraftKings", "BetMGM"], "spreads"),
        ]
        parlay = score_parlay(legs)
        assert len(parlay.factors["leg_scores"]) == 2


class TestEdgeConfidenceFields:
    def test_all_fields_present(self):
        conf = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert isinstance(conf.score, float)
        assert conf.tier in ("VERIFIED", "CORROBORATED", "PROBABLE", "SPECULATIVE", "UNVERIFIED")
        assert conf.source_class in ("PRIMARY", "SECONDARY", "SIGNAL", "INFERRED")
        assert isinstance(conf.ceiling, float)
        assert isinstance(conf.factors, dict)
        assert isinstance(conf.reasoning, str)
        assert len(conf.reasoning) > 0

    def test_score_within_bounds(self):
        conf = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert 0.0 <= conf.score <= 1.0
