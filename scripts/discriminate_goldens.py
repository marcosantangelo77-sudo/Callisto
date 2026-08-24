"""(a)-vs-(b) discriminator: run every speed-golden scenario SERIALLY and
PARALLELly UNDER THE FIX, compare fingerprints to each other (not to stored
goldens). Serial = monkeypatched sequential gather inside the engine."""
import sys, json, tempfile, asyncio
from pathlib import Path

ROOT = Path("/Users/marcosantangelo/callisto-wt/review-ox")
sys.path.insert(0, str(ROOT))

import tools.pipeline.engine as eng

_real_gather = asyncio.gather

async def _serial_gather(*aws, return_exceptions=False):
    out = []
    for aw in aws:
        try:
            out.append(await aw)
        except BaseException as e:
            if return_exceptions:
                out.append(e)
            else:
                raise
    return out

def run_all(mode):
    if mode == "serial":
        eng.asyncio.gather = _serial_gather
    else:
        eng.asyncio.gather = _real_gather
    from tests.test_speed_parallel_leaves import SCENARIOS, _fingerprint, _run_scenario
    fps = {}
    for name, spec in sorted(SCENARIOS.items()):
        with tempfile.TemporaryDirectory() as td:
            result, ledger = _run_scenario(Path(td), **dict(spec))
        fp = _fingerprint(result, ledger)
        # artifact hashes embed tmp paths? check: keep but note
        fps[name] = fp
    return fps

serial = run_all("serial")
eng.asyncio.gather = _real_gather
par = run_all("parallel")

ok = True
for name in sorted(serial):
    s, p = serial[name], par[name]
    if s == p:
        print(f"MATCH   {name} (sealed={p['sealed']} conf={p['confidence_score']})")
    else:
        ok = False
        print(f"DIVERGE {name}")
        for k in set(s) | set(p):
            if s.get(k) != p.get(k):
                print(f"  {k}:\n    serial: {str(s.get(k))[:300]}\n    par:    {str(p.get(k))[:300]}")
print("VERDICT:", "(a) serial==parallel under fix" if ok else "(b) REAL nondeterminism")
