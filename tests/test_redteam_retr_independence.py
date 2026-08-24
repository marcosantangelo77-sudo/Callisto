"""RED TEAM — H2: independence is the load-bearing count.

Every way two dependent sources can be made to look independent:
naming drift, mirror hosts, redirects, CDNs, resellers, URL-hidden
publishers, and the host-keying of independence_key itself.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipeline.retrieval import independence_key, in_family
from tools.pipeline import synthesis as S
from tools.pipeline.synthesis import EvidenceItem, ClaimGroup, triangulate
from tools.sources.base import INDEPENDENCE_FAMILIES


# ── H2a: the key is the HOST, so any host change mints independence ──────


@pytest.mark.parametrize("name,url", [
    ("fred", "https://fred-cache.example.net"),                # cache front
    ("gdelt", "https://gdelt-proxy.aws.example"),              # proxy
    ("worldbank", "https://worldbank-mirror.example.org"),     # mirror
])
def test_mirror_or_cdn_host_mints_fake_independence(name, url):
    """independence_key() falls back to the base_url host for any source
    not in a declared family (which is 17 of 19 adapters). Two fetches
    from the same underlying corpus served via a mirror/CDN/proxy host
    produce two DIFFERENT keys — two 'independent voices' from one
    publisher. The key is literally DNS."""
    k1 = independence_key(name, url)
    k2 = independence_key(name + "_mirror", "https://" + name + ".example")
    assert k1 != k2
    assert k1 == re.sub(r"^https?://", "", url).split("/")[0]


def test_redirect_rewrites_url_and_nobody_checks():
    """RestSource records `url` — the REQUESTED url. A 3xx-following
    transport records the original URL while the bytes came from the
    redirect target; urllib follows redirects inside _http_transport and
    the final URL is discarded. So the ledger and the FetchResult can both
    attribute bytes to a host that never served them."""
    import inspect
    from tools.sources import base as B
    src = inspect.getsource(B.RestSource._http_transport)
    assert "geturl" not in src and "full_url" not in src and \
        "resp.geturl" not in src, (
        "if this fails, the final URL is now captured — defect closed")
    # Consequence: a malicious/compromised mirror can serve bytes that
    # provenance attributes to the canonical host. Nothing downstream
    # (engine, synthesis, seal) ever compares host of bytes to host of URL.


def test_family_membership_ignores_base_url_entirely():
    """The family collapse matches on NAME only. A source that resells
    OpenAlex data under its own name ('scholarly_reseller') is a distinct
    independence unit even when its base_url IS api.openalex.org."""
    k = independence_key("scholarly_reseller", "https://api.openalex.org")
    assert k == "api.openalex.org"
    # Worse: a reseller whose base_url is its OWN marketing domain but
    # whose data is 100% OpenAlex gets a fully independent key:
    k2 = independence_key("scholarly_reseller", "https://reseller.example")
    assert k2 == "reseller.example"
    # And in_family can never catch it because membership is by name:
    assert not in_family("scholarly_reseller",
                         INDEPENDENCE_FAMILIES["scholarly-aggregator"])


def test_naming_drift_leaks_for_anything_outside_the_two_families():
    """The I2 fix normalised names for the TWO declared families only.
    Any other source pair describing the same upstream data with drifted
    names ('world_bank' vs 'worldbank') falls through to host keying —
    and if their specs carry different base_urls (api.worldbank.org vs
    datasets.worldbank.org, which is REAL: the indicators API and the
    files API), the SAME publisher counts as two independent units.
    Note the asymmetry this proves: family members are keyed by NAME and
    immune to host drift; everyone else is keyed by HOST and immune to
    name matching. There is no single rule."""
    assert independence_key("worldbank", "https://api.worldbank.org") == \
        "api.worldbank.org"
    assert independence_key("worldbank_files",
                            "https://datasets.worldbank.org") == \
        "datasets.worldbank.org"
    assert independence_key("worldbank", "https://api.worldbank.org") != \
        independence_key("worldbank", "https://datasets.worldbank.org"), (
        "H2 CONFIRMED: one publisher, two real API hosts, two "
        "'independent' voices")


# ── H2b: synthesis counts items from one fetch as independent voices ─────


def _item(claim, src, url, values=(), stance=""):
    return EvidenceItem(claim=claim, source_name=src,
                        base_url=f"https://{src}.example",
                        source_class="PRIMARY", content_sha256=src,
                        url=url, values=tuple(values), stance=stance)


def test_two_urls_same_host_same_doc_two_voices_in_synthesis():
    """EvidenceItem.from_fetch defaults base_url to
    f'https://{fetch.source_name}' — the ADAPTER NAME, not the host the
    bytes came from. Any caller that omits base_url (the common case:
    the parameter defaults to '') makes the independence unit the source
    name. A publisher exposing two adapters ('openalex', 'openalex_api')
    over one corpus is two voices."""
    a = EvidenceItem.from_fetch(
        type("F", (), {"source_name": "openalex", "content_sha256": "h",
                       "url": "https://api.openalex.org/w/1",
                       "body": "value is 5%"})(),
        claim="resilience improved", source_class="PRIMARY")
    b = EvidenceItem.from_fetch(
        type("F", (), {"source_name": "openalex_api",
                       "content_sha256": "h",
                       "url": "https://api.openalex.org/w/1",
                       "body": "value is 5%"})(),
        claim="resilience improved", source_class="PRIMARY")
    # Identical content hash — literally the same bytes:
    assert a.content_sha256 == b.content_sha256
    g = ClaimGroup(claim="resilience improved", items=[a, b])
    assert g.independent_sources == 2, (
        "H2 CONFIRMED: same bytes, two adapter names, counted as two "
        "independent sources")


def test_claim_group_confidence_rises_with_minted_voices():
    """With the two 'voices' above, confidence_from_agreement grants
    _SINGLE_VOICE_FRACTION + _PER_EXTRA_VOICE of the PRIMARY ceiling —
    confidence manufactured by naming, capped only by the ceiling itself."""
    from tools.pipeline.synthesis import confidence_from_agreement
    a = _item("c", "openalex", "u1")
    b = _item("c", "openalex_api", "u2")
    score, reasons = confidence_from_agreement(
        ClaimGroup(claim="c", items=[a, b]))
    one, _ = confidence_from_agreement(ClaimGroup(claim="c", items=[a]))
    assert score > one, "two dependent items outscore one"


def test_truncation_cannot_create_contradiction_but_hash_still_differs():
    """engine._answer_leaf stores evidence content=f.body[:4000]. Two
    fetches of the SAME document whose bodies differ only after byte 4000
    produce different content_sha256 values (hash is of the full body in
    retrieval, but Evidence.content is the truncated body) — so
    ledger.has_observation(truncated) is FALSE for both: the evidence the
    session carries is not the bytes the ledger recorded. Provenance
    assignment then falls to INFERRED (or misses PRIMARY) inconsistently
    between a 3999-byte document and a 4001-byte one."""
    from agp.provenance import ProvenanceLedger
    led = ProvenanceLedger()
    body = "x" * 5000 + "DIFFERENT-TAIL"
    led.record_tool_result("openalex_fetch", body, primary=True,
                           urls=["https://h/w/1"])
    truncated = body[:4000]
    assert not led.is_primary_bytes(truncated), (
        "H-sibling CONFIRMED: the evidence content the pipeline actually "
        "stores (body[:4000]) is NOT the bytes the ledger recorded as "
        "primary — source-class assignment silently downgrades long "
        "documents, or (worse) two different long documents truncate to "
        "the SAME evidence content and become indistinguishable")
    other = ("x" * 4000 + "TOTALLY-OTHER-DOCUMENT")
    assert other[:4000] == truncated, (
        "and two documents identical in their first 4000 bytes collapse "
        "to identical Evidence.content — one document's provenance "
        "laundered onto another")


# ── H2c: property test — minting is unbounded ────────────────────────────


def test_property_any_new_host_mints_independence():
    """For any pair of hostnames, two fetches from the SAME source get two
    independence keys unless the name is in a two-entry family table.
    Independence is a property of DNS configuration, not of epistemics."""
    from hypothesis import given, settings
    from hypothesis import strategies as st

    hosts = st.from_regex(r"[a-z]{3,8}\.(com|org|net|io)", fullmatch=True)

    @given(hosts, hosts)
    @settings(max_examples=200, deadline=None)
    def prop(h1, h2):
        k1 = independence_key("some_source", f"https://{h1}")
        k2 = independence_key("some_source", f"https://{h2}")
        assert (k1 == k2) == (h1 == h2), (
            "independence tracks the hostname, not the publisher")

    prop()
