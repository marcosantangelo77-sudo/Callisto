"""CME 30-Day Fed Funds futures (ZQ) settlements — the market's own policy
forecast, and the documented derivation of market-implied FOMC probabilities
from it.

PROVENANCE CLASS — this module carries TWO distinct classes and never blurs
them:

  PRIMARY (tier 3: market prices) — the exchange's official daily settlement
    price for each ZQ contract month, fetched from CME's free public
    settlements feed. A settlement is an exchange-published fact with the
    trade date stamped by CME itself; it is provably dated BEFORE any
    meeting that falls after its publication day. Every derived number in
    this file is computed FROM a payload whose sha256 and URL are recorded
    into the ProvenanceLedger by RestSource.

  INFERRED — anything this module computes from those settlements:
    expected average EFFR, expected rate change at a meeting, and the
    market-implied probability of a hike/cut. These follow the published
    CME FedWatch methodology ("Understanding the CME Group FedWatch Tool
    Methodology", cmegroup.com): ZQ is priced at 100 − average EFFR for the
    contract month; under the standard assumptions (25 bp discrete moves,
    deterministic intra-month path) the expected change divided by 25 bp,
    split across the days before/after the meeting, yields the probability.
    The FedWatch API itself is PAID ($25/mo+); we deliberately consume only
    the FREE settlements and derive locally, which keeps every number
    reproducible from the ledgered raw payload.

What this source provides is therefore MARKET-IMPLIED PROBABILITIES (derived),
not realised levels. Realised levels (SP500, DGS10, T10Y2Y via FRED) are a
different thing entirely and must never be used as a benchmark.

PUBLICATION TIMESTAMPS (W5): a benchmark mis-dated after the event it
"predicted" is worse than no benchmark. Guards enforced here:
  - every settlement row carries CME's own trade date; attach_market_implied
    refuses any question whose claim_date is not STRICTLY AFTER the
    settlement's trade date;
  - the meeting date must fall inside the contract month, else no answer.

READ-ONLY: public market-data GETs only. No wallet, keys, order path, or
account access anywhere in this module. Network calls go through RestSource
and are constructed ONLY when CALLISTO_ENABLE_NETWORK=1 is set (opt-in); all
tests run on injectable transports behind the no-socket guard.

Answers: market-implied probability of a 25bp-multiple target-rate move at a
FOMC meeting inside a ZQ contract month, plus the underlying settlement
curve. Cannot answer: probabilities for meetings outside available contract
months, non-rate macro events (CPI etc. — use Kalshi), intraday/pre-settlement
probabilities, and anything requiring CME credentials.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from tools.sources.base import RestSource, SourceSpec

SPEC = SourceSpec(
    name="cmefedfut",
    base_url=(
        "https://www.cmegroup.com/CmeWS/mvc/Settlements/TradeDate/305"
    ),
    description=(
        "CME 30-Day Fed Funds futures settlements: exchange primary prices "
        "plus locally derived FedWatch-style market-implied FOMC "
        "probabilities"
    ),
    answers=(
        "market-implied probability of a Fed target-rate move at an FOMC "
        "meeting inside a fed funds futures contract month (derived from "
        "exchange settlements)",
        "official CME daily settlement prices for ZQ contract months",
        "expected average effective fed funds rate implied per contract month",
    ),
    cannot_answer=(
        "realised interest rates or equity levels — this supplies "
        "market-IMPLIED probabilities derived from futures prices, not "
        "outcomes (use fred for realised series)",
        "FOMC meetings outside the listed ZQ contract months",
        "intraday or pre-settlement probabilities (CME's FedWatch API is "
        "paid; we use free end-of-day settlements only)",
        "non-policy macro events such as CPI prints (use kalshi event "
        "contracts)",
        "any authenticated CME operation — read-only public data by mandate",
    ),
    tier=3,
    min_interval_s=1.0,
    terms_url="https://www.cmegroup.com/market-data/terms-of-use.html",
)

# Product id 305 = 30-Day Federal Funds futures (ZQ) on CmeWS settlements.
PRODUCT_ID = "305"

_SETTLE_URL_TMPL = (
    "https://www.cmegroup.com/CmeWS/mvc/Settlements/TradeDate/"
    f"{PRODUCT_ID}/%s?tradeDate=0&pageSize=500"
)

_MONTH_CODE = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}
_MONTH_NUM = {v: k for k, v in _MONTH_CODE.items()}

_CONTRACT_RE = re.compile(r"^ZQ([FGHJKMNQUVXZ])(\d{1,4})$")
_SETTLE_RE = re.compile(r"^\d{2}:\d{2}$")


class CmeFedFutAdapter:
    """Read-only client over the free public settlements feed + derivation."""

    def __init__(self, source: RestSource):
        self.source = source

    # ── PRIMARY: exchange settlements ─────────────────────────────────────

    def settlements(self, trade_date: str) -> dict:
        """Official settlements for one CME trade date ('YYYYMMDD').

        Returns {'settlements': [rows], 'trade_date': str, '_fetch':
        provenance}. The rows are the exchange's own records — PRIMARY,
        tier 3, sha256-ledgered by RestSource before any derivation runs.
        """
        td = str(trade_date).strip()
        if not re.fullmatch(r"\d{8}", td):
            raise ValueError(f"bad CME trade date {trade_date!r}")
        url = self.source.build_url(
            "", {"tradeDate": td, "pageSize": 500})
        data, rec = self.source.get_json(url)
        return {
            "settlements": list(data.get("settlements", [])),
            "trade_date": td,
            "_fetch": {"url": rec.url, "sha256": rec.content_sha256,
                       "fetched_at": rec.fetched_at},
        }

    def zq_curve(self, trade_date: str) -> dict[str, dict]:
        """ZQ contract-month settlements as {month_code: {...}} where key is
        like 'ZQZ5' and values carry 'price' (100 − expected avg EFFR) and
        the raw row. Only rows whose product matches ZQ are kept."""
        out = self.settlements(trade_date)
        curve: dict[str, dict] = {}
        for row in out["settlements"]:
            sym = str(row.get("product", "")).strip().upper()
            if not _CONTRACT_RE.match(sym):
                continue
            settle = row.get("settle")
            if settle in (None, "") or not _SETTLE_RE.match(str(settle)):
                continue
            price = float(str(settle).replace(":", "."))
            curve[sym] = {
                "price": price,                      # 100 − avg EFFR (percent)
                "expected_effr": round(100.0 - price, 4),
                "raw": row,
            }
        curve["_fetch"] = out["_fetch"]              # type: ignore[assignment]
        curve["_trade_date"] = out["trade_date"]     # type: ignore[assignment]
        return curve

    # ── INFERRED: FedWatch-style probabilities ────────────────────────────

    def implied_probability(self, meeting_date: str, current_rate_pct: float,
                            curve: Optional[dict[str, dict]] = None,
                            trade_date: str = "") -> Optional[dict]:
        """Market-implied probability of ANY change at `meeting_date`.

        meeting_date : 'YYYY-MM-DD' FOMC decision date
        current_rate_pct : current target-range UPPER bound, percent
                           (e.g. 5.50 for 5.25–5.50 %)
        curve : pre-fetched zq_curve (offline/tests), else fetched for
                `trade_date` (the last settlement strictly BEFORE you ask).

        Methodology (published CME FedWatch derivation):
          expected_end = avg EFFR implied by the meeting's contract month
          days_before  = meeting day-of-month minus 1 (EFFR unchanged until
                         the meeting-day-effective new rate)
          expected_change_at_meeting =
              (expected_end * N − current * D) / d   where N = days in
              month, D = days before the meeting, d = days after
          probability = expected_change / 0.25  (25 bp increments)

        Returns None when the meeting does not fall inside an available
        contract month — honest absence beats a fabricated benchmark.
        The result is INFERRED: derived from the PRIMARY settlement payload,
        whose provenance rides along under '_fetch'.
        """
        if curve is None:
            if not trade_date:
                raise ValueError("need pre-fetched curve or trade_date")
            curve = self.zq_curve(trade_date)

        import datetime as _dt

        md = _dt.date.fromisoformat(meeting_date)
        key = f"ZQ{_MONTH_CODE[md.month]}{md.year % 100:02d}"
        entry = curve.get(key)
        if entry is None:
            return None
        n_days = (_dt.date(md.year + (md.month == 12),
                           (md.month % 12) + 1, 1) - md.replace(day=1)).days
        days_before = md.day - 1
        days_after = n_days - days_before
        if days_after <= 0:
            return None
        expected_end = entry["expected_effr"]
        # Average-EFFR identity: expected_end*N = current*D + post*d,
        # where `post` is the implied post-meeting level.
        post_level = ((expected_end * n_days
                       - current_rate_pct * days_before) / days_after)
        expected_change = post_level - current_rate_pct
        prob_change = max(0.0, min(1.0, expected_change / 0.25))
        direction = ("hike" if expected_change > 0.0001
                     else "cut" if expected_change < -0.0001 else "none")
        return {
            "meeting_date": meeting_date,
            "contract": key,
            "probability_of_change": round(prob_change, 6),
            "direction": direction,
            "expected_change_bp": round(expected_change * 100, 3),
            "expected_effr": expected_end,
            "current_rate_upper_pct": current_rate_pct,
            "provenance_class": "INFERRED",
            "derived_from": {
                "class": "PRIMARY",
                "tier": SPEC.tier,
                "source": SPEC.name,
                "trade_date": curve["_trade_date"],
                "fetch": curve["_fetch"],
            },
        }


def make_adapter() -> CmeFedFutAdapter:
    """Opt-in live client. Raises unless CALLISTO_ENABLE_NETWORK=1."""
    if os.environ.get("CALLISTO_ENABLE_NETWORK") != "1":
        raise RuntimeError(
            "cmefedfut network access is opt-in; set "
            "CALLISTO_ENABLE_NETWORK=1 to construct the live client")
    from tools.sources.base import RestSource as _RS
    return CmeFedFutAdapter(_RS(SPEC))


# ── Wiring onto retrodiction questions ────────────────────────────────────
#
# RetrodictionQuestion.market_implied already feeds tools/simulation.py's
# edge computation and batch.magnitude_score. This is the adapter→question
# bridge so beat-market rate becomes computable on macro/rate questions.

def attach_market_implied(questions, probs_by_question_id: dict[str, float],
                          *, strict_dates: bool = True) -> dict[str, str]:
    """Set q.market_implied from {question_id: probability}.

    W5 guard: refuses to attach when the question's claim_date is NOT
    strictly after every benchmark's provenance trade date. Callers pass
    trade dates via probs_by_question_id values being plain floats; for
    date-provable attachment use attach_from_derived() instead, which takes
    full implied_probability() dicts. Returns skipped {qid: reason}.
    """
    return attach_from_derived(
        questions,
        {qid: {"probability_of_change": p} for qid, p
         in probs_by_question_id.items()},
        strict_dates=strict_dates,
        allow_no_provenance=True,
    )


def attach_from_derived(questions, derived_by_qid: dict,
                        *, strict_dates: bool = True,
                        allow_no_provenance: bool = False
                        ) -> dict[str, str]:
    """Attach full implied_probability() dicts to questions.

    Each value needs 'probability_of_change' in [0,1]. With strict_dates
    (default) a value whose provenance trade date is >= the question's
    claim_date is REFUSED — a benchmark dated after what it predicts would
    be leakage, and leakage benchmarks score better than honesty ever
    could. Values without provenance are refused unless explicitly allowed
    (test-only escape hatch).
    """
    import datetime as _dt

    skipped: dict[str, str] = {}
    for q in questions:
        d = derived_by_qid.get(q.question_id)
        if d is None:
            continue
        try:
            prob = float(d["probability_of_change"])
        except (KeyError, TypeError, ValueError):
            skipped[q.question_id] = "missing/invalid probability_of_change"
            continue
        if not 0.0 <= prob <= 1.0:
            skipped[q.question_id] = f"probability {prob} outside [0,1]"
            continue
        prov = d.get("derived_from") or {}
        trade_date = str(prov.get("trade_date", ""))
        if strict_dates and q.claim_date is not None and trade_date:
            td = _dt.datetime.strptime(trade_date, "%Y%m%d").date()
            if td >= q.claim_date:
                skipped[q.question_id] = (
                    f"benchmark trade_date {trade_date} not strictly before "
                    f"claim_date {q.claim_date} — refusing (W5)")
                continue
        elif strict_dates and not trade_date and not allow_no_provenance:
            skipped[q.question_id] = "no provenance trade_date; refusing"
            continue
        q.market_implied = prob
    return skipped
