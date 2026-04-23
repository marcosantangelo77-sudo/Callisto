"""SGP +EV scenario + Kelly sizing verification."""
import sys
sys.path.insert(0, '.')
from tools.sgp import evaluate_sgp
from tools.sizing import bet_size

print('=' * 70)
print('PIPELINE STEP 6: SGP +EV SCENARIO + KELLY')
print('=' * 70)

# Book prices SGP as independent (naive): 0.55 x 0.52 = 0.286 -> dec 3.50
# True joint prob with correlation is higher -> edge
legs = [
    {'type': 'team_total_over', 'fair_prob': 0.55},
    {'type': 'player_pts_over_same_team', 'fair_prob': 0.52},
]
book_decimal = 3.50

result = evaluate_sgp(legs, 'nba', book_decimal)
print(f'Book prices as independent: {1/book_decimal:.4f} implied')
print(f'True joint (midpoint rho=0.45): {result["midpoint_prob"]}')
print(f'Edge midpoint: {result["edge_midpoint"]}%')
print(f'EV midpoint: {result["ev_midpoint"]}%')
print(f'Actionable (midpoint): {result["actionable_midpoint"]}')

BANKROLL = 1000

if result['actionable_midpoint']:
    sizing = bet_size(
        bankroll=BANKROLL,
        fair_prob=result['midpoint_prob'],
        decimal_odds=book_decimal,
        confidence='medium',
    )
    print(f'\nKelly sizing:')
    print(f'  Full Kelly: {sizing["kelly_full"]}')
    print(f'  Quarter Kelly: {sizing["kelly_quarter"]}')
    print(f'  Adjusted: {sizing["kelly_adjusted"]}')
    print(f'  Recommended stake: ${sizing["recommended_stake"]}')
    print(f'  Bankroll risk: {sizing["bankroll_risk_pct"]}%')

    # Manual verification
    b = book_decimal - 1
    p = result['midpoint_prob']
    q = 1 - p
    f_star = (b * p - q) / b
    print(f'\n  Manual Kelly: f* = ({b}*{p} - {q})/{b} = {f_star:.4f}')
    print(f'  Tool f*: {sizing["kelly_full"]}')
    print(f'  Match: {"PASS" if abs(f_star - sizing["kelly_full"]) < 0.001 else "FAIL"}')

if result['actionable_conservative']:
    sizing_c = bet_size(BANKROLL, result['conservative_prob'], book_decimal, 'medium')
    print(f'\nConservative sizing: ${sizing_c["recommended_stake"]}')
else:
    print(f'\nConservative (rho=0.35): NOT actionable (edge={result["edge_conservative"]}%)')
    print('Correct: conservative bound -> thinner edge -> higher bar')

print('\n--- 3-LEG SGP TEST ---')
legs3 = [
    {'type': 'team_total_over', 'fair_prob': 0.55},
    {'type': 'player_pts_over_same_team', 'fair_prob': 0.52},
    {'type': 'game_total_over', 'fair_prob': 0.53},
]
book3 = 7.0
result3 = evaluate_sgp(legs3, 'nba', book3)
print(f'3-leg SGP at +600 (dec 7.0)')
print(f'  Naive:        {result3["naive_joint_prob"]}')
print(f'  Conservative: {result3["conservative_prob"]}')
print(f'  Midpoint:     {result3["midpoint_prob"]}')
print(f'  Book implied: {result3["book_implied"]}')
print(f'  EV mid:       {result3["ev_midpoint"]}%')
print(f'  Pairs: {len(result3["correlation_pairs"])}')
for cp in result3['correlation_pairs']:
    print(f'    {cp["legs"]}: rho_mid={cp["rho_mid"]}')
