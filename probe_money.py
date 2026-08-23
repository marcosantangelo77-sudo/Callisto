from tools.edge import MarketQuote, assess_edge
from tools.kelly import kelly_full, calculate_units

q = MarketQuote(price=0.60, counter_price=0.61, kind="probability")
a = assess_edge("t", 0.62, q)
print("H1b crossed:", a.market_prob_fair, a.edge, a.actionable, "notes:", a.notes)

viol = 0; worst = None
for am in list(range(-2000, 0)) + list(range(100, 10001)):
    dec = 1 + (am / 100 if am > 0 else 100 / abs(am)); b = dec - 1
    implied = 100 / (am + 100) if am > 0 else abs(am) / (abs(am) + 100)
    for e in [x / 10000 for x in range(5, 500, 5)]:
        p = min(1, max(0, implied + e))
        exact = max(0, (b * p - (1 - p)) / b)
        got = kelly_full(e, am)
        if got > exact + 1e-12:
            viol += 1
            d = (got - exact) / exact if exact else 0
            if not worst or d > worst[0]:
                worst = (d, e, am, got, exact)
print("H2 total round-ups:", viol, "worst rel:", worst)

u = calculate_units(1000, edge=-0.05, confidence=0.95)
print("H6 neg edge:", u["units"], u["unit_label"], u["dollar_amount"])

q8 = MarketQuote(price=-105, kind="american")
a8 = assess_edge("t", 0.55, q8)
print("H8 raw:", a8.edge, a8.actionable, "ev:", a8.ev_per_unit, "kelly:", a8.kelly_fraction_full)

q9 = MarketQuote(price=-110, counter_price=105, kind="american")
print("H9 crossed-american:", q9.fair_probability())
