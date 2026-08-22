"""I4 — question-set construction (scripts/retro_questions_i4.py).

The set must be: resolvable, cutoff-verifiable in principle (evidence pages
that Wayback provably crawled before claim_date), spread across domains,
and round-trippable through save/load with the extras sidecar.
No network.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from retro_questions_i4 import (  # noqa: E402
    build_question_set,
    load_set,
    save_set_with_extras,
)
from tools.retrodiction.questions import QuestionType  # noqa: E402


def test_set_is_nonempty_and_valid():
    qs = build_question_set()
    assert len(qs) >= 15
    for q in qs:
        assert q.validate() == [], q.question_id
        # every question is genuinely resolved history
        assert q.resolution_date < date(2026, 8, 1)
        assert q.horizon_days > 0


def test_every_question_carries_wayback_verifiable_evidence():
    """CutoffEnforcer excludes anything without a provable pre-cutoff
    publication date — so every question MUST name at least one stable
    evidence URL. Questions without one are exactly how batches fill up
    with nulls; the generator refuses to emit them."""
    for q in build_question_set():
        urls = getattr(q, "evidence_urls", [])
        assert urls, f"{q.question_id} has no evidence urls"
        assert all(u.startswith("https://") for u in urls)


def test_domains_and_types_spread():
    qs = build_question_set()
    domains = {q.domain for q in qs}
    assert {"FINANCIAL", "TECHNICAL", "SIGNAL", "GENERAL"} <= domains
    types = {q.question_type for q in qs}
    assert QuestionType.BEAT_OR_MISS in types
    assert QuestionType.EVENT_OUTCOME in types


def test_magnitude_questions_have_market_implied():
    """NEXT.md: score against magnitude where a market exists. The financial
    questions carry devigged market-implied probabilities; non-market
    questions carry None and fall back to binary."""
    fin = [q for q in build_question_set() if q.domain == "FINANCIAL"]
    assert all(getattr(q, "market_implied", None) is not None for q in fin)
    for q in fin:
        assert 0.0 <= q.market_implied <= 1.0


def test_save_load_roundtrip_preserves_extras(tmp_path):
    qs = build_question_set()[:3]
    p = tmp_path / "qs.json"
    save_set_with_extras(p, qs)
    loaded = load_set(p)
    assert len(loaded) == 3
    by_id = {q.question_id: q for q in loaded}
    for q in qs:
        assert getattr(by_id[q.question_id], "market_implied") \
            == getattr(q, "market_implied")
        assert getattr(by_id[q.question_id], "evidence_urls") \
            == getattr(q, "evidence_urls")
