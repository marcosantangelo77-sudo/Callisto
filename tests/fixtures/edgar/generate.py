#!/usr/bin/env python3
"""Generate the committed EDGAR companyfacts fixtures as documented XBRL.

Hand-constructed from the SEC companyfacts schema
(https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json) because the
live API rate-limits the build machine (HTTP 403). Realistic beats real here:
the shapes mirror what real filers emit — retired tags, restatements across
accessions, mixed fiscal calendars, non-USD units, missing concepts.

Run from repo root:  python3 tests/fixtures/edgar/generate.py
Idempotent: rewrites the JSON files deterministically.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fact(start, end, val, *, accn, form="10-K", filed, fy, fp="FY",
         frame=None):
    f = {"start": start, "end": end, "val": val, "accn": accn,
         "fy": fy, "fp": fp, "form": form, "filed": filed}
    if frame:
        f["frame"] = frame
    return f


def units(*flists):
    """Merge fact lists into one USD units dict (SEC groups by unit)."""
    out: list[dict] = []
    for fl in flists:
        out.extend(fl)
    return {"USD": out}


# ── 1. LARGE FILER — "Meridian Systems Inc", December FYE, complete ────────
def meridian() -> dict:
    R = "RevenueFromContractWithCustomerExcludingAssessedTax"
    usgaap = {
        R: {"label": "Revenue from Contract with Customer",
            "units": units(
            [fact("2022-01-01", "2022-12-31", 61_498e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 74_201e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 89_770e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "CostOfRevenue": {"units": units(
            [fact("2022-01-01", "2022-12-31", 33_104e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 39_215e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 45_110e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "OperatingExpenses": {"units": units(
            [fact("2022-01-01", "2022-12-31", 18_902e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 21_441e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 25_330e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "NetIncomeLoss": {"units": units(
            [fact("2022-01-01", "2022-12-31", 9_120e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 11_803e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 15_830e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "EarningsPerShareDiluted": {"label": "Earnings Per Share Diluted", "units": {
            "USD/shares": [
                fact("2022-01-01", "2022-12-31", 1.42, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
                fact("2023-01-01", "2023-12-31", 1.81, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
                fact("2024-01-01", "2024-12-31", 2.38, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)]}},
        "Assets": {"units": units(
            [fact(None, "2023-12-31", 346_747e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact(None, "2024-12-31", 401_002e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "CashAndCashEquivalentsAtCarryingValue": {"units": units(
            [fact(None, "2023-12-31", 29_965e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact(None, "2024-12-31", 35_228e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "AssetsCurrent": {"units": units(
            [fact(None, "2024-12-31", 152_987e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "LiabilitiesCurrent": {"units": units(
            [fact(None, "2024-12-31", 78_204e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "LongTermDebtNoncurrent": {"units": units(
            [fact(None, "2024-12-31", 71_500e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "StockholdersEquity": {"units": units(
            [fact(None, "2024-12-31", 201_144e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "NetCashProvidedByUsedInOperatingActivities": {"units": units(
            [fact("2022-01-01", "2022-12-31", 22_180e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 26_410e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 31_220e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": units(
            [fact("2022-01-01", "2022-12-31", 6_910e6, accn="0000320193-23-000001", filed="2023-02-03", fy=2022),
             fact("2023-01-01", "2023-12-31", 7_855e6, accn="0000320193-24-000001", filed="2024-02-02", fy=2023),
             fact("2024-01-01", "2024-12-31", 9_102e6, accn="0000320193-25-000001", filed="2025-01-31", fy=2024)])},
    }
    return {"cik": 1880088, "entityName": "MERIDIAN SYSTEMS INC",
            "facts": {"us-gaap": usgaap}}


# ── 2. RETIRED TAGS — "Northvale Dynamics Corp" (the NVDA pattern) ─────────
# Revenue lived under SalesRevenueNet through FY2019; filer moved to
# RevenueFromContractWithCustomerExcludingAssessedTax from FY2020 onward.
# Same story on the balance sheet: LongTermDebt replaced by
# LongTermDebtNoncurrent. A naive "first candidate tag with any facts wins"
# anchors periods on facts that stop years before the present.
def northvale() -> dict:
    CUR = "RevenueFromContractWithCustomerExcludingAssessedTax"
    usgaap = {
        "SalesRevenueNet": {"label": "Sales Revenue Net (RETIRED)",
                            "units": units(
            [fact("2016-01-01", "2016-12-31", 5_010e6, accn="0001114446-17-000010", filed="2017-02-21", fy=2016),
             fact("2017-01-01", "2017-12-31", 5_920e6, accn="0001114446-18-000010", filed="2018-02-23", fy=2017),
             fact("2018-01-01", "2018-12-31", 7_140e6, accn="0001114446-19-000010", filed="2019-02-20", fy=2018),
             # comparative column in the FY2019 10-K, still the old tag:
             fact("2018-01-01", "2018-12-31", 7_140e6, accn="0001114446-20-000010", filed="2020-02-27", fy=2019)])},
        CUR: {"label": "Revenue from Contract with Customer", "units": units(
            [fact("2019-01-01", "2019-12-31", 8_260e6, accn="0001114446-20-000010", filed="2020-02-27", fy=2019),
             fact("2020-01-01", "2020-12-31", 9_310e6, accn="0001114446-21-000010", filed="2021-02-26", fy=2020),
             fact("2021-01-01", "2021-12-31", 11_450e6, accn="0001114446-22-000010", filed="2022-02-25", fy=2021),
             fact("2022-01-01", "2022-12-31", 13_890e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact("2023-01-01", "2023-12-31", 16_720e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "CostOfGoodsAndServicesSold": {"units": units(
            [fact("2021-01-01", "2021-12-31", 6_210e6, accn="0001114446-22-000010", filed="2022-02-25", fy=2021),
             fact("2022-01-01", "2022-12-31", 7_340e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact("2023-01-01", "2023-12-31", 8_470e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "CostOfRevenue": {  # retired cost tag predating the current one
            "label": "Cost of Revenue (RETIRED)", "units": units(
            [fact("2017-01-01", "2017-12-31", 3_100e6, accn="0001114446-18-000010", filed="2018-02-23", fy=2017)])},
        "NetIncomeLoss": {"units": units(
            [fact("2021-01-01", "2021-12-31", 1_980e6, accn="0001114446-22-000010", filed="2022-02-25", fy=2021),
             fact("2022-01-01", "2022-12-31", 2_610e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact("2023-01-01", "2023-12-31", 3_420e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "Assets": {"units": units(
            [fact(None, "2022-12-31", 28_410e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact(None, "2023-12-31", 33_950e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "CashAndCashEquivalentsAtCarryingValue": {"units": units(
            [fact(None, "2023-12-31", 4_105e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "AssetsCurrent": {"units": units(
            [fact(None, "2023-12-31", 14_220e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "LiabilitiesCurrent": {"units": units(
            [fact(None, "2023-12-31", 6_905e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "LongTermDebt": {"label": "Long-Term Debt (older presentation)",
                         "units": units(
            [fact(None, "2019-12-31", 5_800e6, accn="0001114446-20-000010", filed="2020-02-27", fy=2019)])},
        "LongTermDebtNoncurrent": {"units": units(
            [fact(None, "2022-12-31", 6_240e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact(None, "2023-12-31", 5_975e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "StockholdersEquity": {"units": units(
            [fact(None, "2023-12-31", 16_400e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "NetCashProvidedByUsedInOperatingActivities": {"units": units(
            [fact("2022-01-01", "2022-12-31", 4_010e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact("2023-01-01", "2023-12-31", 4_880e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": units(
            [fact("2022-01-01", "2022-12-31", 1_120e6, accn="0001114446-23-000010", filed="2023-02-24", fy=2022),
             fact("2023-01-01", "2023-12-31", 1_305e6, accn="0001114446-24-000010", filed="2024-02-23", fy=2023)])},
    }
    return {"cik": 1114446, "entityName": "NORTHVALE DYNAMICS CORP",
            "facts": {"us-gaap": usgaap}}


# ── 3. RESTATEMENT — "Cobalt Materials Ltd" ────────────────────────────────
# FY2023 revenue and net income were RESTATED in the FY2024 10-K's
# comparative columns and again in a 10-K/A; the latest filing wins, and the
# period is flagged restated because filings disagree.
def cobalt() -> dict:
    usgaap = {
        "Revenues": {"units": units(
            [fact("2022-01-01", "2022-12-31", 14_200e6, accn="0000073-23-000001", filed="2023-02-28", fy=2022),
             fact("2023-01-01", "2023-12-31", 15_900e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             # FY2022 comparative RE-presented unchanged in the next 10-K:
             fact("2022-01-01", "2022-12-31", 14_200e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             # FY2023 restated DOWN in the FY2024 10-K comparatives:
             fact("2023-01-01", "2023-12-31", 15_100e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "CostOfRevenue": {"units": units(
            [fact("2022-01-01", "2022-12-31", 9_300e6, accn="0000073-23-000001", filed="2023-02-28", fy=2022),
             fact("2023-01-01", "2023-12-31", 10_150e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             fact("2023-01-01", "2023-12-31", 10_600e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "NetIncomeLoss": {"units": units(
            [fact("2022-01-01", "2022-12-31", 2_050e6, accn="0000073-23-000001", filed="2023-02-28", fy=2022),
             fact("2023-01-01", "2023-12-31", 2_480e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             # restated DOWN twice: first in the FY2024 10-K, then a 10-K/A:
             fact("2023-01-01", "2023-12-31", 1_910e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024),
             fact("2023-01-01", "2023-12-31", 1_845e6, accn="0000073-25-000002", form="10-K/A", filed="2025-04-14", fy=2024)])},
        "Assets": {"units": units(
            [fact(None, "2023-12-31", 41_200e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             # balance sheet restated too (goodwill impairment):
             fact(None, "2023-12-31", 39_700e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024),
             fact(None, "2024-12-31", 43_100e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "CashAndCashEquivalentsAtCarryingValue": {"units": units(
            [fact(None, "2024-12-31", 2_940e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "AssetsCurrent": {"units": units(
            [fact(None, "2024-12-31", 15_800e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "LiabilitiesCurrent": {"units": units(
            [fact(None, "2024-12-31", 9_120e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "StockholdersEquity": {"units": units(
            [fact(None, "2024-12-31", 21_600e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "NetCashProvidedByUsedInOperatingActivities": {"units": units(
            [fact("2023-01-01", "2023-12-31", 3_300e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             fact("2024-01-01", "2024-12-31", 3_750e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": units(
            [fact("2023-01-01", "2023-12-31", 1_250e6, accn="0000073-24-000001", filed="2024-02-27", fy=2023),
             fact("2024-01-01", "2024-12-31", 1_410e6, accn="0000073-25-000001", filed="2025-02-25", fy=2024)])},
    }
    return {"cik": 73, "entityName": "COBALT MATERIALS LTD",
            "facts": {"us-gaap": usgaap}}


# ── 4. SPARSE FILER — "Ashfield Retail Group" (September FYE) ──────────────
# Reports revenue, net income, assets, CFO — but NO cost_of_revenue,
# NO operating expenses, NO EPS, NO capex, NO debt/equity split, and only a
# single balance-sheet date. Every hole must surface as an explicit gap.
def ashfield() -> dict:
    usgaap = {
        "Revenues": {"units": units(
            [fact("2022-10-02", "2023-09-30", 8_640e6, accn="0001558370-24-000001", filed="2024-01-11", fy=2023),
             fact("2023-10-01", "2024-09-28", 9_120e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
        "NetIncomeLoss": {"units": units(
            [fact("2022-10-02", "2023-09-30", 214e6, accn="0001558370-24-000001", filed="2024-01-11", fy=2023),
             fact("2023-10-01", "2024-09-28", 268e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
        "Assets": {"units": units(
            [fact(None, "2024-09-28", 6_980e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
        "AssetsCurrent": {"units": units(
            [fact(None, "2024-09-28", 3_410e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
        "LiabilitiesCurrent": {"units": units(
            [fact(None, "2024-09-28", 2_860e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
        "NetCashProvidedByUsedInOperatingActivities": {"units": units(
            [fact("2022-10-02", "2023-09-30", 512e6, accn="0001558370-24-000001", filed="2024-01-11", fy=2023),
             fact("2023-10-01", "2024-09-28", 604e6, accn="0001558370-25-000001", filed="2025-01-10", fy=2024)])},
    }
    return {"cik": 1558370, "entityName": "ASHFIELD RETAIL GROUP",
            "facts": {"us-gaap": usgaap}}


def main() -> None:
    for name, payload in (
        ("meridian_large_filer.json", meridian()),
        ("northvale_retired_tags.json", northvale()),
        ("cobalt_restatement.json", cobalt()),
        ("ashfield_sparse.json", ashfield()),
    ):
        p = HERE / name
        p.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
