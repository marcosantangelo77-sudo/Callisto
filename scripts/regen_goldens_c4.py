"""Regenerate speed-golden fingerprints from the current serial engine.

Precedent: 7ae5f91 (and 2512b6f before it) — after an outcome-neutral,
additive-only change (the restored red-team C4 'asked but NOT contributing'
notes and the C5 reconciliation fields), regenerate the golden fixtures.
The discriminator (scripts/discriminate_goldens.py) previously proved the
engine deterministic serial-vs-parallel; only additive note lines changed.
"""
import json, sys, tempfile, pathlib
sys.path.insert(0, '/Users/marcosantangelo/callisto-wt/gate')
sys.path.insert(0, '/Users/marcosantangelo/callisto-wt/gate/tests')
from test_speed_parallel_leaves import SCENARIOS, _run_scenario, _fingerprint, GOLDEN_DIR

for scenario in sorted(SCENARIOS):
    with tempfile.TemporaryDirectory() as td:
        result, ledger = _run_scenario(pathlib.Path(td), **dict(SCENARIOS[scenario]))
        fp = _fingerprint(result, ledger)
    out = GOLDEN_DIR / f"{scenario}.json"
    old = json.loads(out.read_text())
    changed = {k: (old.get(k), fp.get(k)) for k in fp if old.get(k) != fp.get(k)}
    out.write_text(json.dumps(fp, indent=1, sort_keys=True))
    print(scenario, "-> regenerated; changed keys:", list(changed.keys()))
