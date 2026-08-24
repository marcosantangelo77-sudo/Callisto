"""Runnable worked example: Powell speech-coverage spikes vs Bitcoin.

End-to-end with real data:
  1. EVENT DISCOVERY — GDELT DOC coverage-volume timeline for "Jerome Powell"
     speech over 2 years. Candidate event dates = days where coverage z-score
     exceeds +3 (a dated occurrence you can point at: the coverage spike).
  2. PROVABLE TIMESTAMPS — every candidate must carry a Wayback
     IMMUTABLE_SNAPSHOT proof of the Fed's speech-index page dated strictly
     BEFORE the event date (the page provably existed and listed speeches
     before t=0). Fail-closed: no proof, no event.
  3. OUTCOMES — FRED CBBTCUSD forward log-returns at +1/+4/+12 weeks from t=0.
  4. CONTROL — 5 random dates per event drawn from the same calendar span,
     ≥21 days from any event (regime-matched).
  5. REPORT — n, median, IQR per horizon for events and controls, plus a
     sign-flip permutation test of the median gap.

This script CACHES the GDELT timeline to data/event_study/ so reruns do not
re-hit a rate-limited API. Output is a distribution report only — no verdict,
no confidence score, no signal.

Usage: python3 scripts/event_study_powell_btc.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.event_study.events import Event, EventSet          # noqa: E402
from tools.event_study.outcomes import (                       # noqa: E402
    OutcomeSeries, measure_forward_returns, random_control_returns, report)
from tools.sources.base import RestSource                      # noqa: E402
from tools.sources.gdelt import SPEC as SPEC_GDELT             # noqa: E402
from tools.sources.wayback import WaybackAdapter, SPEC as SPEC_WB  # noqa: E402

QUERY = '"Jerome Powell" speech'
SOURCE_URL = "https://www.federalreserve.gov/newsevents/speeches.htm"
CACHE = os.path.join("data", "event_study", "gdelt_timeline.json")


def load_timeline() -> list:
    """Cached GDELT timelinevol points [{date, value}]."""
    if os.path.exists(CACHE):
        with open(CACHE) as fh:
            return json.load(fh)["timeline"][0]["data"]
    from tools.sources.gdelt import GdeltAdapter
    gd = GdeltAdapter(RestSource(SPEC_GDELT))
    d = gd.coverage_timeline(QUERY, timespan="2y")
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w") as fh:
        json.dump(d, fh)
    return d["timeline"][0]["data"]


def spike_dates(points, threshold=3.0, min_gap_days=21) -> list:
    vals = [p["value"] for p in points]
    mu, sd = statistics.mean(vals), statistics.pstdev(vals)
    spikes = [(p["date"], (p["value"] - mu) / sd) for p in points]
    spikes = [(ds, z) for ds, z in spikes if z > threshold]
    kept = []
    for ds, z in sorted(spikes):
        dd = dt.datetime.strptime(ds[:8], "%Y%m%d").date()
        if kept and (dd - kept[-1][0]).days < min_gap_days:
            continue
        kept.append((dd, z))
    return kept


def prove_events(spikes) -> EventSet:
    wb = WaybackAdapter(RestSource(SPEC_WB))
    es = EventSet()
    for dd, z in spikes:
        label = f"POWELL-COVERAGE-{dd.isoformat()}"
        proof, reason = wb.snapshot_proof(SOURCE_URL, before=dd)
        if proof is None:
            # retry with day+1 window only if capture lands ON the event day
            # is impossible — snapshot_proof already requires strict-before;
            # fail-closed either way.
            es.excluded.append((label, reason))
            continue
        es.events.append(Event(
            label=label, event_date=dd, query=QUERY, source_url=SOURCE_URL,
            seendate=f"z={z:.2f}", proof_locator=proof.locator,
            proof_published_on=proof.published_on,
            proof_sha256=proof.content_sha256))
    return es


def main() -> int:
    print(f"[1] GDELT coverage timeline: {QUERY!r}")
    points = load_timeline()
    print(f"    {len(points)} daily points "
          f"{points[0]['date'][:8]}..{points[-1]['date'][:8]}")

    print("[2] Coverage-spike candidate events (z > +3):")
    spikes = spike_dates(points)
    for dd, z in spikes:
        print(f"    {dd}  z={z:.2f}")

    print("[3] Wayback IMMUTABLE_SNAPSHOT proofs (strictly before event):")
    es = prove_events(spikes)
    for ev in es.events:
        print(f"    {ev.label}: proven via capture {ev.proof_published_on}")
    for label, reason in es.excluded:
        print(f"    EXCLUDED {label}: {reason}")
    s = es.summary()
    print(f"    admitted {s['n_proven']}, excluded {s['n_excluded']} "
          "(fail-closed)")

    print("[4] FRED outcome series: CBBTCUSD")
    lo = min(e.event_date for e in es.events).isoformat()
    hi = (max(e.event_date for e in es.events)
          + dt.timedelta(days=90)).isoformat()
    series = OutcomeSeries.load("CBBTCUSD", start=lo, end=hi)
    print(f"    {len(series.obs)} observations {lo}..{hi}")

    event_rows = measure_forward_returns(es.events, series)
    control_rows = random_control_returns(es.events, series, n_per_event=5)
    print(f"    controls drawn: {len(control_rows)}")

    print("[5] DISTRIBUTION REPORT — CBBTCUSD forward log-returns")
    rep = report(event_rows, control_rows, "CBBTCUSD")
    out_path = os.path.join("data", "event_study",
                            "powell_btc_report.json")
    payload = {"events": [
        {"label": r["label"], "event_date": r["event_date"].isoformat(),
         **{h: r[h] for h in ("w1", "w4", "w12")}} for r in event_rows],
        "controls_count": len(control_rows), "report": rep}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)

    for h, block in rep["horizons"].items():
        e, c = block["events"], block["controls"]
        print(f"\n  {h}: EVENTS   n={e['n']:3d} median={_pct(e['median'])} "
              f"IQR=[{_pct(e['q25'])}, {_pct(e['q75'])}]")
        print(f"      CONTROLS n={c['n']:3d} median={_pct(c['median'])} "
              f"IQR=[{_pct(c['q25'])}, {_pct(c['q75'])}]")
        sf = block["sign_flip"]
        pv = sf.get("p_value")
        print(f"      sign-flip permutation: delta_median="
              f"{_pct(sf.get('delta_median'))}, "
              f"p={'n/a' if pv is None else format(pv, '.3f')} -> "
              f"{block['reading']}")
    print(f"\n  full report written to {out_path}")
    print("\n  NOTE: backward-looking corpus selected by hindsight; this "
          "hit-rate is NOT an unbiased estimate. The sound version fixes a "
          "speaker/event set NOW and captures forward.")
    return 0


def _pct(x):
    return "n/a" if x is None else f"{100 * x:+.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
