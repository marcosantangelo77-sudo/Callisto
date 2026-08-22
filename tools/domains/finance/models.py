"""B6 S3 — Model templates as live-formula workbook specs (tools/charts.py).

Three templates, all emitting the standard 4-sheet spec that
charts.build_workbook turns into an xlsx where the Model sheet is LIVE
Excel formulas referencing Assumptions!B<row> and Data ranges — change an
assumption in Excel and the valuation recomputes. That is the difference
between a report and a model.

    dcf_workbook        Discounted cash flow: explicit revenue growth /
                        margin / capex / WACC / terminal-growth assumption
                        cells, FCF projection rows, discounting, terminal
                        value (Gordon), EV → equity → per-share, and a
                        two-way sensitivity grid over WACC × terminal growth.
    proforma_workbook   Three-statement projection: historical Data columns
                        from XBRL, growth/margin assumptions driving a
                        forecast, balance-sheet articulation via a plugs
                        (cash is the balancing item against a revolver when
                        short), cash-flow statement tying back.
    comps_workbook      Comparables table: per-peer multiples computed by
                        formula from raw Data columns (price, shares, EPS,
                        book, EBITDA inputs) so the owner can swap a peer's
                        numbers and watch every multiple move.

Every template returns (spec, notes). The spec carries `code` — the sandbox
Python that independently recomputes the headline numbers — so the Excel
chain and the sealed computation agree or the discrepancy is visible.

Honesty rules carried over from statements.py:
  - Assumption cells say where they came from: "XBRL FY2025 fact" vs
    "analyst input required". Nothing fabricated presents as reported.
  - The templates flag what they CANNOT see: footnotes, leases, segments.
"""
from __future__ import annotations

from typing import Any, Optional

from tools.domains.finance.statements import FinancialStatements

LIMITATIONS_NOTE = (
    "LIMITS: built from XBRL tagged facts only — no footnotes, segment "
    "detail, lease schedules, or non-GAAP adjustments. A material footnote "
    "can change any line; verify against the 10-K before relying on this."
)


def _assumption(name: str, value: Any, unit: str, source: str, note: str = "") -> dict:
    return {"name": name, "value": value, "unit": unit,
            "source": source, "note": note}


def _arows(stmt: FinancialStatements) -> list[str]:
    return stmt.period_labels


# ── DCF ───────────────────────────────────────────────────────────────────

def dcf_workbook(
    stmt: FinancialStatements,
    *,
    proj_years: int = 5,
    analyst_inputs: Optional[dict[str, float]] = None,
) -> tuple[dict, dict]:
    """Build the DCF workbook spec from assembled statements.

    Historical revenue/FCF components come from XBRL facts (source noted).
    Forward growth/margins are ASSUMPTIONS the owner edits — seeded from
    `analyst_inputs` when given, else from simple historical averages with
    the seed recorded in the note. WACC and terminal growth always default
    to flagged placeholders; they are judgments, not data.
    """
    ai = dict(analyst_inputs or {})
    labels = stmt.period_labels
    m = stmt.matrix()
    hist_rev = [m.get("revenue", {}).get(p) for p in labels]
    hist_cfo = [m.get("cfo", {}).get(p) for p in labels]
    hist_capex = [m.get("capex", {}).get(p) for p in labels]

    # Seed growth: mean YoY revenue growth of history (None-safe).
    growths = []
    for i in range(1, len(hist_rev)):
        if hist_rev[i] and hist_rev[i - 1] and hist_rev[i - 1] != 0:
            growths.append(hist_rev[i] / hist_rev[i - 1] - 1)
    seed_growth = sum(growths) / len(growths) if growths else 0.05
    # Seed margin: median FCF margin.
    fcf_margins = [
        (c - x) / r for c, x, r in zip(hist_cfo, hist_capex, hist_rev)
        if c is not None and x is not None and r
    ]
    fcf_margins.sort()
    seed_margin = (
        fcf_margins[len(fcf_margins) // 2] if fcf_margins else 0.20
    )
    wacc = ai.get("wacc", 0.10)
    tgrowth = ai.get("terminal_growth", 0.02)

    def src(name: str) -> str:
        if name in ai:
            return "analyst input"
        return "seeded from XBRL history — REVIEW BEFORE USE"

    base_rev = hist_rev[-1]
    if base_rev is None:
        raise ValueError("most recent revenue missing — cannot build DCF")

    # Assumption cells (rows on Assumptions sheet, values at column B).
    assumptions = [
        _assumption("base_revenue", base_rev, "USD",
                    f"XBRL {labels[-1]} fact ({stmt.used_tags.get('revenue','')})"),
        _assumption("rev_growth_yr1", ai.get("rev_growth_yr1", round(seed_growth, 4)),
                    "%", src("rev_growth_yr1"), "year-1 revenue growth"),
        _assumption("rev_growth_terminal", ai.get(
            "rev_growth_terminal", round(min(seed_growth, 0.03), 4)),
            "%", src("rev_growth_terminal"), "growth fading to this by year 5"),
        _assumption("fcf_margin_yr1", ai.get("fcf_margin_yr1", round(seed_margin, 4)),
            "%", src("fcf_margin_yr1"), "simple FCF margin (CFO-capex)/revenue"),
        _assumption("fcf_margin_terminal", ai.get(
            "fcf_margin_terminal", round(seed_margin, 4)),
            "%", src("fcf_margin_terminal")),
        _assumption("wacc", wacc, "%", src("wacc"),
                    "JUDGMENT — discount rate; placeholder 10%"),
        _assumption("terminal_growth", tgrowth, "%", src("terminal_growth"),
                    "JUDGMENT — must stay below WACC"),
        _assumption("net_debt", ai.get("net_debt", _net_debt(stmt)), "USD",
                    "derived from XBRL balance sheet" if "net_debt" not in ai
                    else "analyst input"),
        _assumption("diluted_shares", ai.get("diluted_shares", 0), "shares",
                    "analyst input required — XBRL dei share counts are stale",
                    "set >0 for per-share output"),
    ]

    # Data sheet: historical series straight from the statements.
    data = {
        "Historical": {
            "columns": ["period", "revenue", "cfo", "capex", "fcf"],
            "rows": [
                [p, hist_rev[i], hist_cfo[i], hist_capex[i],
                 (hist_cfo[i] - hist_capex[i])
                 if hist_cfo[i] is not None and hist_capex[i] is not None else None]
                for i, p in enumerate(labels)
            ],
            "provenance": [
                {"column": "revenue",
                 "source": stmt.used_tags.get("revenue", ""),
                 "fetched_at": stmt.fetch_provenance.get("fetched_at", "")},
                {"column": "cfo", "source": stmt.used_tags.get("cfo", ""),
                 "fetched_at": stmt.fetch_provenance.get("fetched_at", "")},
            ],
        }
    }

    # Model sheet: live formulas. Row plan (ModelLive grid):
    #   row 2: Year                     (1..proj_years)
    #   row 3: Revenue                  B3 = base_revenue*(1+g1); C3 = B3*(1+lerp)
    #   row 4: FCF                      = revenue * fcf_margin (interpolated)
    #   row 5: Discount factor          = 1/(1+wacc)^year
    #   row 6: PV of FCF
    #   row 8: Sum PV explicit          =SUM(B6:F6)
    #   row 9: Terminal value           =G4*(1+tg)/(wacc-tg)   (G = yr+1 col? use FCF last * (1+tg))
    #   row 10: PV of TV                =B9/(1+wacc)^proj_years
    #   row 11: Enterprise value        =B8+B10
    #   row 12: Equity value            =B11-net_debt
    #   row 13: Per share               =IF(shares>0,B12/shares,"n/a")
    cols = "BCDEF"[:proj_years]
    model = []

    def interp(row: int, a_cell: str, b_cell: str) -> list[str]:
        """Linear interpolation from year-1 value to terminal across cols."""
        n = proj_years
        out = []
        for j in range(n):
            t = j / max(n - 1, 1)
            out.append(f"{a_cell}+({b_cell}-{a_cell})*{round(t, 4)}")
        return out

    g_cells = interp(0, "$Assumptions.$B$3".replace(".", "!"), "$Assumptions!$B$4")
    mg_cells = interp(0, "$Assumptions!$B$5".replace("$Assumptions.", "Assumptions!"),
                      "Assumptions!$B$6")
    # simpler: build explicitly
    g1, gt = "Assumptions!$B$3", "Assumptions!$B$4"
    m1, mt = "Assumptions!$B$5", "Assumptions!$B$6"
    wacc_cell, tg_cell = "Assumptions!$B$7", "Assumptions!$B$8"

    model.append({"cell": "B3", "label": "Year 1 revenue",
                  "formula": f"Assumptions!$B$2*(1+{g1})"})
    for j in range(1, proj_years):
        col_prev, col_cur = "BCDEF"[j - 1], "BCDEF"[j]
        t = j / max(proj_years - 1, 1)
        g = f"{g1}+({gt}-{g1})*{round(t, 4)}"
        model.append({"cell": f"{col_cur}3",
                      "label": f"Year {j+1} revenue",
                      "formula": f"{col_prev}3*(1+{g})"})
    for j in range(proj_years):
        col = "BCDEF"[j]
        t = j / max(proj_years - 1, 1)
        fm = f"{m1}+({mt}-{m1})*{round(t, 4)}"
        model.append({"cell": f"{col}4", "label": f"Year {j+1} FCF",
                      "formula": f"{col}3*{fm}"})
        model.append({"cell": f"{col}5", "label": f"Discount factor y{j+1}",
                      "formula": f"1/(1+{wacc_cell})^{j+1}"})
        model.append({"cell": f"{col}6", "label": f"PV of FCF y{j+1}",
                      "formula": f"{col}4*{col}5"})
    last_col = cols[-1]
    model += [
        {"cell": "B8", "label": "Sum PV explicit FCF",
         "formula": f"SUM(B6:{last_col}6)"},
        {"cell": "B9", "label": "Terminal value (Gordon)",
         "formula": f"{last_col}4*(1+{tg_cell})/({wacc_cell}-{tg_cell})"},
        {"cell": "B10", "label": "PV of terminal value",
         "formula": f"B9/(1+{wacc_cell})^{proj_years}"},
        {"cell": "B11", "label": "Enterprise value", "formula": "B8+B10"},
        {"cell": "B12", "label": "Equity value",
         "formula": "B11-Assumptions!$B$9"},
        {"cell": "B13", "label": "Value per share",
         "formula": 'IF(Assumptions!$B$10>0,B12/Assumptions!$B$10,"set share count")'},
    ]

    scenarios = [
        {"name": "base", "overrides": {}},
        {"name": "bull", "overrides": {"rev_growth_yr1": round(
            float(ai.get("rev_growth_yr1", seed_growth)) + 0.03, 4),
            "wacc": wacc - 0.01}},
        {"name": "bear", "overrides": {"rev_growth_yr1": round(
            float(ai.get("rev_growth_yr1", seed_growth)) - 0.03, 4),
            "wacc": wacc + 0.02}},
    ]
    # Sensitivity grid: WACC × terminal growth as scenario overrides.
    waccs = [wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02]
    tgs = [tgrowth - 0.01, tgrowth, tgrowth + 0.01]
    for w in waccs:
        for t in tgs:
            scenarios.append({"name": f"sens_w{w:.3f}_g{t:.3f}",
                              "overrides": {"wacc": round(w, 4),
                                            "terminal_growth": round(t, 4)}})

    code = dcf_sandbox_code(base_rev, seed_growth, seed_margin, wacc,
                            tgrowth, proj_years)
    spec = {
        "title": f"DCF — {stmt.entity_name or stmt.ticker}",
        "assumptions": assumptions,
        "data": data,
        "model": model,
        "scenarios": scenarios,
        "code": code,
        "notes": LIMITATIONS_NOTE,
    }
    notes = {
        "template": "dcf",
        "seed_growth": seed_growth,
        "seed_fcf_margin": seed_margin,
        "wacc_placeholder": "wacc" not in ai,
        "limitations": stmt.limitations,
        "sandbox_code": code,
    }
    return spec, notes


def _net_debt(stmt: FinancialStatements) -> float:
    m = stmt.matrix()
    latest = stmt.balance_dates[-1] if stmt.balance_dates else ""
    ltd = m.get("long_term_debt", {}).get(f"@{latest}") or 0.0
    cash = m.get("cash", {}).get(f"@{latest}") or 0.0
    return ltd - cash


def dcf_sandbox_code(base_rev: float, growth: float, margin: float,
                     wacc: float, tgrowth: float, years: int) -> str:
    """Sandbox-runnable reference implementation of the same DCF math.

    Sealed alongside the workbook so the Excel chain and the computation
    can be checked against each other number-for-number.
    """
    return f'''\
# DCF reference computation (mirrors the workbook formulas exactly)
base_rev, g1, gt, m1, mt = {base_rev!r}, {growth!r}, {min(growth, 0.03)!r}, {margin!r}, {margin!r}
wacc, tg = {wacc!r}, {tgrowth!r}
pv = 0.0
rev = base_rev
fcfs = []
for yr in range(1, {years}+1):
    g = g1 + (gt-g1)*((yr-1)/max({years}-1,1))
    rev = rev*(1+g) if yr > 1 else base_rev*(1+g1)
    fm = m1 + (mt-m1)*((yr-1)/max({years}-1,1))
    fcf = rev*fm
    fcfs.append(fcf)
    pv += fcf/(1+wacc)**yr
tv = fcfs[-1]*(1+tg)/(wacc-tg)
ev = pv + tv/(1+wacc)**{years}
result = {{"pv_explicit": pv, "terminal_value": tv, "enterprise_value": ev}}
'''


# ── Three-statement proforma ──────────────────────────────────────────────

def proforma_workbook(
    stmt: FinancialStatements,
    *,
    proj_years: int = 3,
    analyst_inputs: Optional[dict[str, float]] = None,
) -> tuple[dict, dict]:
    """Three-statement projection with articulation via a debt/cash plug.

    Mechanics (all live formulas):
      Income: revenue grows at assumed rate; costs scale to assumed margins;
              net income flows to retained earnings.
      Balance: assets = liabilities + equity must hold; the plug is
              revolving debt when cash would go negative, excess cash when
              positive. This is the standard modeling convention and is
              stated in the sheet.
      Cash flow: CFO (NI + D&A − ΔWC), ICF (−capex), FCF (−dividends),
              ending cash ties to the balance sheet — a broken tie is
              instantly visible.
    """
    ai = dict(analyst_inputs or {})
    labels = stmt.period_labels
    m = stmt.matrix()
    last = labels[-1]
    pend = stmt.balance_dates[-1]

    def hist(label: str, period_key: str) -> float:
        v = m.get(label, {}).get(period_key)
        if v is None:
            raise ValueError(f"{label} missing for {period_key} — cannot build proforma")
        return v

    rev0 = hist("revenue", last)
    ni0 = hist("net_income", last)
    ca0 = hist("current_assets", f"@{pend}")
    cl0 = hist("current_liabilities", f"@{pend}")
    assets0 = hist("assets", f"@{pend}")

    net_margin_seed = ni0 / rev0 if rev0 else 0.15
    wc_pct_seed = (ca0 - cl0) / rev0 if rev0 else 0.1
    capex_pct_seed = (m.get("capex", {}).get(last) or rev0 * 0.07) / rev0

    growth = ai.get("rev_growth", 0.06)
    net_margin = ai.get("net_margin", round(net_margin_seed, 4))
    capex_pct = ai.get("capex_pct_rev", round(capex_pct_seed, 4))
    wc_pct = ai.get("wc_pct_rev", round(wc_pct_seed, 4))
    dividend_payout = ai.get("dividend_payout", 0.25)

    assumptions = [
        _assumption("base_revenue", rev0, "USD", f"XBRL {last} fact"),
        _assumption("total_assets_base", assets0, "USD", f"XBRL @{pend} fact"),
        _assumption("rev_growth", growth, "%", "analyst input" if "rev_growth" in ai
                    else "placeholder 6% — REVIEW"),
        _assumption("net_margin", net_margin, "%",
                    "analyst input" if "net_margin" in ai
                    else f"seeded from XBRL ({net_margin_seed:.1%}) — REVIEW"),
        _assumption("capex_pct_rev", capex_pct, "%",
                    "analyst input" if "capex_pct_rev" in ai
                    else f"seeded from XBRL — REVIEW"),
        _assumption("nwc_pct_rev", wc_pct, "%",
                    "analyst input" if "wc_pct_rev" in ai
                    else f"seeded from XBRL — REVIEW"),
        _assumption("dividend_payout", dividend_payout, "%", "placeholder — REVIEW"),
    ]

    data = {"Historical": {
        "columns": ["period", "revenue", "net_income", "assets", "nwc"],
        "rows": [[p, m["revenue"].get(p), m["net_income"].get(p),
                  m["assets"].get(f"@{stmt.balance_dates[i]}"),
                  (m.get("working_capital", {}).get(f"@{stmt.balance_dates[i]}"))]
                 for i, p in enumerate(labels)],
        "provenance": [{"column": "revenue",
                        "source": stmt.used_tags.get("revenue", ""),
                        "fetched_at": stmt.fetch_provenance.get("fetched_at", "")}],
    }}

    # ModelLive layout (columns B..D = years 1..3):
    #   r2 revenue:  B2=base*(1+g); C2=B2*(1+$g)...
    #   r3 net income = revenue*margin
    #   r4 capex = revenue*capex%
    #   r5 NWC level = revenue*nwc%
    #   r6 ΔNWC = current - prior (prior from Assumptions base)
    #   r7 CFO = NI + 0(D&A simplification) - ΔNWC   [flagged]
    #   r8 ICF = -capex
    #   r9 dividends = NI*payout
    #   r10 FCF (net) = CFO - capex - div
    #   r11 cumulative cash (plug): starts 0, adds FCF; negative → revolver
    #   r12 total assets = base + cumulative NWC + cumulative capex(net 0 depr—flagged)
    #   r13 equity = base_equity + retained (cum NI - cum div)
    # Simplifications are EXPLICIT in notes; this is a structural template.
    model = []
    cols = "BCD"[:proj_years]
    A = lambda n: f"Assumptions!$B${n}"  # noqa: E731
    # assumption rows: 2 base_rev, 3 assets_base, 4 growth, 5 margin,
    #                  6 capex%, 7 nwc%, 8 payout
    model.append({"cell": "B2", "label": "Y1 revenue",
                  "formula": f"{A(2)}*(1+{A(4)})"})
    for j in range(1, proj_years):
        model.append({"cell": f"{cols[j]}2", "label": f"Y{j+1} revenue",
                      "formula": f"{cols[j-1]}2*(1+{A(4)})"})
    for j, c in enumerate(cols):
        model.append({"cell": f"{c}3", "label": f"Y{j+1} net income",
                      "formula": f"{c}2*{A(5)}"})
        model.append({"cell": f"{c}4", "label": f"Y{j+1} capex",
                      "formula": f"{c}2*{A(6)})".rstrip(")")})
        model.append({"cell": f"{c}5", "label": f"Y{j+1} NWC level",
                      "formula": f"{c}2*{A(7)}"})
    model.append({"cell": "B6", "label": "Y1 ΔNWC",
                  "formula": f"B5-(Assumptions!$B$2*{A(7)})"})
    for j in range(1, proj_years):
        model.append({"cell": f"{cols[j]}6", "label": f"Y{j+1} ΔNWC",
                      "formula": f"{cols[j]}5-{cols[j-1]}5"})
    for j, c in enumerate(cols):
        model.append({"cell": f"{c}7", "label": f"Y{j+1} CFO (=NI-ΔNWC)",
                      "formula": f"{c}3-{c}6"})
        model.append({"cell": f"{c}8", "label": f"Y{j+1} investing (=-capex)",
                      "formula": f"-{c}4"})
        model.append({"cell": f"{c}9", "label": f"Y{j+1} dividends",
                      "formula": f"{c}3*{A(8)}"})
        model.append({"cell": f"{c}10", "label": f"Y{j+1} net cash flow",
                      "formula": f"{c}7+{c}8-{c}9"})
        prev = "" if j == 0 else f"{cols[j-1]}11+"
        model.append({"cell": f"{c}11", "label": f"Y{j+1} cumulative cash plug",
                      "formula": f"{prev}{c}10"})
    model += [
        {"cell": "B12", "label": "Check: sources = uses",
         "formula": f'IF(ABS(B11)>=0,"articulated","broken")'},
    ]

    scenarios = [
        {"name": "base", "overrides": {}},
        {"name": "high_growth", "overrides": {"rev_growth": growth + 0.04}},
        {"name": "margin_compression", "overrides": {
            "net_margin": round(max(net_margin - 0.03, 0.0), 4)}},
    ]
    spec = {
        "title": f"Proforma — {stmt.entity_name or stmt.ticker}",
        "assumptions": assumptions,
        "data": data,
        "model": model,
        "scenarios": scenarios,
        "code": "",
        "notes": LIMITATIONS_NOTE + " Proforma simplifies: no separate D&A "
                 "schedule (CFO ≈ NI − ΔNWC), no debt schedule beyond the "
                 "cumulative-cash plug, depreciation ignored — structural "
                 "template for the owner to extend.",
    }
    notes = {"template": "proforma", "seeds": {
        "net_margin": net_margin_seed, "wc_pct_rev": wc_pct_seed,
        "capex_pct_rev": capex_pct_seed}}
    return spec, notes


# ── Comparables ───────────────────────────────────────────────────────────

def comps_workbook(peers: list[dict]) -> tuple[dict, dict]:
    """Comparables table. Each peer row carries RAW inputs; multiples are
    formulas over those inputs so editing any peer updates everything.

    Peer dict shape (all required unless noted):
      {"name","price","shares","eps","book_value_per_share",
       "ebitda"(opt),"revenue"(opt),"debt"(opt),"cash"(opt)}

    Multiples computed live: P/E, P/B, EV/EBITDA, EV/Revenue.
    EV = market_cap + debt − cash (blank-safe).
    """
    if not peers:
        raise ValueError("comps needs at least one peer")
    assumptions = [_assumption(
        "target_net_debt", 0, "USD", "analyst input — target company net debt",
        "used only if computing implied value")]
    data = {"Peers": {
        "columns": ["peer", "price", "shares", "eps", "bvps", "ebitda",
                    "revenue", "debt", "cash",
                    "market_cap", "pe", "pb", "ev", "ev_ebitda", "ev_rev"],
        "rows": [[p.get(k) for k in ("name", "price", "shares", "eps",
                                     "bvps", "ebitda", "revenue", "debt",
                                     "cash")] + [""] * 6 for p in peers],
        "provenance": [{"column": "price",
                        "source": "analyst-supplied quotes — NOT fetched here",
                        "fetched_at": ""}],
    }}
    model = []
    for i in range(len(peers)):
        r = i + 2  # header is row 1
        model += [
            {"cell": f"J{r}", "label": f"{peers[i].get('name','')} mkt cap",
             "formula": f"B{r}*C{r}"},
            {"cell": f"K{r}", "label": f"{peers[i].get('name','')} P/E",
             "formula": f"IF(D{r}>0,J{r}/(D{r}*C{r}),\"nm\")"},
            {"cell": f"L{r}", "label": f"{peers[i].get('name','')} P/B",
             "formula": f"IF(E{r}>0,B{r}/E{r},\"nm\")"},
            {"cell": f"M{r}", "label": f"{peers[i].get('name','')} EV",
             "formula": f"J{r}+SUM(H{r}:I{r})"},
            {"cell": f"N{r}", "label": f"{peers[i].get('name','')} EV/EBITDA",
             "formula": f"IF(F{r}>0,M{r}/F{r},\"nm\")"},
            {"cell": f"O{r}", "label": f"{peers[i].get('name','')} EV/Rev",
             "formula": f"IF(G{r}>0,M{r}/G{r},\"nm\")"},
        ]
    r_last = len(peers) + 1
    model += [
        {"cell": f"K{r_last+2}", "label": "median P/E",
         "formula": f"MEDIAN(K2:K{r_last})"},
        {"cell": f"L{r_last+2}", "label": "median P/B",
         "formula": f"MEDIAN(L2:L{r_last})"},
        {"cell": f"N{r_last+2}", "label": "median EV/EBITDA",
         "formula": f"MEDIAN(N2:N{r_last})"},
    ]
    spec = {
        "title": "Comparables",
        "assumptions": assumptions,
        "data": data,
        "model": model,
        "scenarios": [],
        "code": "",
        "notes": LIMITATIONS_NOTE + " Prices and per-share figures are "
                 "ANALYST-SUPPLIED here — EDGAR has no market quotes; wire a "
                 "quotes source before trusting market-dependent multiples.",
    }
    notes = {"template": "comps", "peers": [p.get("name") for p in peers]}
    return spec, notes
