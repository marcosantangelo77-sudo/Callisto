from tools.edge import MarketQuote, assess_edge, MIN_EDGE_TO_ACT

# H17b: kind mismatch — price=-110 with counter as int 105 but kind='american' works;
# now sweep with valid american counters. Key question: does assess_edge's
# ev_per_unit (computed at RAW decimal payout) disagree with edge (vs devigged fair)?
disagree = []
for am in list(range(-400, -99)) + list(range(100, 2001, 25)):
    for p1000 in range(300, 700, 50):
        p = p1000 / 1000
        ctr = -104 if am < 0 else 108
        qq = MarketQuote(price=am, counter_price=ctr, kind="american")
        try:
            aa = assess_edge("t", p, qq)
        except Exception:
            continue
        # Consistency invariant: actionable iff edge>=min AND ev>0 (that's the code).
        # Real invariant to test: if edge >= min_edge (fair-prob edge) then EV at FAIR
        # decimal odds should also be positive... check ev vs fair-based ev divergence.
        dec = 1.0 / aa.market_prob_raw
        b = dec - 1.0
        q_ = 1.0 - p
        kelly_at_raw = max(0.0, (b * p - q_) / b)
        # kelly reported uses raw decimal too; but edge uses fair. If market_prob_fair
        # differs from raw by vig, a "fair" edge can coexist with kelly computed on raw.
        if aa.actionable and abs(aa.market_prob_fair - aa.market_prob_raw) > 1e-9:
            pass  # normal
        # The suspicious case: kelly>0 while fair-edge negative (raw payout rescues it)
        if aa.kelly_fraction_full > 0 and aa.edge < 0:
            disagree.append((am, p, round(aa.edge, 4), round(aa.kelly_fraction_full, 4), aa.actionable))
print("H17b kelly-positive-but-negative-edge:", len(disagree), disagree[:5])
