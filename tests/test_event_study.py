"""Offline tests for the event-study harness — no sockets, synthetic series.

Run: python3 tests/test_event_study.py
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.event_study.events import Event, EventSet            # noqa: E402
from tools.event_study.outcomes import (                        # noqa: E402
    OutcomeSeries, measure_forward_returns, random_control_returns,
    dist_stats, sign_flip_test, report)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def make_series():
    # linear growth 1% per day from 2024-01-01, gaps on weekends
    obs = {}
    d = dt.date(2024, 1, 1)
    v = 100.0
    while d < dt.date(2025, 1, 1):
        if d.weekday() < 5:               # skip weekends
            obs[d] = round(v, 4)
            v *= 1.01
        d += dt.timedelta(days=1)
    return OutcomeSeries(series_id="TEST", obs=obs)


def main() -> int:
    s = make_series()
    t0 = dt.date(2024, 3, 4)              # a Monday

    r7 = s.forward_return(t0, 7)
    check("w1 return ≈ 5 trading days of +1%", abs(r7 - 5 * 0.00995) < 0.002)

    # missing start endpoint → None, not a bogus 0.0
    check("return before data start is None",
          s.forward_return(dt.date(2023, 11, 20), 7) is None)

    evs = [Event(label=f"E{i}", event_date=d, query="q", source_url="u",
                 proof_locator="l", proof_published_on=d)
           for i, d in enumerate([dt.date(2024, 2, 5), dt.date(2024, 6, 3),
                                  dt.date(2024, 9, 2)])]
    rows = measure_forward_returns(evs, s)
    check("3 event rows measured", len(rows) == 3
          and all(rows[i]["w1"] is not None for i in range(3)))

    ctr = random_control_returns(evs, s, n_per_event=4, seed=11)
    check("controls drawn (n>=8)", len(ctr) >= 8)
    check("controls avoid ±21d of events", all(
        all(abs((c["event_date"] - e.event_date).days) >= 21 for e in evs)
        for c in ctr))

    st = dist_stats([r["w1"] for r in rows])
    check("dist_stats fields", st["n"] == 3 and st["median"] is not None
          and st["q25"] <= st["median"] <= st["q75"])

    # sign-flip: identical distributions → not significant; shifted → yes
    import random as _r
    rng = _r.Random(3)
    a = [rng.gauss(0.05, 0.02) for _ in range(40)]
    b = [rng.gauss(0.05, 0.02) for _ in range(40)]
    c2 = [rng.gauss(0.20, 0.02) for _ in range(40)]
    t_ns = sign_flip_test(a, b, n_permutations=2000, seed=1)
    t_s = sign_flip_test(a, c2, n_permutations=2000, seed=1)
    check("no-signal case p >= 0.05", t_ns["p_value"] >= 0.05)
    check("shifted case p < 0.05", t_s["p_value"] < 0.05)

    rep = report(rows, ctr, "TEST")
    check("report has 3 horizons", set(rep["horizons"]) == {"w1", "w4", "w12"})
    check("report reading mentions controls", "controls" in
          rep["horizons"]["w1"]["reading"])

    # empty-set robustness
    es = EventSet()
    check("empty EventSet summary", es.summary()["n_proven"] == 0)

    print(f"\n{len(FAILS)} failures" if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
