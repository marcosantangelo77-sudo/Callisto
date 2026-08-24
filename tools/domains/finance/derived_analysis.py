"""Finance instantiation of the derived-analysis loop.

Relationships chosen because (a) every input line already exists in the
assembled statements, and (b) each has a defensible normal range that is
DERIVED FROM THE ENTITY'S OWN HISTORY by the generic engine — nothing here is
a textbook constant. A relationship whose behaviour is stable for a given
company has a tight band; deviations from THAT company's own norm are what
trigger research. Ratios nobody looks at are not added; these five are the
ones whose abnormal behaviour is classically diagnostic.
"""
from __future__ import annotations

from typing import Optional

from tools.derived_analysis import Relationship

# series is {label: {period_label: value}} from FinancialStatements.matrix().


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return num / den


RELATIONSHIPS: list[Relationship] = [
    Relationship(
        key="accruals_share",
        description="net income vs operating cash flow (earnings backed by cash?)",
        unit="ratio",
        compute=lambda s: {
            p: v for p in _periods(s)
            if (v := _ratio(
                (s.get("net_income") or {}).get(p),
                (s.get("cfo") or {}).get(p))) is not None
        },
    ),
    Relationship(
        key="cash_conversion",
        description="free cash flow vs net income",
        unit="ratio",
        compute=lambda s: {
            p: v for p in _periods(s)
            if (v := _ratio(
                (s.get("free_cash_flow") or {}).get(p),
                (s.get("net_income") or {}).get(p))) is not None
        },
    ),
    Relationship(
        key="gross_margin",
        description="gross profit / revenue",
        unit="ratio",
        compute=lambda s: {
            p: v for p in _periods(s)
            if (v := _ratio(
                (s.get("gross_profit") or {}).get(p),
                (s.get("revenue") or {}).get(p))) is not None
        },
    ),
    Relationship(
        key="capex_intensity",
        description="capex / revenue",
        unit="ratio",
        compute=lambda s: {
            p: v for p in _periods(s)
            if (v := _ratio(
                (s.get("capex") or {}).get(p),
                (s.get("revenue") or {}).get(p))) is not None
        },
    ),
    Relationship(
        key="current_ratio",
        description="current assets / current liabilities",
        unit="ratio",
        compute=lambda s: {
            p: v for p in _balance_periods(s)
            if (v := _ratio(
                (s.get("current_assets") or {}).get(p),
                (s.get("current_liabilities") or {}).get(p))) is not None
        },
    ),
]


def _periods(s: dict) -> list[str]:
    """Duration periods present on revenue (the anchor line)."""
    rev = s.get("revenue") or {}
    return [p for p in rev if not p.startswith("@")]


def _balance_periods(s: dict) -> list[str]:
    ca = s.get("current_assets") or {}
    return sorted(ca)


def statements_series(stmt) -> dict[str, dict[str, Optional[float]]]:
    """FinancialStatements → generic-engine series (its matrix())."""
    return stmt.matrix()


def finance_focus_period(stmt) -> str:
    """The newest assembled fiscal year — where anomalies are looked for."""
    return stmt.period_labels[-1]
