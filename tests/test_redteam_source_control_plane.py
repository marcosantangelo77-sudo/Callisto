"""RED TEAM — source control plane: health-probe coverage, identity
deduplication, and selection reachability.

Surface: the parts of the source stack that decide what MAY run and what
COUNTS — tools/sources/health.py (probe table), the retriever's tier
ceiling (tools/pipeline/retrieval.py::retrieve), and the independence
voice-set (trace.independent_keys). The fetch/admission seam itself
(registry/query_builder/gate) is attacked by the concurrent
redteam/source-registry pass (branch redteam/source-registry @c4b5942);
this file deliberately does not re-pin its seven defects.

Method: property/coverage sweeps over the control plane's own tables —
every registered source must be probed, every probe key must resolve,
every planner-routed source must be selectable, and identical bytes must
never manufacture a second independent voice. Plus one differential pin:
the two independence call sites (trace counts spec.base_url; why.py counts
f.url) coincide only while planned URLs stay on the declared host — swept
across all adapters x a question battery and pinned so the coincidence
cannot break silently.

All offline: transports injected, urlopen monkeypatched and restored,
health thunks stubbed before any dispatch. No socket is opened.
"""
from __future__ import annotations

import json

import pytest

from tools.pipeline.engine import fixture_transport  # noqa: F401  (pre-guard)

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever,
    in_family,
    independence_key,
)
from tools.sources import health as src_health
from tools.sources.adapters import register_all  # noqa: E402
from tools.sources.base import (  # noqa: E402
    INDEPENDENCE_FAMILIES,
    RestSource,
    SourceSpec,
)
from tools.sources.registry import (  # noqa: E402
    SourceAdapter,
    SourceRegistry,
)


def _full_registry() -> SourceRegistry:
    reg = SourceRegistry()
    register_all(reg)
    return reg


# ═════════════════════════════════════════════════════════════════════════
# H — the health layer must cover the registry it claims to report on
# ═════════════════════════════════════════════════════════════════════════


def test_h1_probe_table_covers_registry_exactly():
    """Family 1 + 3: health.PROBES was written against MODULE FILENAMES
    ('sec_fts', 'cftc') while registration uses SPEC names ('sec_fulltext',
    'cftc_cot'). Four registered sources have no resolvable probe
    (cftc_cot, cmefedfut, sec_fulltext, semanticscholar-by-drift) and three
    probe keys name nothing registered. run_all(names=None) iterates the
    probe table, so cmefedfut produces NO row at all — absence reported as
    a green summary line."""
    reg = _full_registry()
    names = set(reg.names())
    probed = set(src_health.PROBES)
    assert probed == names, (
        "health probes and the source registry have drifted.\n"
        f"  registered but UNPROBED (silently absent from every health "
        f"report): {sorted(names - probed)}\n"
        f"  probe keys naming NO registered source (each reports a "
        f"permanent false verdict): {sorted(probed - names)}")


def test_h2_every_probe_key_resolves_through_build():
    """_build's alias rule only strips underscores, so 'sec_fts'→'sec_fulltext'
    and 'cftc'→'cftc_cot' do NOT resolve: _build raises KeyError, run_all
    converts that to a permanent BROKEN verdict regardless of the live API.
    A health tool that always cries broken for healthy sources trains its
    readers to ignore it — alarm fatigue is how D4-style theatre survives."""
    reg = _full_registry()
    for key in sorted(src_health.PROBES):
        try:
            src, _ad = src_health._build(key)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"health probe key {key!r} cannot resolve to a registered "
                f"source ({exc}); the probe can never run") from exc
        assert src.spec.name in reg.names()


def test_h3_default_run_all_reports_on_every_registered_source(monkeypatch):
    """run_all(names=None) walks sorted(PROBES) only. Any registered source
    without a probe entry is silently absent from the default health report
    — the summary line reads green while a whole adapter goes unchecked.
    Thunks are stubbed, so this never touches the network."""
    monkeypatch.setenv("CALLISTO_SOURCE_HEALTH_NET", "1")
    stubbed = {}
    for name, (_key_env, _thunk) in src_health.PROBES.items():

        def _ok(n=name):
            r = src_health.ProbeResult(n)
            r.verdict = src_health.OK
            r.row_count = 1
            return r

        stubbed[name] = ("", _ok)
    monkeypatch.setattr(src_health, "PROBES", stubbed)
    got = {r.source for r in src_health.run_all()}
    want = set(_full_registry().names())
    assert want <= got, (
        f"default health report omits registered sources {sorted(want - got)} "
        f"and invents non-registered rows {sorted(got - want)}")


# ═════════════════════════════════════════════════════════════════════════
# V — identical bytes are one voice, whatever host serves them
# ═════════════════════════════════════════════════════════════════════════


_BODY = json.dumps({"results": [
    {"title": "unemployment rate report", "value": 3.4},
]})


def _q(text="unemployment rate january 2023", min_ind=2):
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _registry_with(*specs):
    reg = SourceRegistry()

    def make_adapter(source):
        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    return source.get_json(
                        source.build_url("/x", {"q": "t"}))[0]
                return call

        return _Ad()

    for spec in specs:
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


_ANS = ("unemployment rate statistics",)


def test_v1_identical_bytes_from_two_hosts_are_one_independent_voice():
    """Family 5: the voice-set is keyed on DECLARED identity (adapter name /
    base_url host) and never on CONTENT. Two unrelated adapters serving the
    same bytes — a mirror pair, a live site and its wayback snapshot, one
    upstream republished — each add a key, and byte-identical evidence
    satisfies min_independent_sources=2. Corroboration of a document by
    ITSELF is manufactured here; the content hash is already computed and
    simply unused by the counting rule."""
    reg = _registry_with(
        SourceSpec(name="alpha", base_url="https://alpha.example",
                   description="", answers=_ANS, tier=1),
        SourceSpec(name="beta", base_url="https://beta.example",
                   description="", answers=_ANS, tier=1),
    )
    retr = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=fixture_transport({"alpha.example": _BODY,
                                     "beta.example": _BODY}),
        generic_calls={"alpha": ("works_search", ("term",), {}),
                       "beta": ("works_search", ("term",), {})})
    trace = retr.retrieve(_q(min_ind=2), "", min_independent=2)
    assert len(trace.independent_keys) == 1, (
        "byte-identical content from two hosts counted as TWO independent "
        f"voices: {sorted(trace.independent_keys)}")
    assert "sufficient" not in trace.stop_reason, (
        f"identical-bytes 'corroboration' satisfied min_independent_sources="
        f"2: {trace.stop_reason}")


# ═════════════════════════════════════════════════════════════════════════
# R — every planner-routed source must be reachable by the retriever
# ═════════════════════════════════════════════════════════════════════════


def test_r1_every_planned_source_is_selectable_at_retriever_ceiling():
    """The retriever hardcodes max_tier=3 (retrieval.py::retrieve,
    registry.select call). gdelt declares tier=4, has a full query-builder
    plan (_plan_gdelt) and a health probe — yet can NEVER be selected, for
    any question, silently. A registered, planned, probed source that
    cannot run is family 1 at the selection seam: the machinery exists,
    looks authoritative, and never executes. The registry holds 21 sources;
    the pipeline can reach 20 and nothing anywhere says so."""
    from tools.sources.query_builder import plannable_sources

    reg = _full_registry()
    unreachable = []
    seen = set()
    for name in plannable_sources():
        if name in seen or reg.get(name) is None:
            continue  # legacy-spelling duplicates / unregistered keys
        seen.add(name)
        entry = reg.get(name)
        # A question drawn from the source's OWN answers vocabulary is the
        # best possible selection case; if even this cannot select it, the
        # source is unreachable for every real question.
        candidates = " ".join(entry.spec.answers)
        selected = {d.name for d in reg.select_explained(
            candidates, max_tier=3) if d.included}
        if name not in selected:
            unreachable.append(
                f"{name} (tier {entry.spec.tier} > retriever ceiling 3)")
    assert not unreachable, (
        "planner-routed sources the retriever can never select:\n  " +
        "\n  ".join(unreachable))


# ═════════════════════════════════════════════════════════════════════════
# Honest negatives — attacks that did NOT land, kept as regression pins
# ═════════════════════════════════════════════════════════════════════════


def test_pin_planned_fetch_urls_stay_on_declared_host():
    """DIFFERENTIAL PIN: trace.independent_keys are computed from
    spec.base_url, why.independence_from_fetches recomputes from f.url.
    The two agree ONLY while every planned URL shares its spec's host.
    Swept all registered adapters x a question battery: no mismatch today.
    Known way to break it: wayback.fetch_snapshot() fetches web.archive.org
    against base_url archive.org — if a snapshot fetch ever enters the
    fetch list, trace keys and audit keys diverge for the same bytes."""
    from urllib.parse import urlparse

    from tools.sources import query_builder as qb

    reg = _full_registry()
    questions = [
        "What was the US unemployment rate in January 2023?",
        "Is Paris the capital of France?",
        "What does research say about semiconductor supply chains?",
        "Which banks failed this year?",
        "clinical trials for cancer vaccines",
        "average interest rates national debt yield curve",
        "news coverage of climate policy",
        "patent applications battery technology",
        "first amendment court cases",
        "GDP population life expectancy CO2 exports",
    ]
    mismatches = []
    for name in reg.names():
        entry = reg.get(name)
        try:
            plan = qb.build_plan(name, questions[0])
        except Exception:  # noqa: BLE001
            continue
        if not plan.plannable:
            continue
        src = RestSource(entry.spec, transport=lambda u, h: (200, "{}"))
        ad = entry.make_adapter(src)
        try:
            getattr(ad, plan.queries[0].method)(*plan.queries[0].args,
                                                **plan.queries[0].kwargs)
        except Exception:  # noqa: BLE001 — shape errors fine, we read rec
            pass
        rec = src.last_record
        if rec is None:
            continue
        if urlparse(rec.url).netloc != urlparse(entry.spec.base_url).netloc:
            mismatches.append(f"{name}: {rec.url}")
    assert not mismatches, (
        "planned URLs left the declared host; trace vs why independence "
        f"keys now diverge for: {mismatches}")


def test_pin_family_collapse_agrees_across_all_consumers():
    """The membership rule lives once (INDEPENDENCE_FAMILIES) and every
    consumer must collapse identically — including spelling drift, the
    exact shape that broke why.py the first time. Differential across
    independence_key / in_family / why.independence_from_fetches."""
    from types import SimpleNamespace

    from tools.why import independence_from_fetches

    variants = ["openalex", "OpenAlex", "semantic_scholar",
                "semanticscholar", "Semantic-Scholar"]
    for v in variants:
        k = independence_key(v, "https://host.example")
        assert k == next(iter(INDEPENDENCE_FAMILIES)), \
            f"{v!r} did not collapse: {k}"
        fam = next(iter(INDEPENDENCE_FAMILIES.values()))
        assert in_family(v, fam)
    acc = independence_from_fetches([
        SimpleNamespace(source_name="openalex",
                        url="https://api.openalex.org/works"),
        SimpleNamespace(source_name="semanticscholar",
                        url="https://api.semanticscholar.org/graph/v1/x"),
    ])
    assert acc.n_independent == 1, acc
    assert any("collapse" in c.lower() for c in acc.collapses)


def test_purely_empty_body_is_still_rejected():
    """Companion pin to the concurrent pass's S1: a body with NO echoable
    tokens at all ({"results": []}) must stay rejected — whatever fix lands
    for metadata-echo admission must not start admitting true emptiness."""
    from tools.pipeline.retrieval import RelevanceGate

    ok, cov, reason = RelevanceGate().judge(
        "unemployment rate january 2023", "empirical", {"results": []})
    assert not ok
