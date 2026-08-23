import sqlite3, os

# Query BOTH databases
print("=" * 80)
print("DATABASE 1: memory/callisto.db (canonical)")
print("=" * 80)
db1 = os.path.join('memory', 'callisto.db')
conn1 = sqlite3.connect(db1)
c1 = conn1.cursor()

c1.execute("""
    SELECT h.name, h.status, COUNT(be.id) as event_count
    FROM hypotheses h
    LEFT JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status NOT IN ('rejected', 'retired')
      AND h.sport = 'basketball_nba'
    GROUP BY h.hypothesis_id
    ORDER BY event_count DESC
""")
rows1 = c1.fetchall()
print(f'Total active NBA hypotheses: {len(rows1)}')
for name, status, count in rows1:
    print(f'  {status:15s} | {count:5d} events | {name}')

counts1 = [c for _, _, c in rows1 if c > 0]
from collections import Counter
freq1 = Counter(counts1)
print(f'\nEvent count frequency:')
for cnt, freq in freq1.most_common():
    flag = " <-- IDENTICAL" if freq > 1 else ""
    print(f'  {cnt} events: {freq} hypotheses{flag}')
conn1.close()

print()
print("=" * 80)
print("DATABASE 2: data/hypotheses.db (legacy)")
print("=" * 80)
db2 = os.path.join('data', 'hypotheses.db')
if os.path.exists(db2):
    conn2 = sqlite3.connect(db2)
    c2 = conn2.cursor()
    c2.execute("""SELECT name, event_count, status FROM hypotheses
    WHERE sport_key LIKE '%nba%' AND status NOT IN ('rejected')
    ORDER BY event_count DESC""")
    rows2 = c2.fetchall()
    print(f'Total active NBA hypotheses: {len(rows2)}')
    for name, ecount, status in rows2:
        print(f'  {status:15s} | {str(ecount):>5} events | {name}')

    counts2 = [c for _, c, _ in rows2 if c and c > 0]
    freq2 = Counter(counts2)
    print(f'\nEvent count frequency:')
    for cnt, freq in freq2.most_common():
        flag = " <-- IDENTICAL" if freq > 1 else ""
        print(f'  {cnt} events: {freq} hypotheses{flag}')
    conn2.close()
else:
    print("data/hypotheses.db not found")
