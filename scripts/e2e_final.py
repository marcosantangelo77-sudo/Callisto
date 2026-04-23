"""Final end-to-end integration test — full pipeline on live data."""
import sys, json
sys.path.insert(0, '.')
from tools.devig import devig_american
from tools.sim import nba_game_sim, compare_sim_to_book
from tools.ev import evaluate_edge, ev_binary
from tools.sizing import bet_size, bet_size_american, best_price
from tools.sgp import evaluate_sgp
from tools.math_utils import american_to_decimal, american_to_implied, implied_scores

print('=' * 70)
print('END-TO-END INTEGRATION TEST')
print('Indiana Pacers @ Orlando Magic — Full Pipeline')
print('=' * 70)

BANKROLL = 1000

# ══════════════════════════════════════════════════
# STAGE 1: LIVE ODDS + DEVIG
# ══════════════════════════════════════════════════
print('\n[STAGE 1] ODDS INGESTION + DEVIG')
print('-' * 50)

dk_ml = devig_american(260, -360, method='power')
fd_ml = devig_american(290, -410, method='power')
fair_ind = (dk_ml['fair_probabilities'][0] + fd_ml['fair_probabilities'][0]) / 2
fair_orl = (dk_ml['fair_probabilities'][1] + fd_ml['fair_probabilities'][1]) / 2
print(f'Consensus ML:  IND {fair_ind:.4f}  ORL {fair_orl:.4f}')

dk_sp = devig_american(-115, -115, method='power')
fd_sp = devig_american(-102, -130, method='power')
fair_sp_ind = (dk_sp['fair_probabilities'][0] + fd_sp['fair_probabilities'][0]) / 2
print(f'Consensus Spread: IND+7.5 {fair_sp_ind:.4f}')

dk_tot = devig_american(-110, -120, method='power')
fair_over = dk_tot['fair_probabilities'][0]
fair_under = dk_tot['fair_probabilities'][1]
print(f'DK Total: O227.5 {fair_over:.4f}  U227.5 {fair_under:.4f}')

# ══════════════════════════════════════════════════
# STAGE 2: MONTE CARLO SIMULATION
# ══════════════════════════════════════════════════
print('\n[STAGE 2] MONTE CARLO SIMULATION')
print('-' * 50)

# Use implied scores from spread/total to get power ratings
# Spread = -7.5 (Orlando favored), Total = 227.5
# Implied: ORL = (227.5 + 7.5) / 2 = 117.5, IND = (227.5 - 7.5) / 2 = 110.0
scores = implied_scores(-7.5, 227.5)
print(f'Implied scores: IND {scores[1]:.1f}  ORL {scores[0]:.1f}')

# NBA sim with reasonable ratings derived from the line
# ORL: off_rating ~113 (slightly above avg), def_rating ~108 (good defense)
# IND: off_rating ~112, def_rating ~113 (below avg defense)
# Pace: ORL ~97, IND ~102, league ~100
sim = nba_game_sim(
    team_a_off_rtg=113.0, team_a_def_rtg=108.0,  # ORL (home)
    team_b_off_rtg=112.0, team_b_def_rtg=113.0,  # IND (away)
    team_a_pace=97.0, team_b_pace=102.0,
    n_sims=100000,
)
print(f'Sim results (100K sims):')
print(f'  ORL mean: {sim["a_mean"]:.1f}  IND mean: {sim["b_mean"]:.1f}')
print(f'  Total mean: {sim["total_mean"]:.1f}')
print(f'  ORL win%: {sim["a_win_prob"]:.4f}  IND win%: {sim["b_win_prob"]:.4f}')
# IND +7.5 means team_b spread of +7.5 -> margin + 7.5 > 0
sp_7_5 = sim["spreads"].get(7.5, sim["spreads"].get("7.5", "N/A"))
print(f'  IND +7.5 cover: {sp_7_5}')
# Over 227.5
ov_227_5 = sim["totals"].get(227.5, sim["totals"].get("227.5", "N/A"))
print(f'  Over 227.5: {ov_227_5}')

# ══════════════════════════════════════════════════
# STAGE 3: EDGE EVALUATION (DEVIG vs BOOK)
# ══════════════════════════════════════════════════
print('\n[STAGE 3] EDGE EVALUATION')
print('-' * 50)

# Compare devigged fair probs to book prices
markets = [
    ('IND ML', fair_ind, [(260, 'DK'), (290, 'FD')]),
    ('ORL ML', fair_orl, [(-360, 'DK'), (-410, 'FD')]),
    ('IND +7.5', fair_sp_ind, [(-115, 'DK'), (-102, 'FD')]),
    ('Over 227.5', fair_over, [(-110, 'DK')]),
    ('Under 227.5', fair_under, [(-120, 'DK')]),
]

for label, fair, book_lines in markets:
    for odds, book in book_lines:
        ev = evaluate_edge(fair, odds, confidence='medium')
        status = 'ACTIONABLE' if ev['actionable'] else 'PASS'
        print(f'  {label} @ {book} {odds:+d}: EV={ev["ev_pct"]:+.2f}%  edge={ev["edge_pct"]:+.2f}%  {ev["rating"]}  [{status}]')

# ══════════════════════════════════════════════════
# STAGE 4: BEST PRICE SELECTION
# ══════════════════════════════════════════════════
print('\n[STAGE 4] BEST PRICE')
print('-' * 50)

bp_ind = best_price(260, 290)
bp_orl = best_price(-360, -410)
bp_sp = best_price(-115, -102)
print(f'IND ML:   {bp_ind["best_book"]} {bp_ind["best_odds_american"]:+d} (saves {bp_ind["improvement_pct"]}%)')
print(f'ORL ML:   {bp_orl["best_book"]} {bp_orl["best_odds_american"]:+d} (saves {bp_orl["improvement_pct"]}%)')
print(f'IND +7.5: {bp_sp["best_book"]} {bp_sp["best_odds_american"]:+d} (saves {bp_sp["improvement_pct"]}%)')

# ══════════════════════════════════════════════════
# STAGE 5: KELLY SIZING (for any actionable bets)
# ══════════════════════════════════════════════════
print('\n[STAGE 5] KELLY SIZING')
print('-' * 50)

any_actionable = False
for label, fair, book_lines in markets:
    for odds, book in book_lines:
        ev = evaluate_edge(fair, odds, confidence='medium')
        if ev['actionable']:
            any_actionable = True
            sizing = bet_size_american(BANKROLL, fair, odds, 'medium')
            print(f'  {label} @ {book} {odds:+d}:')
            print(f'    Stake: ${sizing["recommended_stake"]}  Kelly adj: {sizing["kelly_adjusted"]}  Risk: {sizing["bankroll_risk_pct"]}%')

if not any_actionable:
    print('  No actionable bets on this game.')
    print('  System correctly refuses all -EV positions.')

# ══════════════════════════════════════════════════
# STAGE 6: SGP CHECK
# ══════════════════════════════════════════════════
print('\n[STAGE 6] SGP CORRELATION CHECK')
print('-' * 50)

# Hypothetical 2-leg SGP: ORL team total Over + player pts Over
sgp_legs = [
    {'type': 'team_total_over', 'fair_prob': fair_over},
    {'type': 'player_pts_over_same_team', 'fair_prob': 0.54},
]
sgp_book = 3.20  # hypothetical SGP pricing

sgp = evaluate_sgp(sgp_legs, 'nba', sgp_book)
print(f'SGP: ORL team total Over + player pts Over')
print(f'  Naive joint:       {sgp["naive_joint_prob"]}')
print(f'  Correlated (mid):  {sgp["midpoint_prob"]}')
print(f'  Book implied:      {sgp["book_implied"]}')
print(f'  Edge midpoint:     {sgp["edge_midpoint"]}%')
print(f'  Actionable:        {sgp["actionable_midpoint"]}')

if sgp['actionable_midpoint']:
    sgp_sizing = bet_size(BANKROLL, sgp['midpoint_prob'], sgp_book, 'medium')
    print(f'  Stake: ${sgp_sizing["recommended_stake"]}')

# ══════════════════════════════════════════════════
# FINAL REPORT
# ══════════════════════════════════════════════════
print('\n' + '=' * 70)
print('PIPELINE INTEGRITY REPORT')
print('=' * 70)

checks = {
    'Devig sums to 1.0': all(
        abs(sum(r['fair_probabilities']) - 1.0) < 0.0001
        for r in [dk_ml, fd_ml, dk_sp, fd_sp, dk_tot]
    ),
    'EV math correct': abs(
        (fair_ind * american_to_decimal(260) - 1) * 100 -
        evaluate_edge(fair_ind, 260, 'medium')['ev_pct']
    ) < 0.01,
    'Kelly = 0 on -EV': bet_size_american(1000, fair_ind, 260, 'medium')['recommended_stake'] == 0,
    'Best price selects higher': bp_ind['best_odds_american'] == 290,
    'SGP naive < correlated': sgp['naive_joint_prob'] < sgp['midpoint_prob'],
    'Sim total reasonable': sim['total_mean'] > 180 and sim['total_mean'] < 280,
}

all_pass = True
for check, passed in checks.items():
    status = 'PASS' if passed else 'FAIL'
    if not passed:
        all_pass = False
    print(f'  [{status}] {check}')

print(f'\nOverall: {"ALL CHECKS PASS" if all_pass else "SOME CHECKS FAILED"}')
