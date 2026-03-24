"""Full 13-game NBA scan — find any cross-book edge."""
import sys, json
sys.path.insert(0, '.')
from tools.devig import devig_american
from tools.ev import evaluate_edge
from tools.sizing import best_price
from tools.math_utils import american_to_decimal, american_to_implied

with open('live_odds_dump.json') as f:
    games = json.load(f)

print('=' * 70)
print('PIPELINE STEP 4: FULL 13-GAME NBA SCAN')
print('=' * 70)

edges_found = []

for g in games:
    away = g['away_team']
    home = g['home_team']

    books = {}
    for bm in g.get('bookmakers', []):
        books[bm['key']] = {}
        for mkt in bm.get('markets', []):
            books[bm['key']][mkt['key']] = {
                o['name']: {'price': o['price'], 'point': o.get('point')}
                for o in mkt['outcomes']
            }

    dk = books.get('draftkings', {})
    fd = books.get('fanduel', {})
    if not dk or not fd:
        continue

    # --- Moneyline ---
    if 'h2h' in dk and 'h2h' in fd:
        dk_away = dk['h2h'].get(away, {}).get('price')
        dk_home = dk['h2h'].get(home, {}).get('price')
        fd_away = fd['h2h'].get(away, {}).get('price')
        fd_home = fd['h2h'].get(home, {}).get('price')

        if dk_away and dk_home and fd_away and fd_home:
            try:
                dk_dv = devig_american(int(dk_away), int(dk_home), method='power')
                fd_dv = devig_american(int(fd_away), int(fd_home), method='power')

                fair_away = (dk_dv['fair_probabilities'][0] + fd_dv['fair_probabilities'][0]) / 2
                fair_home = (dk_dv['fair_probabilities'][1] + fd_dv['fair_probabilities'][1]) / 2

                for label, fair, odds_list in [
                    (f'{away} ML', fair_away, [(int(dk_away), 'DK'), (int(fd_away), 'FD')]),
                    (f'{home} ML', fair_home, [(int(dk_home), 'DK'), (int(fd_home), 'FD')]),
                ]:
                    for odds, book in odds_list:
                        ev_result = evaluate_edge(fair, odds, confidence='medium')
                        if ev_result['ev_pct'] > 0:
                            edges_found.append({
                                'game': f'{away} @ {home}',
                                'bet': f'{label} ({book} {odds:+d})',
                                'fair_prob': round(fair, 4),
                                'ev_pct': ev_result['ev_pct'],
                                'edge_pct': ev_result['edge_pct'],
                                'rating': ev_result['rating'],
                                'actionable': ev_result['actionable'],
                            })
            except Exception as e:
                print(f"  WARN: ML edge calc failed for {away}@{home}: {e}")

    # --- Spreads ---
    if 'spreads' in dk and 'spreads' in fd:
        for team in [away, home]:
            dk_sp = dk['spreads'].get(team, {})
            fd_sp = fd['spreads'].get(team, {})
            other = home if team == away else away
            dk_other = dk['spreads'].get(other, {})
            fd_other = fd['spreads'].get(other, {})

            if dk_sp and fd_sp and dk_other and fd_other:
                if dk_sp.get('point') == fd_sp.get('point'):
                    try:
                        dk_dv = devig_american(int(dk_sp['price']), int(dk_other['price']), method='power')
                        fd_dv = devig_american(int(fd_sp['price']), int(fd_other['price']), method='power')
                        fair = (dk_dv['fair_probabilities'][0] + fd_dv['fair_probabilities'][0]) / 2

                        pt = dk_sp['point']
                        for odds, book in [(int(dk_sp['price']), 'DK'), (int(fd_sp['price']), 'FD')]:
                            ev_result = evaluate_edge(fair, odds, confidence='medium')
                            if ev_result['ev_pct'] > 0:
                                edges_found.append({
                                    'game': f'{away} @ {home}',
                                    'bet': f'{team} {pt:+.1f} ({book} {odds:+d})',
                                    'fair_prob': round(fair, 4),
                                    'ev_pct': ev_result['ev_pct'],
                                    'edge_pct': ev_result['edge_pct'],
                                    'rating': ev_result['rating'],
                                    'actionable': ev_result['actionable'],
                                })
                    except Exception as e:
                        print(f"  WARN: Spread edge calc failed for {team} in {away}@{home}: {e}")

    # --- Totals ---
    if 'totals' in dk and 'totals' in fd:
        dk_over = dk['totals'].get('Over', {})
        dk_under = dk['totals'].get('Under', {})
        fd_over = fd['totals'].get('Over', {})
        fd_under = fd['totals'].get('Under', {})

        if dk_over and dk_under and fd_over and fd_under:
            if dk_over.get('point') == fd_over.get('point'):
                try:
                    dk_dv = devig_american(int(dk_over['price']), int(dk_under['price']), method='power')
                    fd_dv = devig_american(int(fd_over['price']), int(fd_under['price']), method='power')
                    fair_over = (dk_dv['fair_probabilities'][0] + fd_dv['fair_probabilities'][0]) / 2
                    fair_under = (dk_dv['fair_probabilities'][1] + fd_dv['fair_probabilities'][1]) / 2

                    total_num = dk_over['point']
                    for label, fair, odds_list in [
                        (f'Over {total_num}', fair_over, [(int(dk_over['price']), 'DK'), (int(fd_over['price']), 'FD')]),
                        (f'Under {total_num}', fair_under, [(int(dk_under['price']), 'DK'), (int(fd_under['price']), 'FD')]),
                    ]:
                        for odds, book in odds_list:
                            ev_result = evaluate_edge(fair, odds, confidence='medium')
                            if ev_result['ev_pct'] > 0:
                                edges_found.append({
                                    'game': f'{away} @ {home}',
                                    'bet': f'{label} ({book} {odds:+d})',
                                    'fair_prob': round(fair, 4),
                                    'ev_pct': ev_result['ev_pct'],
                                    'edge_pct': ev_result['edge_pct'],
                                    'rating': ev_result['rating'],
                                    'actionable': ev_result['actionable'],
                                })
                except Exception as e:
                    print(f"  WARN: Total edge calc failed for {away}@{home}: {e}")

print(f'\nScanned {len(games)} games across ML, spreads, totals')
print(f'Cross-book edges found (EV > 0%): {len(edges_found)}')

if edges_found:
    edges_found.sort(key=lambda x: x['ev_pct'], reverse=True)
    print('\n--- ALL POSITIVE EV OPPORTUNITIES ---')
    for e in edges_found:
        flag = ' ** ACTIONABLE **' if e['actionable'] else ''
        print(f'  {e["game"]}')
        print(f'    {e["bet"]}  fair={e["fair_prob"]}  EV={e["ev_pct"]}%  edge={e["edge_pct"]}%  {e["rating"]}{flag}')
else:
    print('\nNo positive EV found across any market.')
    print('This is EXPECTED. Most nights, standard markets have no exploitable edge')
    print('between DK and FanDuel. Edge comes from boosts, props, and line moves.')
