"""RED TEAM — attacks 1, 2: memory reinjection & wiki compilation.

Question under attack: can content that left the system as INFERRED re-enter
carrying a better class than it earned?
"""
from __future__ import annotations

import hashlib
import json

import pytest

# ── Attack 1: memory reinjection ──────────────────────────────────────────


class TestMemoryReinjection:
    def test_unsealed_class_above_inferred_is_capped(self):
        from tools.memory_epistemics import admit_learning
        a = admit_learning(key="k", confidence=0.95, source="claude",
                           source_class="PRIMARY")
        assert a.source_class == "INFERRED"
        assert a.stored_confidence <= 0.55

    def test_seal_from_legacy_unkeyed_era_verifies(self):
        """THE ESCALATION PATH. verify_learning_seal accepts the LEGACY
        UNKEYED sha256 digest as valid proof of a seal. Any learning can
        therefore claim SECONDARY/PRIMARY provenance by hashing its own
        session dict with plain sha256 — no secret required."""
        from tools.memory_epistemics import (
            _canonical_payload, verify_learning_seal)

        forged_session = {
            "session_id": "forged-1",
            "evidence": [{"content": "I assert this is PRIMARY"}],
            "conclusion": "whatever I like",
            "seal_hash": None,
        }
        legacy_hash = hashlib.sha256(
            json.dumps(forged_session, sort_keys=True,
                       ensure_ascii=False).encode("utf-8")).hexdigest()

<<<<<<< HEAD
        assert verify_learning_seal(forged_session, legacy_hash) is False, (
=======
        assert verify_learning_seal(forged_session, legacy_hash) is True, (
>>>>>>> origin/build/dd-decomposition-diversity
            "unkeyed sha256 accepted as seal verification — an attacker "
            "without CALLISTO_SEAL_KEY can mint seals for claimed-class "
            "escalation")

    def test_end_to_end_class_escalation_via_forged_legacy_seal(self):
        """Full chain: INFERRED guess in → 'sealed PRIMARY' out,
        using only public functions and no key material."""
        from tools.memory_epistemics import admit_learning

        session = {"session_id": "s", "conclusion": "x", "seal_hash": None}
        seal_hash = hashlib.sha256(json.dumps(session, sort_keys=True).encode()).hexdigest()

        admission = admit_learning(
            key="my_guess", confidence=0.99, source="claude",
            source_class="PRIMARY",          # lie
            seal_session=session, seal_hash=seal_hash)  # self-minted

        assert admission.source_class == "INFERRED", (
            "self-minted legacy seal escalated an INFERRED learning to "
            f"{admission.source_class} (stored {admission.stored_confidence})")
        assert admission.stored_confidence <= 0.55

    def test_reinjection_row_without_source_class_reads_as_inferred(self):
        """Rows written before P4 have NULL source_class in the DB; the
        reinjection SELECT does not even read the column. annotate must
        still cap them — it does default to INFERRED, but confirm the
        ceiling actually travels and nothing above survives."""
        from tools.memory_epistemics import annotate_for_reinjection
        row = {"confidence": 0.9}   # pre-P4 row: no class field at all
        out = annotate_for_reinjection(row)
        assert out["source_class"] == "INFERRED"
        assert out["effective_confidence"] <= 0.55

    def test_reoccurrence_resets_decay_clock(self):
        """Decay defense: re-recording refreshes learned_at. A stale INFERRED
        learning kept alive by periodic re-writes never decays below the
        admission threshold — decay is defeated by mere repetition."""
        from datetime import datetime, timedelta, timezone
        from tools.memory_epistemics import CONFIDENCE_HALF_LIFE_DAYS, decay_confidence
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=CONFIDENCE_HALF_LIFE_DAYS * 4)
        assert decay_confidence(0.55, old.isoformat(), now) < 0.1
        refreshed = now - timedelta(minutes=1)
        assert decay_confidence(0.55, refreshed.isoformat(), now) > 0.5
        # record_learning sets learned_at = NOW on every upsert (hermes_memory
        # line ~311), so occurrences+1 keeps eff-confidence pinned at max.


# ── Attack 2: wiki compilation ────────────────────────────────────────────


class TestWikiCompilation:
    def test_empty_sources_default_confidence_is_half(self):
        """An article compiled from NO sources gets confidence 0.5 — the same
        value the wiki's historical admission gate treats as compile-worthy.
        Nothing earned it."""
        from tools.knowledge_wiki import _article_confidence
        c = _article_confidence([])
        assert c <= 0.3, f"empty-source article scored {c}"

    def test_missing_confidence_defaults_to_half_per_source(self):
        """A source dict with no confidence key silently counts as 0.5.
        An attacker (or a buggy writer) omitting the field manufactures a
        0.5 floor for the whole article."""
        from tools.knowledge_wiki import _article_confidence
        c = _article_confidence([{"id": "a"}, {"id": "b"}])  # no confidence
<<<<<<< HEAD
        assert c == 0.0  # fail closed: omitting confidence must pull DOWN
=======
        assert c == 0.5  # documents the defect if it holds
>>>>>>> origin/build/dd-decomposition-diversity

    def test_no_source_class_anywhere_in_wiki_confidence(self):
        """Article confidence is computed ONLY from float confidences; there
        is no source-class term at all. Two INFERRED 0.55 items and two
        PRIMARY 1.0 items are indistinguishable to the compiler."""
        import inspect
        from tools import knowledge_wiki
        src = inspect.getsource(knowledge_wiki._article_confidence)
        src += inspect.getsource(knowledge_wiki._merged_article_confidence)
        assert "source_class" not in src, (
            "wiki confidence ignores provenance class entirely")
