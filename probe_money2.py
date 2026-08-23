"""H18: does assess_edge's kelly_fraction_quarter get rounded UP by round() in summary?
H19: crossed book end-to-end via the Kalshi adapter path (probability kind) with
a calibrated prob just above fair — the phantom-edge scenario.
H20: devig_market on a 1-element list (single-sided 'market').
H21: additive_devig negative fallback — does auto ever pick additive? (no: auto
never selects additive, so that path is dead code from production callers.)
"""
from tools.edge import MarketQuote, assess_edge
from tools.devig import devig_market

# H20 single-sided
print("H20:", devig_market([1.9]))

# H19: kalshi-style quote, yes_ask 0.60 no_ask 0.61 -> overround 0.21 (huge spread).
# Real Kalshi books: yes 0.63/0.65, no 0.37 ask? complementary means no_ask = 1 - yes_bid.
# If yes_bid=0.59, yes_ask=0.61 -> no_ask should be ~0.41 -> sum = 1.02 fine.
# Crossed case arises when stale cache mixes two snapshots:
q = MarketQuote(price=0.55, counter_price=0.50, kind="probability")
over = 0.55 + 0.50 - 1
r = q.fair_probability()
print("H19 stale-crossed overround:", over, "fair:", r)
a = assess_edge("t", 0.56, q)
print("H19 assess edge:", a.edge, "actionable:", a.actionable, "kelly:", a.kelly_fraction_full)

# H22: summary() rounding direction on edge/kelly
for p in [0.5000004, 0.5000006]:
    qq = MarketQuote(price=-110, counter_price=-110, kind="american")
    aa = assess_edge("t", p, qq)
    s = aa.summary()
    print("H22 p=", p, "edge raw:", aa.edge, "summary edge:", s["edge"], "moved up:", s["edge"] > aa.edge)
