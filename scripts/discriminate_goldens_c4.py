"""Discrimination check: prove the golden regeneration captured ONLY the
additive C4 notes, with zero outcome changes (score/tier/stance/seal)."""
import json, glob, os, subprocess

changed_fields = {}
for p in sorted(glob.glob('tests/fixtures/speed_golden/*.json')):
    name = os.path.basename(p)
    old_raw = subprocess.run(
        ['git', 'show', f'HEAD:tests/fixtures/speed_golden/{name}'],
        capture_output=True, text=True).stdout
    if not old_raw:
        continue
    old = json.loads(old_raw)
    new = json.load(open(p))
    diffs = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    changed_fields[name] = diffs

ok = True
allowed = {'notes'}
for name, diffs in changed_fields.items():
    bad = set(diffs) - allowed
    status = "OK" if not bad else f"UNEXPECTED: {sorted(bad)}"
    if bad:
        ok = False
    print(f"{name}: {diffs} -> {status}")
print("ALL ADDITIVE-ONLY" if ok else "REGRESSION DETECTED")
