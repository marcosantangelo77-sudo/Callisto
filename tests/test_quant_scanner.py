"""Unit tests for tools.quant.scanner._snapshot_rows_from_games.

The scanner's job is to reshape an odds-api-io ``games`` payload into
MarketSnapshot rows. The critical correctness requirement is that alt
lines MUST NOT conflate across books. If DK offers Cleveland -1.5 and
Pinnacle offers Cleveland -2.5 under the same ``outcome.name``, those
are different bets; grouping them feeds the consensus engine
apples-vs-oranges and manufactures impossible 10-25% "edges" on liquid
markets. These tests lock that invariant down.
"""

from __future__ import annotations

from tools.quant.scanner import _snapshot_rows_from_games


def _bm(book: str, market: str, outcomes: list[dict]) -> dict:
    return {
        "key": book,
        "last_update": "2026-04-18T20:00:00Z",
        "markets": [{"key": market, "outcomes": outcomes}],
    }


def _game(bookmakers: list[dict]) -> dict:
    return {
        "id": "E1",
        "home_team": "Cleveland Guardians",
        "away_team": "Baltimore Orioles",
        "commence_time": "2026-04-18T23:00:00Z",
        "bookmakers": bookmakers,
    }


def test_spreads_with_different_points_do_not_conflate_across_books():
    # DK offers Cleveland -1.5 (implied ~0.55)
    # Pinnacle offers Cleveland -2.5 (implied ~0.30)
    # These are different bets. The scanner must not feed them to a
    # single consensus — otherwise the consensus would be ~0.42, making
    # DK look like it has a ~13% edge, which is pure apples-vs-oranges.
    games = [_game([
        _bm("draftkings", "spreads", [
            {"name": "Cleveland Guardians", "price": -120, "point": -1.5},
            {"name": "Baltimore Orioles",  "price": +100, "point": +1.5},
        ]),
        _bm("pinnacle", "spreads", [
            {"name": "Cleveland Guardians", "price": +230, "point": -2.5},
            {"name": "Baltimore Orioles",  "price": -260, "point": +2.5},
        ]),
        _bm("fanduel", "spreads", [
            {"name": "Cleveland Guardians", "price": -115, "point": -1.5},
            {"name": "Baltimore Orioles",  "price": -105, "point": +1.5},
        ]),
    ])]

    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"draftkings"})

    # DK's Cleveland -1.5 consensus must only include fanduel's -1.5,
    # not pinnacle's -2.5. There should be exactly one snapshot for
    # Cleveland -1.5 from DK.
    dk_cle_snaps = [
        s for s in snaps
        if s.placement_line.book == "draftkings"
        and "Cleveland Guardians" in s.outcome
        and "-1.5" in s.outcome
    ]
    assert len(dk_cle_snaps) == 1
    snap = dk_cle_snaps[0]
    books_in_consensus = {bl.book for bl in snap.all_lines}
    assert books_in_consensus == {"draftkings", "fanduel"}
    assert "pinnacle" not in books_in_consensus


def test_totals_over_under_at_different_totals_do_not_conflate():
    # Over 8.5 and Over 9.0 are different bets even though both are
    # named "Over". The scanner must bucket them separately.
    games = [_game([
        _bm("draftkings", "totals", [
            {"name": "Over",  "price": -110, "point": 8.5},
            {"name": "Under", "price": -110, "point": 8.5},
        ]),
        _bm("pinnacle", "totals", [
            {"name": "Over",  "price": +120, "point": 9.0},
            {"name": "Under", "price": -140, "point": 9.0},
        ]),
    ])]
    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"draftkings"})
    # DK's Over 8.5 has only one book (itself) → len(book_lines) < 2 →
    # no snapshot emitted.
    assert snaps == []


def test_totals_same_total_across_books_does_pair():
    games = [_game([
        _bm("draftkings", "totals", [
            {"name": "Over",  "price": -110, "point": 8.5},
            {"name": "Under", "price": -110, "point": 8.5},
        ]),
        _bm("pinnacle", "totals", [
            {"name": "Over",  "price": -105, "point": 8.5},
            {"name": "Under", "price": -105, "point": 8.5},
        ]),
    ])]
    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"draftkings"})
    over_snaps = [s for s in snaps if s.outcome.endswith("Over 8.5")]
    assert len(over_snaps) == 1
    assert {bl.book for bl in over_snaps[0].all_lines} == {"draftkings", "pinnacle"}


def test_h2h_moneyline_groups_by_team_name_only():
    # h2h has no point — all books' prices for the same team must
    # aggregate, not split.
    games = [_game([
        _bm("draftkings", "h2h", [
            {"name": "Cleveland Guardians", "price": -140},
            {"name": "Baltimore Orioles",  "price": +120},
        ]),
        _bm("pinnacle", "h2h", [
            {"name": "Cleveland Guardians", "price": -135},
            {"name": "Baltimore Orioles",  "price": +125},
        ]),
        _bm("fanduel", "h2h", [
            {"name": "Cleveland Guardians", "price": -138},
            {"name": "Baltimore Orioles",  "price": +122},
        ]),
    ])]
    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"draftkings"})
    cle = [s for s in snaps if s.outcome.endswith("Cleveland Guardians")]
    assert len(cle) == 1
    assert {bl.book for bl in cle[0].all_lines} == {"draftkings", "pinnacle", "fanduel"}


def test_spreads_outcome_label_includes_signed_point():
    games = [_game([
        _bm("draftkings", "spreads", [
            {"name": "Cleveland Guardians", "price": -120, "point": -1.5},
            {"name": "Baltimore Orioles",  "price": +100, "point": +1.5},
        ]),
        _bm("pinnacle", "spreads", [
            {"name": "Cleveland Guardians", "price": -115, "point": -1.5},
            {"name": "Baltimore Orioles",  "price": -105, "point": +1.5},
        ]),
    ])]
    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"draftkings"})
    labels = {s.outcome for s in snaps}
    assert any("Cleveland Guardians -1.5" in lbl for lbl in labels)
    assert any("Baltimore Orioles +1.5" in lbl for lbl in labels)


def test_placement_book_without_line_produces_no_snapshot():
    # fanatics is the placement book but doesn't offer this market.
    games = [_game([
        _bm("draftkings", "h2h", [
            {"name": "Cleveland Guardians", "price": -140},
            {"name": "Baltimore Orioles",  "price": +120},
        ]),
        _bm("pinnacle", "h2h", [
            {"name": "Cleveland Guardians", "price": -135},
            {"name": "Baltimore Orioles",  "price": +125},
        ]),
    ])]
    snaps = _snapshot_rows_from_games(games, "baseball_mlb", {"fanatics"})
    assert snaps == []
