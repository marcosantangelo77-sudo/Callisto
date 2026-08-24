"""Opt-in, network-gated source-health tests.

These NEVER run in the normal suite: the whole module is skipped unless
CALLISTO_SOURCE_HEALTH_NET=1 is set in the environment. The no-socket
barrier (tests/helpers/no_socket.py) is untouched — these tests do not
import it and only activate under the explicit opt-in env var.

Run:
    CALLISTO_SOURCE_HEALTH_NET=1 python3 -m pytest \
        tests/test_source_health_live.py -v
"""

from __future__ import annotations

import os

import pytest

from tools.sources.health import (
    BROKEN,
    DEGRADED,
    NET_GATE_ENV,
    PROBES,
    OK,
    SKIPPED,
    ProbeResult,
    render_table,
    require_net_gate,
    run_all,
)

pytestmark = pytest.mark.skipif(
    os.environ.get(NET_GATE_ENV) not in ("1", "true", "yes"),
    reason=f"live-API probes; set {NET_GATE_ENV}=1 to opt in")


def test_gate_blocks_without_env(monkeypatch):
    monkeypatch.delenv(NET_GATE_ENV, raising=False)
    with pytest.raises(RuntimeError, match=NET_GATE_ENV):
        require_net_gate()


def test_every_registered_source_has_a_probe():
    """A registered source with no probe is a silent blind spot."""
    from tools.sources.registry import get_source_registry
    registered = {a.spec.name for a in
                  (get_source_registry().get(n) for n in
                   get_source_registry().names()) if a is not None}
    missing = registered - set(PROBES)
    assert not missing, f"no health probe for: {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(PROBES))
def test_source_health(name):
    results = run_all([name])
    r: ProbeResult = results[0]
    assert r.verdict != DEGRADED, (
        f"{name}: reachable but ZERO rows — silent-empty defect "
        f"(url={r.url}): {r.evidence}")
    assert r.verdict != BROKEN, (
        f"{name}: broken (url={r.url}): {r.evidence}")
    # OK or SKIPPED


def test_report_rendering_smoke():
    results = run_all(sorted(PROBES))
    table = render_table(results)
    for verdict in (OK, DEGRADED, BROKEN, SKIPPED):
        if any(r.verdict == verdict for r in results):
            assert verdict in table
