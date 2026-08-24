"""Golden corpus for the stopping-rule study (data-driven).

Each case: question text, question_type (drives source selection),
min_independent, and a fixture route table. The route tables are built so
that the sources registry.select() actually picks are served realistic
canned bodies; irrelevant routes return junk that the gate rejects.
"""
from __future__ import annotations

import json
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures"

# ── canned bodies ──────────────────────────────────────────────────────────


def _gdelt(titles):
    return json.dumps({"articles": [
        {"title": t, "url": f"https://news{i}.example.org/a",
         "seendate": "20240110T120000"}
        for i, t in enumerate(titles)]})


def _ct(title):
    return json.dumps({"studies": [{"protocolSection": {
        "identificationModule": {"nctId": "NCT1", "briefTitle": title}}}]})


def _openalex(titles):
    return json.dumps({"results": [
        {"id": f"W{i}", "title": t, "publication_year": 2024}
        for i, t in enumerate(titles)]})


def _fred():
    return json.dumps({"seriess": [
        {"id": "CPIAUCSL", "title": "Consumer Price Index observations "
                                     "series data", "observations": []}]})


APPLE_Q = ("Will Apple report quarterly results above Wall Street consensus "
           "expectations in its next earnings report?")
BOEING_Q = ("Did Boeing complete its 737 MAX safety review before the FAA "
            "cleared the aircraft to resume passenger flights?")
FED_Q = ("Did the Federal Reserve raise the federal funds rate at its last "
         "meeting of the year?")
SEMICONDUCTOR_Q = ("What does research say about semiconductor supply chain "
                   "resilience under export controls?")

_NEWS_TITLES_APPLE = [
    "Apple quarterly earnings: results above Wall Street consensus "
    "expectations in its next report",
    "Analysts split on whether Apple results beat consensus expectations "
    "next quarter",
    "Wall Street watches Apple quarterly report for earnings surprise",
]
_NEWS_TITLES_BOEING = [
    "Boeing 737 MAX safety review completed before FAA clearance to resume "
    "flights",
    "FAA cleared the Boeing aircraft to resume passenger service after the "
    "safety review",
    "Regulators approved Boeing's return to service following the review",
]
_FRED_TITLES = ["Federal Reserve funds rate decision meeting of the year",
                "Fed raised rates at its December meeting"]


def build_cases() -> list[dict]:
    cases: list[dict] = []
    news_qtype = "news coverage of events"
    scholarly_qtype = "scholarly work search"
    econ_qtype = "economic time series observations"

    # ── family A: retrieval shapes on one leaf ────────────────────────────
    cases.append(dict(
        name="A1 news-sufficient-first-round",
        qtext=APPLE_Q, qtype=news_qtype, min_ind=2,
        routes={"/doc": _gdelt(_NEWS_TITLES_APPLE),
                "/api/v2/studies": _ct("Apple consensus expectations study "
                                       "of quarterly earnings reports")},
    ))
    cases.append(dict(
        name="A2 news-null-irrelevant-only",
        qtext=APPLE_Q, qtype=news_qtype, min_ind=2,
        routes={"/doc": _gdelt(["Mating habits of deep-sea isopods",
                                "Local bakery wins bread prize"]),
                "/api/v2/studies": _ct("Isopod deep sea mating behaviour "
                                       "trial")},
    ))
    cases.append(dict(
        name="A3 news-single-source-then-refine-fails",
        qtext=APPLE_Q, qtype=news_qtype, min_ind=3,
        routes={"/doc": _gdelt(_NEWS_TITLES_APPLE)},
    ))
    cases.append(dict(
        name="A4 scholarly-sufficient-first-round",
        qtext=SEMICONDUCTOR_Q, qtype=scholarly_qtype, min_ind=2,
        routes={"/works": _openalex([
            "Semiconductor supply chain resilience review",
            "Export controls and chip manufacturing resilience"])},
    ))
    cases.append(dict(
        name="A5 econ-time-series-sufficient",
        qtext=FED_Q, qtype=econ_qtype, min_ind=2,
        routes={"/series/search": json.dumps({"seriess": [
            {"id": "DFF", "title": "Federal funds rate meeting decision "
                                   "time series"},
            {"id": "DFEDTARU", "title": "Fed raised rates year target "
                                        "range series"}]}),},
    ))

    # ── family B: retrodiction questions through plannable sources ────────
    qfile = Path(__file__).resolve().parents[1] / \
        "data/retro_batch/questions.json"
    if qfile.exists():
        qs = json.loads(qfile.read_text())
        tickers = sorted({q["text"].split()[1] for q in qs
                          if len(q["text"].split()) > 1})
        for i, rq in enumerate(qs):
            ticker = rq["text"].split()[1] if len(rq["text"].split()) > 1 \
                else "Acme"
            cases.append(dict(
                name=f"C-retro-{i:02d}-{rq['question_type']}",
                qtext=rq["text"], qtype=news_qtype, min_ind=2,
                routes={"/doc": _gdelt([
                    f"{ticker} quarterly earnings results above Wall Street "
                    "consensus expectations report",
                    f"{ticker} analysts consensus expectations beat next "
                    "quarter report",
                    f"Wall Street {ticker} earnings surprise report"]),
                        "/api/v2/studies": _ct(f"{ticker} consensus "
                                               "expectations study")}))

    return cases
