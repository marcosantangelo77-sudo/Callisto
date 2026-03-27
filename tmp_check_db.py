import sqlite3
c = sqlite3.connect('data/callisto.db')
print('game_results:', c.execute('SELECT COUNT(*) FROM game_results').fetchone()[0])
print('game_contexts_with_scores:', c.execute('SELECT COUNT(*) FROM game_contexts WHERE home_score IS NOT NULL AND away_score IS NOT NULL').fetchone()[0])
print('hypotheses_backtesting:', c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='backtesting'").fetchone()[0])
print('signals:', c.execute('SELECT COUNT(*) FROM signals').fetchone()[0])
# Check the self_repair minimum_events vs PROMOTION_GATES
print('---')
print('self_repair minimum_events_for_promotion target:', 20)
print('PROMOTION_GATES backtesting->paper min_signals:', 5)
c.close()
