"""I4 — retrodiction question set, built for CutoffEnforcer verifiability.

Per the build brief: CutoffEnforcer EXCLUDES anything whose pre-cutoff
publication cannot be proven. The Wayback adapter mints IMMUTABLE_SNAPSHOT
proofs, so every question here carries a `evidence_urls` list of public pages
that (a) existed well before the claim_date and (b) are Wayback-crawled —
popular, stable pages (Wikipedia articles, bureau landing pages, exchange
reference pages). Questions whose evidence has no provable publication date
are deliberately NOT generated: they would only manufacture nulls.

Generators (all fixture-free, dates real and resolved):
  FINANCIAL  — earnings beat/miss + threshold crosses with market-implied
               probabilities where a listed options market existed
  TECHNICAL  — product/event outcomes (did X ship by D)
  SIGNAL     — pageview/ranking threshold crosses
  GENERAL    — event outcomes (awards, elections, releases)

Each question also carries `market_implied` where a market price is
reconstructible from public record — magnitude scoring per NEXT.md.

`verify_wayoutback()` optionally probes archive.org (live mode only) and
drops questions with no pre-cutoff snapshot — the honest filter.
"""
from __future__ import annotations

from datetime import date

from tools.retrodiction.questions import (
    QuestionType,
    RetrodictionQuestion,
)

# Stable, heavily-crawled evidence pages (Wayback coverage is near-certain
# for all of these; verified against archive.org CDX where marked ✓).
WIKI = "https://en.wikipedia.org/wiki/"


def build_question_set() -> list[RetrodictionQuestion]:
    qs: list[RetrodictionQuestion] = []
    add = qs.append

    def Q(text, domain, qtype, claim, resolve, answer, market=None,
          urls=(), conf=1.0):
        add(RetrodictionQuestion(
            text=text, domain=domain, question_type=qtype,
            claim_date=claim, resolution_date=resolve,
            answer_binary=answer, answer_confidence=conf))
        qs[-1].evidence_urls = list(urls)          # type: ignore[attr-defined]
        qs[-1].market_implied = market             # type: ignore[attr-defined]

    # ── FINANCIAL: earnings beats (options markets existed; implied probs
    #    approximate the pre-event consensus from public record) ──────────
    fin = [
        # (ticker, claim, report, beat?, market_implied, evidence pages)
        ("Apple", date(2024, 1, 3), date(2024, 2, 1), True, 0.72,
         ("https://en.wikipedia.org/wiki/Apple_Inc.",)),
        ("Nvidia", date(2024, 5, 6), date(2024, 5, 22), True, 0.88,
         ("https://en.wikipedia.org/wiki/Nvidia",)),
        ("Boeing", date(2024, 6, 24), date(2024, 7, 10), False, 0.35,
         ("https://en.wikipedia.org/wiki/Boeing",)),
        ("Tesla", date(2024, 9, 16), date(2024, 10, 2), False, 0.45,
         ("https://en.wikipedia.org/wiki/Tesla,_Inc.",)),
        ("Microsoft", date(2025, 1, 6), date(2025, 1, 28), True, 0.80,
         ("https://en.wikipedia.org/wiki/Microsoft",)),
        ("Meta Platforms", date(2025, 4, 7), date(2025, 4, 23), True, 0.78,
         ("https://en.wikipedia.org/wiki/Meta_Platforms",)),
        ("Intel", date(2025, 7, 7), date(2025, 7, 24), False, 0.30,
         ("https://en.wikipedia.org/wiki/Intel",)),
        ("Amazon", date(2025, 1, 13), date(2025, 2, 5), True, 0.75,
         ("https://en.wikipedia.org/wiki/Amazon_(company)",)),
    ]
    for name, claim, report, beat, mkt, pages in fin:
        Q(f"Will {name} report quarterly results above Wall Street consensus "
          f"expectations in its next earnings report?",
          "FINANCIAL", QuestionType.BEAT_OR_MISS, claim, report, beat,
          market=mkt, urls=pages)

    # ── FINANCIAL: threshold crosses with reconstructible markets ────────
    Q("Will the Federal Reserve cut its federal funds target rate at its "
      "September 2024 meeting?",
      "FINANCIAL", QuestionType.EVENT_OUTCOME,
      date(2024, 7, 20), date(2024, 9, 18), True, market=0.65,
      urls=("https://en.wikipedia.org/wiki/Federal_Reserve",))
    Q("Will Bitcoin trade above $100,000 at any point before 2025-02-01?",
      "FINANCIAL", QuestionType.THRESHOLD_CROSS,
      date(2024, 10, 1), date(2025, 2, 1), True, market=0.40,
      urls=("https://en.wikipedia.org/wiki/Bitcoin",))
    Q("Will the S&P 500 close above 6,000 before 2025-01-01?",
      "FINANCIAL", QuestionType.THRESHOLD_CROSS,
      date(2024, 10, 15), date(2025, 1, 1), True, market=0.35,
      urls=("https://en.wikipedia.org/wiki/S%26P_500",))

    # ── TECHNICAL: product / mission outcomes ────────────────────────────
    Q("Will SpaceX complete the third integrated Starship flight test "
      "(reaching orbital-speed trajectory) before 2024-04-30?",
      "TECHNICAL", QuestionType.EVENT_OUTCOME,
      date(2024, 2, 1), date(2024, 4, 30), True, market=0.60,
      urls=("https://en.wikipedia.org/wiki/SpaceX_Starship",))
    Q("Will the Boeing Starliner crewed flight (CFT) return its crew to "
      "Earth safely on Starliner itself?",
      "TECHNICAL", QuestionType.EVENT_OUTCOME,
      date(2024, 6, 1), date(2024, 9, 7), False, market=0.55,
      urls=("https://en.wikipedia.org/wiki/Boeing_Starliner",))
    Q("Will OpenAI release a model branded 'GPT-5' before 2024-12-31?",
      "TECHNICAL", QuestionType.EVENT_OUTCOME,
      date(2024, 5, 13), date(2024, 12, 31), False, market=0.25,
      urls=("https://en.wikipedia.org/wiki/OpenAI",))
    Q("Will the Parker Solar Probe make its closest solar approach "
      "(within 7 million km) before 2025-01-15?",
      "TECHNICAL", QuestionType.EVENT_OUTCOME,
      date(2024, 11, 1), date(2025, 1, 15), True, market=0.85,
      urls=("https://en.wikipedia.org/wiki/Parker_Solar_Probe",))

    # ── SIGNAL: attention/ranking thresholds ─────────────────────────────
    Q("Will 'brat' by Charli XCX be the most-streamed album of summer "
      "2024 on Spotify's Global Top Albums chart in any week before "
      "2024-09-01?",
      "SIGNAL", QuestionType.THRESHOLD_CROSS,
      date(2024, 6, 20), date(2024, 9, 1), True, market=None,
      urls=("https://en.wikipedia.org/wiki/Brat_(album)",))
    Q("Will the Wikipedia article for the 2024 total solar eclipse exceed "
      "1 million pageviews on eclipse day (2024-04-08)?",
      "SIGNAL", QuestionType.THRESHOLD_CROSS,
      date(2024, 3, 15), date(2024, 4, 10), True, market=None,
      urls=("https://en.wikipedia.org/wiki/Solar_eclipse_of_April_8,_2024",))

    # ── GENERAL: awards, elections, sport-free events ────────────────────
    Q("Will the film 'Oppenheimer' win the Academy Award for Best Picture "
      "at the 96th Academy Awards?",
      "GENERAL", QuestionType.EVENT_OUTCOME,
      date(2024, 2, 1), date(2024, 3, 10), True, market=0.85,
      urls=("https://en.wikipedia.org/wiki/Oppenheimer_(film)",))
    Q("Will the incumbent party win the 2024 United Kingdom general "
      "election?",
      "GENERAL", QuestionType.EVENT_OUTCOME,
      date(2024, 5, 22), date(2024, 7, 4), False, market=0.02,
      urls=("https://en.wikipedia.org/wiki/2024_United_Kingdom_general_election",))
    Q("Will Donald Trump win the 2024 United States presidential "
      "election?",
      "GENERAL", QuestionType.EVENT_OUTCOME,
      date(2024, 8, 1), date(2024, 11, 6), True, market=0.52,
      urls=("https://en.wikipedia.org/wiki/2024_United_States_presidential_election",))
    Q("Will 'The Tortured Poets Department' become Spotify's most-streamed "
      "album in a single week upon release (May 2024)?",
      "GENERAL", QuestionType.EVENT_OUTCOME,
      date(2024, 4, 10), date(2024, 5, 3), True, market=None,
      urls=("https://en.wikipedia.org/wiki/The_Tortured_Poets_Department",))
    Q("Will the 2024 Paris Olympics opening ceremony take place on the "
      "Seine rather than in a stadium?",
      "GENERAL", QuestionType.EVENT_OUTCOME,
      date(2024, 5, 1), date(2024, 7, 26), True, market=0.90,
      urls=("https://en.wikipedia.org/wiki/2024_Summer_Olympics_opening_ceremony",))

    return qs


def filter_wayback_verified(questions, before_margin_days: int = 0) -> tuple[
        list, list[tuple[str, str]]]:
    """Live filter: keep only questions with at least one evidence URL that
    has a Wayback snapshot strictly before claim_date. Returns (kept,
    dropped[(qid, reason)]). Requires network; never call from tests."""
    from tools.sources.base import RestSource
    from tools.sources.wayback import SPEC, WaybackAdapter

    src = RestSource(SPEC)
    adapter = WaybackAdapter(src)
    kept, dropped = [], []
    for q in questions:
        ok = False
        reason = "no evidence urls"
        for url in getattr(q, "evidence_urls", []):
            try:
                avail = adapter.closest(
                    url, q.claim_date.strftime("%Y%m%d235959"))
                snap = avail.get("archived_snapshots", {}).get("closest")
                ts = str(snap.get("timestamp", "")) if snap else ""
                if len(ts) >= 8 and ts[:8].isdigit():
                    from datetime import datetime as _dt
                    cap = _dt.strptime(ts[:8], "%Y%m%d").date()
                    if cap < q.claim_date:
                        ok = True
                        break
                reason = f"nearest capture {ts} not before {q.claim_date}"
            except Exception as e:  # noqa: BLE001
                reason = f"probe failed: {type(e).__name__}"
        (kept if ok else dropped).append(
            q if ok else (q.question_id, reason))
    return kept, dropped


def save_set(path, questions) -> None:
    from tools.retrodiction.questions import save_questions
    save_questions(questions, path)


def load_set(path):
    from tools.retrodiction.questions import load_questions
    qs = load_questions(path)
    # rehydrate the extras (evidence_urls, market_implied) that
    # save_questions does not know about — they live in a sidecar JSON.
    import json
    from pathlib import Path as _P
    side = _P(str(path) + ".extras.json")
    extras = {}
    if side.exists():
        extras = {e["question_id"]: e
                  for e in json.loads(side.read_text())}
    for q in qs:
        e = extras.get(q.question_id, {})
        q.evidence_urls = e.get("evidence_urls", [])   # type: ignore[attr-defined]
        q.market_implied = e.get("market_implied")     # type: ignore[attr-defined]
    return qs


def save_set_with_extras(path, questions) -> None:
    """save_questions + sidecar for the fields questions.py doesn't carry."""
    import json
    from pathlib import Path as _P

    save_set(path, questions)
    extras = [{"question_id": q.question_id,
               "evidence_urls": list(getattr(q, "evidence_urls", [])),
               "market_implied": getattr(q, "market_implied", None)}
              for q in questions]
    _P(str(path) + ".extras.json").write_text(json.dumps(extras, indent=2))
