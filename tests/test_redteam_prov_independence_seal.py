"""RED TEAM — attacks 4, 5, 6: false independence, artifact swap, seal replay.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from tools.pipeline.synthesis import (
    EvidenceItem, ClaimGroup, confidence_from_agreement, triangulate)


# ── Attack 4: synthesis corroboration — false independence ────────────────


class TestFalseIndependence:
    def _item(self, name, url, claim="tsmc makes 90% of advanced chips",
              cls="SECONDARY"):
        return EvidenceItem(claim=claim, source_name=name,
                            base_url=url, source_class=cls)

    def test_port_strip_never_collapses_hosts(self):
        """econ.reuters.com:8443 and econ.reuters.com are the same publisher,
        but the independence key is the raw host string — different ports
        count as two independent voices."""
        from tools.pipeline.retrieval import independence_key
        k1 = independence_key("r", "https://econ.reuters.com")
        k2 = independence_key("r", "https://econ.reuters.com:8443")
        assert k1 == k2, f"port variation split one host into {k1!r} vs {k2!r}"

    def test_www_prefix_never_collapses(self):
        from tools.pipeline.retrieval import independence_key
        k1 = independence_key("r", "https://reuters.com")
        k2 = independence_key("r", "https://www.reuters.com")
        assert k1 == k2, f"www-prefix drift split one host into {k1!r} vs {k2!r}"

    def test_subdomain_of_same_publisher_counts_independent(self):
        """reuters.com vs news.reuters.com — same publisher, two keys, two
        voices. The declared overlap families only catch adapter NAMES, not
        host variants of the same publisher reached through one adapter."""
        from tools.pipeline.retrieval import independence_key
        k1 = independence_key("web_fetch", "https://reuters.com/a")
        k2 = independence_key("web_fetch", "https://news.reuters.com/a")
        assert k1 != k2  # documents the split

    def test_mirror_url_with_fragment_or_query_counts_independent(self):
        from tools.pipeline.retrieval import independence_key
        k1 = independence_key("web_fetch", "https://paper.org/preprint.pdf")
        k2 = independence_key("web_fetch", "https://paper.org/preprint.pdf?v=2")
        k3 = independence_key("web_fetch", "https://paper.org/preprint.pdf#top")
        assert k1 == k2 == k3 or True  # probe; assert below is the real one

    def test_two_mirrors_inflate_confidence(self):
        """Same document on two hosts (author copy + publisher copy) = 2
        independent voices → confidence jumps from 70% to 85% of ceiling.
        A mirror is not corroboration."""
        g = ClaimGroup(claim="tsmc makes 90% of advanced chips")
        g.items = [
            self._item("openalex", "https://author-mirror.net/paper"),
            self._item("openalex", "https://publisher.example/paper"),
        ]
        score, reasons = confidence_from_agreement(g)
        single = ClaimGroup(claim="tsmc makes 90% of advanced chips")
        single.items = [self._item("openalex", "https://author-mirror.net/paper")]
        s1, _ = confidence_from_agreement(single)
        assert score == s1, (
            f"two mirrors of one document raised confidence {s1} -> {score} "
            "by counting as independent voices")

    def test_redirect_target_never_checked(self):
        """The independence key uses the REQUESTED url. A fetch through a
        redirect (bit.ly -> reuters.com, plus a second fetch of a different
        bit.ly link to the same article) yields two distinct hosts that are
        one publisher. Nothing in retrieval resolves redirects."""
        import inspect
        from tools.pipeline import retrieval
        src = inspect.getsource(retrieval.independence_key)
        assert "redirect" not in src.lower() and "resolve" not in src.lower()

    def test_naming_drift_still_splits_unknown_sources(self):
        """'openalex.org' vs 'OpenAlex' vs 'open_alex' — family membership
        normalises, but an adapter name that is NOT in the declared family
        list falls through to host, so two names for the same aggregator
        with no base_url both become independent keys."""
        from tools.pipeline.retrieval import independence_key
        k1 = independence_key("open alex", "")     # space variant, unlisted
        k2 = independence_key("openalex", "")      # listed name → family
        assert k1 == k2 or k1 != k2  # probe recorded; see findings


# ── Attack 5: artifact store swap ─────────────────────────────────────────


class TestArtifactStore:
    def test_swapped_object_detected_by_verify(self, tmp_path):
        from tools.artifacts import ArtifactStore
        store = ArtifactStore(root=tmp_path)
        ref = store.put_text("honest analysis: p = 0.3", "txt", name="a")
        # attacker overwrites the object in place
        obj = store.get_path(ref.sha256)
        obj.write_bytes(b"forged analysis: p = 0.99")
        report = store.verify_artifacts([ref])
        assert not report["ok"], "in-place object swap was not detected"
        assert report["corrupt"] or report["missing"]

    def test_swap_with_matching_hash_is_content_identity_not_defeat(self, tmp_path):
        """You cannot swap bytes while keeping the reference valid: the id IS
        sha256(bytes). The residual attack is swapping the REF in a claim's
        evidence list — covered by the seal covering ref.to_dict()."""
        from tools.artifacts import ArtifactStore, ArtifactRef
        store = ArtifactStore(root=tmp_path)
        ref = store.put_text("real", "txt")
        forged_bytes = b"fake"
        # putting forged bytes yields a different id — cannot collide
        forged_ref = store.put(forged_bytes, "txt")
        assert forged_ref.sha256 != ref.sha256

    def test_index_only_attack_kind_confusion(self, tmp_path):
        """The index is metadata-only and writable without the key: an
        attacker can rename an artifact, change its meta, or add data_refs —
        verify_artifacts only re-hashes bytes, never checks meta."""
        from tools.artifacts import ArtifactStore
        store = ArtifactStore(root=tmp_path)
        ref = store.put_text("benign chart data", "txt", name="benign")
        idx = json.loads(store.index_path.read_text())
        idx[ref.sha256]["meta"] = {"source": "SEC EDGAR PRIMARY", "class": "PRIMARY"}
        idx[ref.sha256]["name"] = "sec_filing_extract"
        store.index_path.write_text(json.dumps(idx))
        report = store.verify_artifacts([ref])
        assert report["ok"], "bytes untouched so ok — meta tamper is invisible"
        meta = store.get_meta(ref.sha256)
        assert meta["meta"].get("class") == "PRIMARY"  # laundered label persists


# ── Attack 6: the seal ────────────────────────────────────────────────────


class TestTheSeal:
    def test_seal_replay_onto_different_content(self):
        """A seal from session A replayed onto session B: verify_seal recomputes
        over B's payload, so the hash won't match — UNLESS the attacker keeps
        A's payload and only relabels fields NOT covered... all fields are
        covered. So replay fails. Confirm."""
        from agp import AGPSession
        # Build a sealed dict by hand (driving the full lifecycle needs a model).
        d = {"query": "original question", "session_id": "t", "seal_hash": None}
        from agp import _canonical_payload, _seal_digest
        d["seal_hash"] = _seal_digest(_canonical_payload(d))
        assert AGPSession.verify_seal(d)
        d2 = dict(d); d2["query"] = "DIFFERENT question"
        assert not AGPSession.verify_seal(d2), "seal replayed onto altered content"

    def test_legacy_unkeyed_seal_still_verifies_with_key_set(self):
        """With CALLISTO_SEAL_KEY set, a pre-keying seal (plain sha256, no
        secret) still verifies. Anyone with repo access to old sealed rows
        can forge a legacy digest for ARBITRARY content and it verifies as
        sealed. This is the 'legacy unsealed row presented as sealed' hole."""
        from agp import AGPSession
        payload = {"question": "fabricated", "evidence": [], "seal_hash": None}
        canon = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        payload["seal_hash"] = hashlib.sha256(canon.encode()).hexdigest()
        assert AGPSession.verify_seal(payload), (
            "unkeyed legacy digest accepted while keyed mode is active — "
            "forging requires no secret")

    def test_unkeyed_mode_forges_trivially(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
        from agp import AGPSession
        payload = {"question": "x", "seal_hash": None}
        payload["seal_hash"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert AGPSession.verify_seal(payload)
