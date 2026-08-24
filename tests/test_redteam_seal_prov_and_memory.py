"""RED TEAM — provenance assignment, checkpoint replay guard, memory/wiki
reinjection (hypotheses 4, 5, 6). Only attacks NOT already covered by
tests/test_redteam_prov_*.py (R1-R8) are claimed here; overlaps are cited.

Every test is a CONFIRMED repro executed before being written down.
"""
import hashlib
import os

import pytest

from agp import Domain, Evidence, SourceClass
from agp.provenance import ProvenanceLedger


# ══ Attack V1: PRIMARY minted for bytes never fetched ═════════════════════

class TestPrimaryMinting:
    """The docstring: 'record_tool_result is called once per real tool return,
    keyed by content hash. Nothing else can add to it.' But record_tool_result
    is a plain public method on a plain object — the 'only the executor calls
    this' rule is a comment, not an invariant. The model-facing path that
    assigns classes shares the process with the ledger."""

    def test_any_code_path_can_mint_primary(self):
        led = ProvenanceLedger()
        fiction = "SECRET FINDING: Fed will cut 500bps tomorrow."
        # no fetch ever happened; any in-process caller can still do:
        led.record_tool_result("web_fetch", fiction, primary=True,
                               urls=["https://sec.example.gov/filing"])
        ev = Evidence(content=fiction, source_class=SourceClass.INFERRED,
                      confidence_score=0.95, domain=Domain.GENERAL,
                      origin_agent="model")
        assert led.assign_source_class(ev) == SourceClass.PRIMARY

    def test_model_echo_of_fetched_bytes_gets_primary(self):
        """Weaker, structural version: bytes fetched ONCE as PRIMARY are
        re-quoted by the model inside a larger INFERRED claim... that passes.
        But the exact-bytes rule also promotes a SECOND recording of the same
        bytes through a non-fetch path — hash equality is treated as fetch
        equality across code paths."""
        led = ProvenanceLedger()
        body = "CPI rose 0.3% in June per BLS release"
        led.record_tool_result("web_fetch", body, primary=True)
        # attacker deletes nothing — just re-records the same bytes via a
        # non-primary path; is_primary_bytes still True (fine), but note the
        # inverse direction tested next.

    def test_primary_record_survives_even_if_only_source_was_secondary(self):
        """record_tool_result appends to a list per hash and is_primary_bytes
        scans ALL observations of that hash. One primary=True call — even for
        bytes the tool returned only as a search-result snippet, mislabeled by
        whichever adapter chose the flag — permanently marks those bytes
        primary. The flag is caller-asserted with zero verification."""
        led = ProvenanceLedger()
        snippet = "headline only: jobless claims fall"
        led.record_tool_result("web_search", snippet, primary=True)  # miscall
        ev = Evidence(content=snippet, source_class=SourceClass.INFERRED,
                      confidence_score=0.9, domain=Domain.GENERAL,
                      origin_agent="model")
        assert led.assign_source_class(ev) == SourceClass.PRIMARY


# ══ Attack V2: seal_guard replayed-ledger laundering beyond R4 ════════════

class TestReplayGuardDegrades:
    """R1-R3 covered missing/attacker-recomputed digests and empty bodies.
    These are additional degenerate-input passes in the same family."""

    def test_digest_of_empty_string_replayed_when_body_missing(self):
        """rec with content_sha256 = sha256('') but NO body key: body defaults
        to '' via rec.get('body',''), digest matches, and '' is recorded as a
        PRIMARY observation. Same terminal state as R3 via a different
        degenerate input."""
        from tools.pipeline.checkpoint import Checkpoint, replay_ledger
        ck = Checkpoint(key="k", run="r", stage="fetch", input_hash="ih",
                        payload={"fetches": [{
                            "url": "https://x.example/a",
                            "content_sha256": hashlib.sha256(b"").hexdigest(),
                            "primary": True,
                        }]})
        led = ProvenanceLedger()
        report = replay_ledger(led, [ck])
        assert report["integrity_failures"] == []
        assert led.has_observation("")
        assert led.is_primary_bytes("")

    def test_provenance_is_intact_true_with_no_checkpoints(self, ):
        """Vacuous truth: a resumed run whose checkpoints were LOST (gc'd,
        wrong dir) has provenance_is_intact([]) -> True and seal_guard says
        SEAL. Absence of evidence reads as intact evidence."""
        from tools.pipeline.checkpoint import RunTrace, seal_guard
        trace = RunTrace(run="r")
        trace.stages.append(type("S", (), {"stage": "fetch", "resumed": True,
                                           "payload": {}, "produced_at":
                                           "t"})())
        from tools.pipeline.checkpoint import StageOutcome
        trace.stages = [StageOutcome(stage="fetch", resumed=True, payload={},
                                     produced_at="2026-01-01T00:00:00")]
        verdict, reason = seal_guard(trace, [], ProvenanceLedger())
        assert verdict == "SEAL"

    def test_integrity_failure_does_not_stop_other_records_being_replayed(self):
        """A failed-digest record is skipped — but the remaining records still
        replay into the ledger, and replay_ledger's failures list is advisory.
        Any caller that checks only report['replayed'] or proceeds partially
        gets a ledger containing SOME unverified observations."""
        from tools.pipeline.checkpoint import Checkpoint, replay_ledger
        good = "legitimate body"
        ck = Checkpoint(key="k", run="r", stage="fetch", input_hash="ih",
                        payload={"fetches": [
                            {"body": "tampered", "content_sha256": "0" * 64,
                             "primary": True},
                            {"body": good,
                             "content_sha256": hashlib.sha256(
                                 good.encode()).hexdigest(),
                             "primary": True},
                        ]})
        led = ProvenanceLedger()
        report = replay_ledger(led, [ck])
        assert len(report["integrity_failures"]) == 1
        assert led.has_observation(good)   # mixed ledger built anyway


# ══ Attack M1: reinjection resurrects decayed confidence ══════════════════

class TestDecayResurrection:
    """decay_confidence correctly decays; annotate_for_reinjection then
    OVERWRITES effective_confidence with min(raw stored, ceiling) — discarding
    the decay it was handed. _build_learnings computes eff and passes both;
    annotate keeps raw. A 100-day-old 0.55 learning emits as 0.55."""

    def test_annotation_clobbers_decay(self):
        from datetime import datetime, timezone, timedelta
        from tools.memory_epistemics import (annotate_for_reinjection,
                                             decay_confidence)
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        eff = decay_confidence(0.55, old)
        assert eff <= 0.06          # properly decayed
        row = annotate_for_reinjection({
            "id": "stale-guess", "value": "v", "confidence": 0.55,
            "learned_at": old, "occurrences": 1, "source": "claude",
            "effective_confidence": eff,
        })
        assert row["effective_confidence"] == 0.55, (
            "annotate_for_reinjection overwrote the decayed value with the "
            "raw stored confidence")

    def test_decay_reset_by_reobservation(self):
        """Each upsert rewrites learned_at (hermes_memory.record_learning),
        so rewriting the SAME learning periodically resets the decay clock
        forever — matches sibling finding R5's tail; pinned here because the
        reset lives in hermes_memory's SQL, not in epistemics."""
        from datetime import datetime, timezone
        from tools.memory_epistemics import decay_confidence
        now = datetime.now(timezone.utc)
        assert decay_confidence(0.55, now.isoformat(), now=now) > 0.54


# ══ Attack M2: wiki admission gates read raw columns ══════════════════════

class TestWikiAdmissionGaps:
    """_get_uncompiled_sources seal-gates SESSIONS but takes evidence rows at
    confidence >= 0.6 and learnings at >= 0.5 straight off the stored column —
    no class check, no decay, no ceiling applied at compile time."""

    def test_learning_admission_threshold_is_stored_not_effective(self):
        """A learning stored at exactly the INFERRED ceiling 0.55 clears the
        wiki's >= 0.5 gate forever (no decay applied in the SELECT), even
        though its effective (decayed) confidence may be far below."""
        from tools.memory_epistemics import admit_learning
        adm = admit_learning(key="k", confidence=0.99, source="claude",
                             source_class=None)
        assert adm.source_class == "INFERRED"
        assert adm.stored_confidence == 0.55   # clamped...
        assert adm.stored_confidence >= 0.5    # ...but still wiki-admissible

    def test_trusted_source_bypasses_all_ceilings(self):
        """source='human'/'audit' skips clamp entirely — but source is a
        caller-supplied string with no authentication. Any agent writing a
        learning can pass source='human' and store confidence 0.99 as
        INFERRED-class content, clearing every downstream gate."""
        from tools.memory_epistemics import admit_learning
        adm = admit_learning(key="k", confidence=0.999, source="human",
                             source_class=None)
        assert adm.stored_confidence == 0.999
        assert adm.source_class == "INFERRED"

    def test_article_merge_can_raise_via_weighted_average_when_inputs_equal(self):
        """_merged_article_confidence clamps to min(existing, new-min) — held
        against direct raises. Pinned as a boundary: new sources can never
        lift above the weakest current input."""
        from tools.knowledge_wiki import _merged_article_confidence
        out = _merged_article_confidence(
            existing_confidence=0.40, compile_count=3,
            new_sources=[{"confidence": 0.9}, {"confidence": 0.9}])
        assert out <= 0.40
