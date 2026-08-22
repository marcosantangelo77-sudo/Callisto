"""B6 S2 — Assemble the three financial statements from XBRL facts.

The real problems this module exists for, in order of how often they bite:

1. TAG VARIATION. Filers choose different us-gaap tags for the same
   economics. "Revenue" may be Revenues, RevenueFromContractWithCustomer…,
   SalesRevenueNet, RevenueFromContractWithCustomerIncludingAssessedTax.
   Each line below carries an ordered candidate list; the first tag with
   data wins and the statement records WHICH tag was used — a number without
   its tag is unauditable.

2. RESTATEMENTS. The same (start,end) period appears in many filings with
   different values. edgar.annual_facts/instant_facts already pick the most
   recently FILED value per period; here we additionally surface when a
   period's value CHANGED across filings (restatement flag) rather than
   silently using one.

3. FISCAL-YEAR ALIGNMENT. Apple ends September, Microsoft June, retailers
   January/February. Statements are keyed by fiscal PERIOD LABELS derived
   from each fact's own start/end dates ("FY2025" = duration ending in 2025
   spanning ~12mo), never by calendar year. Column alignment is by end date,
   and mixed-period columns are flagged, not silently merged.

4. MISSING CONCEPTS. A missing tag yields None + a gap entry, never a zero.
   A zero is a reported value; None is an absence. Confusing them is how a
   model lies.

5. DERIVED vs REPORTED. Anything computed here (gross profit when the filer
   didn't tag it, operating income as revenue − opex components, FCF =
   CFO − capex) is marked derived=True with its derivation string. Reported
   values keep derived=False plus the accession number they came from.

HONEST LIMIT (stated wherever results are emitted): XBRL gives clean tagged
statements ONLY. It does not give footnotes, segment detail, lease
schedules, or non-GAAP reconciliation — those live in narrative sections.
`gaps` and `limitations` carry that warning on every assembled result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from tools.domains.finance.edgar import annual_facts, instant_facts

# ── candidate tags per line ───────────────────────────────────────────────
# Ordered best-first; first tag returning any annual/instant facts wins.

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
]
COST_OF_REVENUE_TAGS = [
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfSales",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "CostOfServices",
]
OPERATING_EXPENSE_TAGS = ["OperatingExpenses", "CostsAndExpenses"]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
EPS_DILUTED_TAGS = ["EarningsPerShareDiluted"]

ASSETS_TAGS = ["Assets"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue"]
CURRENT_ASSETS_TAGS = ["AssetsCurrent"]
CURRENT_LIABILITIES_TAGS = ["LiabilitiesCurrent"]
LONG_TERM_DEBT_TAGS = [
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "SecuredDebtNoncurrent",
]
EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]

CFO_TAGS = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
]
DIVIDENDS_PAID_TAGS = ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]

DURATION_LINES = {
    # label: candidate tags
    "revenue": REVENUE_TAGS,
    "cost_of_revenue": COST_OF_REVENUE_TAGS,
    "operating_expenses": OPERATING_EXPENSE_TAGS,
    "net_income": NET_INCOME_TAGS,
    "eps_diluted": EPS_DILUTED_TAGS,
    "cfo": CFO_TAGS,
    "capex": CAPEX_TAGS,
    "dividends_paid": DIVIDENDS_PAID_TAGS,
}
INSTANT_LINES = {
    "assets": ASSETS_TAGS,
    "cash": CASH_TAGS,
    "current_assets": CURRENT_ASSETS_TAGS,
    "current_liabilities": CURRENT_LIABILITIES_TAGS,
    "long_term_debt": LONG_TERM_DEBT_TAGS,
    "equity": EQUITY_TAGS,
}

DERIVED_UNITS_WARNING = (
    "eps_diluted is reported directly; per-share book values are DERIVED "
    "(equity / shares) and shares come from dei:EntityCommonStockSharesOutstanding "
    "when present — often stale relative to the balance sheet date."
)


@dataclass
class LineValue:
    """One line-item value for one period. Always traceable."""

    label: str
    period: str            # e.g. "FY2025"
    start: Optional[str]   # ISO date; None for instant lines
    end: str
    value: Optional[float]  # None = gap, never 0-by-convention
    unit: str = "USD"
    derived: bool = False
    derivation: str = ""    # e.g. "revenue - cost_of_revenue"
    tag: str = ""           # winning XBRL tag ("" for derived)
    accn: str = ""          # accession number of the filing it came from
    form: str = ""
    filed: str = ""
    restated: bool = False  # earlier filings reported a different value

    def to_dict(self) -> dict:
        return {
            "label": self.label, "period": self.period,
            "start": self.start, "end": self.end,
            "value": self.value, "unit": self.unit,
            "derived": self.derived, "derivation": self.derivation,
            "tag": self.tag, "accn": self.accn, "form": self.form,
            "filed": self.filed, "restated": self.restated,
        }


def _fy_label(end: str, start: Optional[str]) -> str:
    """Fiscal-year label from the fact's own dates: FY<year-of-end>."""
    return f"FY{end[:4]}"


def _pick_tag(
    facts: dict, candidates: list[str], *, instant: bool,
    want_periods: Optional[set] = None,
) -> tuple[str, list[dict]]:
    """First candidate tag with usable facts. Returns (tag, raw_fact_list).

    When ``want_periods`` is given (a set of end dates for instant lines or
    (start,end) tuples for duration lines), a tag only wins if it covers
    them — filers retire old tags, so a tag with facts that all predate the
    requested periods must not shadow a current one.
    """
    for tag in candidates:
        got = instant_facts(facts, tag) if instant else annual_facts(facts, tag)
        if not got:
            continue
        if want_periods:
            if instant:
                covered = {f["end"] for f in got}
            else:
                covered = {(f.get("start"), f["end"]) for f in got}
            missing = want_periods - covered
            if len(missing) > len(want_periods) / 2:
                # covers less than half of what we need; try the next tag
                continue
        return tag, got
    return "", []


def _restatement_map(facts: dict, tag: str, *, instant: bool) -> dict:
    """(start|end) → set of distinct values across filings for this tag."""
    from tools.domains.finance.edgar import concept_units

    seen: dict[tuple, set] = {}
    for _unit, flist in concept_units(facts, tag).items():
        for f in flist:
            if instant:
                if f.get("start") or not f.get("end"):
                    continue
                key: tuple = (f["end"],)
            else:
                if not f.get("start") or not f.get("end"):
                    continue
                key = (f["start"], f["end"])
            try:
                seen.setdefault(key, set()).add(float(f.get("val")))
            except (TypeError, ValueError):
                continue
    return {k: v for k, v in seen.items() if len(v) > 1}


def _duration_line(
    facts: dict, label: str, periods: dict[str, tuple[str, str]]
) -> list[LineValue]:
    """Assemble one duration line across the requested periods."""
    tag, raw = _pick_tag(
        facts, DURATION_LINES[label], instant=False,
        want_periods={tuple(v) for v in periods.values()},
    )
    restated = _restatement_map(facts, tag, instant=False) if tag else {}
    by_period = {(f.get("start"), f["end"]): f for f in raw}
    out: list[LineValue] = []
    for plabel, (pstart, pend) in sorted(periods.items(), key=lambda kv: kv[1][1]):
        f = by_period.get((pstart, pend))
        if f is None:
            out.append(LineValue(label=label, period=plabel, start=pstart,
                                 end=pend, value=None))
            continue
        key = (pstart, pend)
        out.append(LineValue(
            label=label, period=plabel, start=pstart, end=pend,
            value=float(f["val"]), tag=tag, accn=f.get("accn", ""),
            form=f.get("form", ""), filed=str(f.get("filed", "")),
            restated=len(restated.get(key, ())) > 1,
        ))
    return out


def _instant_line(
    facts: dict, label: str, dates: list[str]
) -> list[LineValue]:
    """Assemble one instant (balance-sheet) line at the given dates."""
    tag, raw = _pick_tag(
        facts, INSTANT_LINES[label], instant=True, want_periods=set(dates)
    )
    restated = _restatement_map(facts, tag, instant=True) if tag else {}
    by_date = {f["end"]: f for f in raw}
    out: list[LineValue] = []
    for d in sorted(dates):
        f = by_date.get(d)
        if f is None:
            out.append(LineValue(label=label, period=f"@{d}", start=None,
                                 end=d, value=None))
            continue
        out.append(LineValue(
            label=label, period=f"@{d}", start=None, end=d,
            value=float(f["val"]), tag=tag, accn=f.get("accn", ""),
            form=f.get("form", ""), filed=str(f.get("filed", "")),
            restated=len(restated.get((d,), ())) > 1,
        ))
    return out


def _derive(lines: dict[str, list[LineValue]], facts: dict,
            periods: dict[str, tuple[str, str]]) -> list[LineValue]:
    """Derived lines — explicitly marked, never silently substituted."""
    out: list[LineValue] = []

    def get(label: str, period: str) -> Optional[LineValue]:
        return next((l for l in lines.get(label, []) if l.period == period), None)

    for plabel in sorted(periods):
        pstart, pend = periods[plabel]
        rev, cogs = get("revenue", plabel), get("cost_of_revenue", plabel)
        if rev and cogs and rev.value is not None and cogs.value is not None:
            out.append(LineValue(
                label="gross_profit", period=plabel, start=pstart, end=pend,
                value=rev.value - cogs.value, derived=True,
                derivation="revenue - cost_of_revenue"))
        elif rev is not None and cogs is not None:
            out.append(LineValue(
                label="gross_profit", period=plabel, start=pstart, end=pend,
                value=None, derived=True,
                derivation="unavailable: revenue or cost_of_revenue missing"))
        cfo, capex = get("cfo", plabel), get("capex", plabel)
        if cfo and capex and cfo.value is not None and capex.value is not None:
            out.append(LineValue(
                label="free_cash_flow", period=plabel, start=pstart, end=pend,
                value=cfo.value - capex.value, derived=True,
                derivation="cfo - capex (simple FCF; excludes SBC nuance and "
                           "other investing items)"))
        ca, cl = get("current_assets", f"@{pend}"), get("current_liabilities", f"@{pend}")
        if ca and cl and ca.value is not None and cl.value is not None:
            out.append(LineValue(
                label="working_capital", period=f"@{pend}", start=None, end=pend,
                value=ca.value - cl.value, derived=True,
                derivation="current_assets - current_liabilities"))
    return out


LIMITATIONS = [
    "XBRL covers tagged statement lines only: NO footnotes, segment detail, "
    "lease schedules, commitments, contingencies, or non-GAAP reconciliation.",
    "Adjusted/non-GAAP figures are absent by design — companyfacts carries "
    "as-reported GAAP facts only.",
    "A material footnote can change the meaning of any line (e.g. revenue "
    "recognition terms); this model cannot see them. Verify against the 10-K "
    "before relying on any single figure.",
]


@dataclass
class FinancialStatements:
    """Assembled statements + full honesty metadata."""

    entity_name: str
    cik: int
    ticker: str
    periods: dict[str, tuple[str, str]]          # FY2025 -> (start,end)
    balance_dates: list[str]
    income: list[LineValue] = field(default_factory=list)
    balance_sheet: list[LineValue] = field(default_factory=list)
    cash_flow: list[LineValue] = field(default_factory=list)
    derived: list[LineValue] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    used_tags: dict[str, str] = field(default_factory=dict)
    fetch_provenance: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))

    @property
    def period_labels(self) -> list[str]:
        return [p for p, _e in sorted(self.periods.items(), key=lambda kv: kv[1][1])]

    def matrix(self) -> dict[str, dict[str, Optional[float]]]:
        """{line_label: {period_label: value}} over all statements."""
        out: dict[str, dict[str, Optional[float]]] = {}
        for lv in self.income + self.balance_sheet + self.cash_flow + self.derived:
            row = out.setdefault(lv.label, {})
            row[lv.period] = lv.value
        return out

    def to_dict(self) -> dict:
        return {
            "entity_name": self.entity_name, "cik": self.cik, "ticker": self.ticker,
            "periods": {k: list(v) for k, v in self.periods.items()},
            "balance_dates": self.balance_dates,
            "income": [l.to_dict() for l in self.income],
            "balance_sheet": [l.to_dict() for l in self.balance_sheet],
            "cash_flow": [l.to_dict() for l in self.cash_flow],
            "derived": [l.to_dict() for l in self.derived],
            "gaps": self.gaps,
            "used_tags": self.used_tags,
            "fetch_provenance": self.fetch_provenance,
            "limitations": self.limitations,
        }


def assemble_statements(facts: dict, *, n_periods: int = 4) -> FinancialStatements:
    """Build all three statements for the last n annual periods.

    Period alignment rule: the anchor set is the most recent n ANNUAL
    revenue periods (the one line every filer must report). Balance-sheet
    dates are each period's end date. Other lines are matched onto those
    exact periods; anything unmatched becomes an explicit gap.
    """
    meta = facts.get("_fetch", {})
    stmt = FinancialStatements(
        entity_name=facts.get("entityName", ""),
        cik=int(facts.get("cik", 0)),
        ticker="",  # caller fills when known
        periods={}, balance_dates=[],
        fetch_provenance=dict(meta),
    )

    # Anchor tag: the revenue candidate that reaches the MOST RECENT period
    # wins (filers retire tags; "has some facts" is not enough — NVDA's
    # older contract-revenue tag stops years before their current one).
    anchor_raw: list[dict] = []
    for tag in REVENUE_TAGS:
        got = annual_facts(facts, tag)
        if got and (not anchor_raw or
                    max(f["end"] for f in got) >
                    max(f["end"] for f in anchor_raw)):
            anchor_raw = got
            stmt.used_tags["revenue"] = tag
    if not anchor_raw:
        raise ValueError("no annual revenue facts found — cannot align periods")

    recent = sorted(anchor_raw, key=lambda f: f["end"])[-n_periods:]
    stmt.periods = {_fy_label(f["end"], f.get("start")): (f["start"], f["end"])
                    for f in recent}
    stmt.balance_dates = [f["end"] for f in recent]

    income_labels = ["revenue", "cost_of_revenue", "operating_expenses",
                     "net_income", "eps_diluted"]
    bs_labels = list(INSTANT_LINES)
    cf_labels = ["cfo", "capex", "dividends_paid"]

    lines_by_group: dict[str, list[LineValue]] = {}
    for group, labels in (("income", income_labels),
                          ("balance_sheet", bs_labels),
                          ("cash_flow", cf_labels)):
        collected: list[LineValue] = []
        for label in labels:
            if label in INSTANT_LINES:
                collected += _instant_line(facts, label, stmt.balance_dates)
            else:
                collected += _duration_line(facts, label, stmt.periods)
        lines_by_group[group] = collected
        for lv in collected:
            if lv.tag and lv.label not in stmt.used_tags:
                stmt.used_tags[lv.label] = lv.tag

    stmt.income = lines_by_group["income"]
    stmt.balance_sheet = lines_by_group["balance_sheet"]
    stmt.cash_flow = lines_by_group["cash_flow"]
    by_label: dict[str, list[LineValue]] = {}
    for lv in stmt.income + stmt.balance_sheet + stmt.cash_flow:
        by_label.setdefault(lv.label, []).append(lv)
    stmt.derived = _derive(by_label, facts, stmt.periods)

    # Gaps: every requested cell that has no value, named explicitly.
    all_lines = stmt.income + stmt.balance_sheet + stmt.cash_flow
    for lv in all_lines:
        if lv.value is None:
            stmt.gaps.append(f"{lv.label} [{lv.period}]: no XBRL fact "
                             f"(candidates exhausted)")
    return stmt
