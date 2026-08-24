"""Score the battery results against ground truth.

Reads findings/battery/results.jsonl + questions.json, classifies each
question, and prints headline metrics + per-question rows for the report.
"""
import json
import re

BANK = {q['id']: q for q in json.load(open('findings/battery/questions.json'))}
RECS = [json.loads(l) for l in open('findings/battery/results.jsonl')]


def verdict_of(rec):
    tail = rec['stdout_tail']
    if 'SEALED' in tail:
        m = re.search(r'SEALED\s+confidence ([\d.]+) tier=(\w+)', tail)
        conf = float(m.group(1)) if m else None
        tier = m.group(2) if m else None
        return 'sealed', conf, tier
    if 'REFUSED' in tail:
        m = re.search(r'reason\s+:(.*)', tail)
        return 'refused', None, (m.group(1).strip()[:100] if m else '')
    return 'unknown', None, None


def sources_of(rec):
    m = re.search(r'sources\s+:\s+(\d+) distinct \(([^)]*)\) / (\d+) fetches', rec['stdout_tail'])
    if m:
        return int(m.group(1)), [s.strip() for s in m.group(2).split(',') if s.strip()], int(m.group(3))
    return 0, [], 0


rows = []
for r in RECS:
    qid = r['id']
    q = BANK[qid]
    v, conf, tier_or_reason = verdict_of(r)
    ns, srcs, nf = sources_of(r)
    unknowable = 'unknowable' in q['shape']
    if v == 'sealed':
        # what did it seal? extract conclusion first lines
        idx = r['stdout_tail'].find('=' * 72)
        concl = r['stdout_tail'][idx:idx + 400].replace('\n', ' ')
        outcome = ('confident-on-unknowable FAILURE' if unknowable
                   else f'sealed conf={conf} tier={tier_or_reason} -> judge vs GT')
    elif v == 'refused':
        outcome = ('correct-refusal SUCCESS' if unknowable else 'refused on answerable')
    else:
        outcome = 'NO VERDICT PARSED'
    rows.append({
        'id': qid,
        'source': q['source'],
        'shape': q['shape'],
        'verdict': v,
        'confidence': conf,
        'tier': tier_or_reason if v == 'sealed' else '',
        'n_sources': ns,
        'sources': ','.join(srcs),
        'n_fetches': nf,
        'elapsed_s': r['elapsed_s'],
        'outcome': outcome,
    })

print(f"{'id':<14} {'verdict':<8} {'conf':<5} {'srcs':<22} {'nf':>3} {'sec':>6}  shape")
for row in rows:
    print(f"{row['id']:<14} {row['verdict']:<8} {str(row['confidence'] or ''):<5} "
          f"{row['sources'][:22]:<22} {row['n_fetches']:>3} {str(row['elapsed_s']):>6}  {row['shape']}")

sealed = [r for r in rows if r['verdict'] == 'sealed']
refused = [r for r in rows if r['verdict'] == 'refused']
unknowables = [r for r in rows if 'unknowable' in r['shape']]
answerable = [r for r in rows if 'unknowable' not in r['shape']]

print()
print('HEADLINES')
print(f"total: {len(rows)}")
print(f"answerable questions: {len(answerable)}; sealed: {len([r for r in answerable if r['verdict']=='sealed'])}; "
      f"refused: {len([r for r in answerable if r['verdict']=='refused'])}")
print(f"unknowable questions: {len(unknowables)}; refused(correct): "
      f"{len([r for r in unknowables if r['verdict']=='refused'])}; "
      f"sealed(confident-FAILURE): {[r['id'] for r in unknowables if r['verdict']=='sealed']}")

json.dump(rows, open('findings/battery/scored_rows.json', 'w'), indent=1)
