"""Offline tests for the source-health checker.

The checker itself opens real sockets when it runs — which is exactly why
these tests NEVER call run_all() or any probe against the network. What is
tested here, with a fake transport:

  1. the net gate: run_all refuses without CALLISTO_SOURCE_HEALTH_NET=1
  2. verdict classification:
     - non-empty known-good query        -> OK
     - HTTP 200 with ZERO result rows    -> DEGRADED (the ClinicalTrials /
       FDIC silent-empty failure mode must never classify as OK)
     - unreachable / HTTP error          -> BROKEN
     - shape drift on a positive payload -> BROKEN
     - keyed source without credentials  -> SKIPPED
  3. every registered source has a probe (no silently-untested registry)

Run under the normal suite; NoSocket is installed at import so an
accidental network path fails loudly.
"""

from __future__ import annotations

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from tools.sources import health as H  # noqa: E402


class FakeTransport:
    """Route table keyed by substring. Records every attempted URL."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        for needle, (status, body) in self.routes.items():
            if needle in url:
                return status, body
        return 200, "{}"


GOOD_ROWS = '{"data": [{"data": {"NAME": "x"}}], "totals": 1}'


def _make_registry(monkeypatch):
    """One-entry registry standing in for the real one."""
    from tools.sources.base import SourceSpec
    from tools.sources.registry import SourceAdapter, SourceRegistry

    spec = SourceSpec(name="fdic", base_url="https://banks.example/api",
                      description="t", answers=("bank financials",),
                      cannot_answer=(), tier=1, min_interval_s=0.0)
    reg = SourceRegistry()
    reg.register(SourceAdapter(spec=spec,
                               make_adapter=lambda src: object.__new__(
                                   type("A", (), {}))))
    return reg


def _run(reg, transport):
    # bypass the net gate for classification tests by monkeypatching it;
    # the gate itself is tested separately
    orig = H.require_net_gate
    H.require_net_gate = lambda: None
    try:
        return H.run_all(registry=reg, transport=transport)
    finally:
        H.require_net_gate = orig


def test_net_gate_blocks_run_all():
    from tools.sources.registry import SourceRegistry

    with pytest.raises(RuntimeError, match="CALLISTO_SOURCE_HEALTH_NET"):
        H.run_all(registry=SourceRegistry())


def test_ok_when_nonempty(monkeypatch):
    reg = _make_registry(monkeypatch)
    t = FakeTransport({"banks.example": (200, GOOD_ROWS)})
    results = _run(reg, t)
    assert len(results) == 1
    r = results[0]
    assert r.verdict == "OK", r.evidence
    assert "1 result row" in r.evidence
    assert t.urls, "probe never issued a request"


def test_zero_results_is_degraded_not_ok(monkeypatch):
    """THE regression this module exists for: 200-with-zero-results hid
    both the FDIC filters defect and the ClinicalTrials status-word bug."""
    reg = _make_registry(monkeypatch)
    t = FakeTransport({"banks.example": (200, '{"data": [], "totals": 0}')})
    r = _run(reg, t)[0]
    assert r.verdict == "DEGRADED", r.evidence
    assert "0 rows" in r.evidence


def test_unreachable_is_broken(monkeypatch):
    reg = _make_registry(monkeypatch)

    def dead_transport(url, headers):
        raise OSError("name resolution failed")

    r = _run(reg, dead_transport)[0]
    assert r.verdict == "BROKEN"
    assert "OSError" in r.evidence


def test_http_error_is_broken(monkeypatch):
    reg = _make_registry(monkeypatch)
    t = FakeTransport({"banks.example": (404, "not found")})
    r = _run(reg, t)[0]
    assert r.verdict == "BROKEN"


def test_nonjson_is_broken(monkeypatch):
    reg = _make_registry(monkeypatch)
    t = FakeTransport({"banks.example": (200, "<html>redirect page</html>")})
    r = _run(reg, t)[0]
    assert r.verdict == "BROKEN"


def test_keyed_source_without_credentials_skips(monkeypatch):
    from tools.sources.base import SourceSpec
    from tools.sources.registry import SourceAdapter, SourceRegistry

    spec = SourceSpec(name="fred", base_url="https://api.example/fred",
                      description="t", answers=("rates",), cannot_answer=(),
                      tier=1, min_interval_s=0.0,
                      key_env_var="CALLISTO_TEST_HEALTH_KEY")
    monkeypatch.delenv("CALLISTO_TEST_HEALTH_KEY", raising=False)
    reg = SourceRegistry()
    reg.register(SourceAdapter(spec=spec, make_adapter=lambda s: None))
    r = _run(reg, FakeTransport({}))[0]
    assert r.verdict == "SKIPPED"
    assert "CALLISTO_TEST_HEALTH_KEY" in r.evidence


def test_every_registered_source_has_a_probe():
    """A source with no probe would report SKIPPED forever and nobody
    would notice it rotting — same silence the fixtures had."""
    from tools.sources.adapters import register_all
    from tools.sources.registry import SourceRegistry

    reg = SourceRegistry()
    register_all(reg)
    unprobed = [n for n in reg.names() if n not in H.PROBES]
    extra = [n for n in H.PROBES if n not in reg.names()]
    assert not unprobed, f"registered sources without a probe: {unprobed}"
    assert not extra, f"probes for unregistered sources: {extra}"
