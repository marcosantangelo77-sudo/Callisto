"""Repairs for the Task-61 source-health run (2026-08-24).

Seven sources surfaced BROKEN to users as 'the literature is silent'.
This file pins each repair, offline: every fetch runs through RestSource's
injectable transport; the no-socket guard is installed before imports.
Fixtures below are LIVE CAPTURES (2026-08-24) unless marked synthetic —
a fixture passing while the live API returns nothing is precisely how
eleven defects hid.

Repairs pinned here:
  R1  registry resolves defining-module filenames ('cftc', 'sec_fts') as
      well as spec names — one shared lookup bug masked three dead probes
      as one 'cannot unpack non-iterable NoneType' TypeError
  R2  federalregister term queries use conditions[term] (bare
      conditions= gets HTTP 500) and fields[] repeats per-field
  R3  federalregister._rename walks the REAL response shapes
      ({'results':[...]}, bare document dict), restoring published_at
  R4  every health-probe name must resolve to a registered source
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from tools.sources.base import (  # noqa: E402
    RestSource,
    _RateLimiter,
)
from tools.sources.registry import get_source_registry  # noqa: E402


class UrlRecordingTransport:
    """Returns one canned JSON body for every request; records URLs."""

    def __init__(self, payload):
        self.payload = json.dumps(payload)
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        return 200, self.payload


def make_source(spec, transport):
    return RestSource(spec, ledger=None, transport=transport,
                      _limiter=_RateLimiter(0.0))


# ── LIVE CAPTURE 2026-08-24 ──────────────────────────────────────────────
# GET https://www.federalregister.gov/api/v1/documents.json
#     ?conditions%5Bterm%5D=climate&fields%5B%5D=...&per_page=2&order=newest
# Second result dropped for length; envelope + first document verbatim.
FR_LIVE_BODY = {
    "description": "Documents matching 'climate'",
    "count": 10000,
    "total_pages": 50,
    "next_page_url": (
        "https://www.federalregister.gov/api/v1/documents"
        "?conditions%5Bterm%5D=climate&fields%5B%5D=title"
        "&fields%5B%5D=type&fields%5B%5D=abstract&fields%5B%5D=action"
        "&fields%5B%5D=publication_date&fields%5B%5D=effective_on"
        "&fields%5B%5D=docket_ids&fields%5B%5D=citation"
        "&fields%5B%5D=document_number&fields%5B%5D=html_url"
        "&fields%5B%5D=agencies&format=json&order=newest&page=2"
        "&per_page=2"),
    "results": [
        {
            "title": "Montana Regulatory Program",
            "type": "Rule",
            "abstract": (
                "The Office of Surface Mining Reclamation and Enforcement "
                "(OSM) is not approving, with one exception, an amendment "
                "to the Montana regulatory program under the Surface "
                "Mining Control and Reclamation Act of 1977 (SMCRA or the "
                "Act). The Montana legislature, specifically Montana House "
                "Bill 328, proposes to add a definition of affected "
                "drainage basin to the Montana Code Annotated (MCA). "
                "Additionally, House Bill 328 proposes changes to the "
                "Montana Code Annotated, pertaining to bond release "
                "application requirements."),
            "action": "Final rule; not approving, with one exception.",
            "publication_date": "2026-08-21",
            "effective_on": "2026-09-21",
            "docket_ids": [
                "SATS No. MT-041-FOR",
                "Docket ID: OSM-2023-0002",
                "S1D1S SS08011000 SX064A000 212S180110",
                "S2D2S SS08011000 SX064A000 21XS501520",
            ],
            "citation": "91 FR 54218",
            "document_number": "2026-17055",
            "html_url": (
                "https://www.federalregister.gov/documents/2026/08/21/"
                "2026-17055/montana-regulatory-program"),
            "agencies": [
                {
                    "raw_name": "DEPARTMENT OF THE INTERIOR",
                    "name": "Interior Department",
                    "id": 253,
                    "url": (
                        "https://www.federalregister.gov/agencies/"
                        "interior-department"),
                    "json_url": (
                        "https://www.federalregister.gov/api/v1/agencies/253"),
                    "parent_id": None,
                    "slug": "interior-department",
                },
                {
                    "raw_name": (
                        "Office of Surface Mining Reclamation and "
                        "Enforcement"),
                    "name": "Surface Mining Reclamation Enforcement Office",
                    "id": 480,
                    "url": (
                        "https://www.federalregister.gov/agencies/"
                        "surface-mining-reclamation-and-enforcement-office"),
                    "json_url": (
                        "https://www.federalregister.gov/api/v1/agencies/480"),
                    "parent_id": 253,
                    "slug": (
                        "surface-mining-reclamation-and-enforcement-office"),
                },
            ],
        },
    ],
}


# ── R1: module-filename resolution in the registry ────────────────────────

@pytest.mark.parametrize("module_name,spec_name", [
    ("cftc", "cftc_cot"),
    ("sec_fts", "sec_fulltext"),
    ("semantic_scholar", "semanticscholar"),
])
def test_registry_resolves_module_filename_and_spec_name(module_name,
                                                         spec_name):
    reg = get_source_registry()
    by_module = reg.get(module_name)
    assert by_module is not None, (
        f"registry cannot resolve module filename {module_name!r} — the "
        f"shared lookup bug that masked three dead probes as one TypeError")
    assert by_module.spec.name == spec_name
    assert reg.get(spec_name) is by_module


# ── R2/R3: federalregister wire format ────────────────────────────────────

def _fr_adapter(payload):
    from tools.sources.federalregister import FederalRegisterAdapter
    t = UrlRecordingTransport(payload)
    ad = FederalRegisterAdapter(make_source(
        __import__("tools.sources.federalregister",
                   fromlist=["SPEC"]).SPEC, t))
    return ad, t


def test_fr_term_search_uses_conditions_term_not_bare_conditions():
    ad, t = _fr_adapter(FR_LIVE_BODY)
    ad.search(query_term="climate", limit=5)
    url = t.urls[0]
    # bare conditions= gets HTTP 500 (live-verified 2026-08-24)
    assert "conditions=climate" not in url
    assert "conditions%5Bterm%5D=climate" in url


def test_fr_fields_are_repeated_params_not_comma_joined():
    ad, t = _fr_adapter(FR_LIVE_BODY)
    ad.search(query_term="climate", limit=5)
    url = t.urls[0]
    assert "fields%5B%5D=title" in url
    assert "fields%5B%5D=publication_date" in url
    joined = ("title%2Ctype%2Cabstract" in url
              or "title,type,abstract" in url)
    assert not joined, f"comma-joined fields[] came back: {url}"


def test_fr_search_preserves_published_at_contract_from_live_shape():
    ad, _t = _fr_adapter(FR_LIVE_BODY)
    out = ad.search(query_term="climate", limit=5)
    doc = out["results"][0]
    # live body carries publication_date; consumers expect published_at
    assert doc.get("published_at") == "2026-08-21"
    assert "publication_date" not in doc
    # untouched envelope passes through verbatim
    assert out["count"] == 10000


def test_fr_single_document_rename_applies_to_bare_dict():
    ad, _t = _fr_adapter({
        "document_number": "2026-17055",
        "title": "Montana Regulatory Program",
        "publication_date": "2026-08-21",
    })
    out = ad.document("2026-17055")
    assert out.get("published_at") == "2026-08-21"


# ── R4: every health-probe name resolves (offline construction only) ──────

def test_every_health_probe_name_resolves_to_a_registered_source():
    from tools.sources.health import PROBES, _build

    unresolved = []
    for name in PROBES:
        try:
            src, _ad = _build(name)
        except KeyError as exc:
            unresolved.append(f"{name}: {exc}")
            continue
        assert src.spec.name, name
    assert not unresolved, (
        f"probes whose names resolve to NO registered source report "
        f"infrastructure errors instead of source health: {unresolved}")


def test_build_raises_keyerror_for_genuinely_unknown_names():
    from tools.sources.health import _build

    with pytest.raises(KeyError):
        _build("no_such_source_anywhere")


# ── R5: BEA error payloads are errors, not empty data ─────────────────────
# LIVE CAPTURE 2026-08-24 (key configured but inactive on BEA's side):
# HTTP 200 {"BEAAPI":{"Results":{"Error":{"APIErrorCode":"4",
# "APIErrorDescription":"This UserId is not active. Please activate it and
# try again."}}}} — the envelope key also moved 'BEAAPIs' -> 'BEAAPI'.

def _bea_adapter(payload):
    from tools.sources.bea import BeaAdapter
    t = UrlRecordingTransport(payload)
    ad = BeaAdapter(make_source(
        __import__("tools.sources.bea", fromlist=["SPEC"]).SPEC, t))
    ad.source.api_key = lambda: "k"
    return ad, t


def test_bea_error_payload_raises_instead_of_reading_as_zero_rows():
    ad, _t = _bea_adapter({
        "BEAAPI": {"Request": {},
                   "Results": {"Error": {
                       "APIErrorCode": "4",
                       "APIErrorDescription": (
                           "This UserId is not active. Please activate it "
                           "and try again.")}}},
    })
    with pytest.raises(Exception, match="not active"):
        ad.get_data("NIPA", "T10101", linecode="1", frequency="A",
                    years="2023")


def test_bea_new_envelope_key_beaaapi_still_yields_data():
    ad, _t = _bea_adapter({
        "BEAAPI": {"Results": {"Data": [
            {"DataValue": "22671.0", "TimePeriod": "2023"}]}},
    })
    out = ad.get_data("NIPA", "T10101", linecode="1", frequency="A",
                      years="2023")
    data = out["BEAAPI"]["Results"]["Data"]
    assert data[0]["DataValue"] == "22671.0"
    assert "_fetch" in out


def test_bea_legacy_envelope_key_beaaapis_still_accepted():
    ad, _t = _bea_adapter({
        "BEAAPIs": {"Results": {"Data": [
            {"DataValue": "1.0", "TimePeriod": "2022"}]}},
    })
    out = ad.get_data("NIPA", tablename="T10101")
    assert out["BEAAPIs"]["Results"]["Data"][0]["TimePeriod"] == "2022"
