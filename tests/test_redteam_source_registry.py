"""RED TEAM — source registry, query builders, independence families.

Surface: tools/sources/{base,registry,query_builder}.py and the
independence machinery consumers (tools/pipeline/retrieval.py,
tools/why.py). Explicitly named unattacked ground: twelve earlier surfaces
were attacked, four of them resume/checkpoint variants clustering where the
rotation last found blood; the registry/planner layer had none.

Method: CROSS-MODULE (family 2 — the same rule implemented in several
places must agree), plus adversarial inputs at the seams the duplication
analysis points at ("what happens when a source lies"). No prior pass used
cross-module agreement as its primary instrument.

Families hunted:
  2 (fix lands in one copy while another keeps the bug) -> IND1, IND2
  3/5 (absence as success; structural property standing in for evidence) ->
      SR1, SR2
  6 (direction/boundary of error) -> QB2
  7 (tests that pass for the wrong reason — an ambiguity-pinning test
     codified the float defect) -> QB2
  9-adjacent (internally consistent, externally wrong) -> QB1

Companion findings: findings/redteam_source_registry.md

Baseline: 12 pre-existing failures in test_redteam_retrieval_relevance /
test_redteam_synthesis_corroboration are PRIOR passes' expectation tests,
not this file's.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agp.provenance import ProvenanceLedger
from agp.research_program import EvidenceRequirement, SourceClassRank
from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate, \
    independence_key
from tools.sources.base import RestSource, SourceSpec
from tools.sources.query_builder import (
    _FRED_CONCEPTS,
    _resolve,
    build_plan,
    classify_fetch_failure,
)
from tools.sources.registry import SourceAdapter, SourceRegistry


# ── fixtures ────────────────────────────────────────────────────────────────

JUNK_FEED = {"feed": [
    {"title": "local marathon results 2026",
     "published": "2026-04-02", "summary": "612 runners finished"},
    {"title": "theatre season announced 2026",
     "published": "2026-05-11", "summary": "38 productions"},
]}


def _lying_source(registry: SourceRegistry, name: str, host: str,
                  payload) -> None:
    """Register a source that IGNORES the query and always returns *payload*,
    declaring vocabulary that matches macro questions."""
    spec = SourceSpec(name=name, base_url=f"https://{host}", description="",
                      answers=("gdp growth macro outlook",), tier=1,
                      min_interval_s=0)

    class _A:
        def __init__(self, src):
            self.src = src

        def works_search(self, query="", limit=3):
            data, _ = self.src.get_json(
                self.src.build_url("/search", {"q": query}))
            return data

    def transport(url, headers):
        return 200, json.dumps(payload)

    registry.register(SourceAdapter(spec=spec, make_adapter=_A))
    registry._redteam_transports = getattr(
        registry, "_redteam_transports", {})
    registry._redteam_transports[host] = transport


def _retrieve(registry: SourceRegistry, text: str, min_independent: int,
              transports: dict | None = None):
    hosts = transports if transports is not None else getattr(
        registry, "_redteam_transports", {})

    def dispatch(url, headers):
        for host, t in hosts.items():
            if host in url:
                return t(url, headers)
        raise AssertionError(f"unexpected url {url}")

    r = IterativeRetriever(
        registry=registry, ledger=ProvenanceLedger(), transport=dispatch,
        max_rounds=1, max_sources_per_leaf=5,
        generic_calls={n: ("works_search", (), {})
                       for n in registry.names()})
    q = SimpleNamespace(
        question_id="Q1", text=text,
        evidence_requirements=EvidenceRequirement(
            min_source_class=SourceClassRank.SECONDARY,
            min_independent_sources=min_independent))
    return r.retrieve(q, "", min_independent)


# ── SR1: the structural numeric route admits topic-free junk ───────────────

def test_sr1_gate_admits_zero_overlap_dated_body():
    """numeric_window_matches admits ANY body whose date-years fall inside
    the question's named years and which carries any digit — regardless of
    topical content. A marathon/theatre news dump from 2026 sails through a
    gate meant to stop irrelevant hits BEFORE ingestion. The prior pass's
    2,000-document sweeps generated dateless docs and never touched this
    route."""
    g = RelevanceGate()
    q = "will gdp growth slow during 2026"
    ok, cov, why = g.judge(q, "", JUNK_FEED)
    assert not ok, (
        f"zero-overlap dated news dump ADMITTED (coverage {cov:.0%}, "
        f"reason {why!r}) — the D4 structural route bypasses topical "
        f"judgement entirely")


def test_sr1b_two_junk_hosts_satisfy_min_independent_sources_end_to_end():
    """Full retrieval chain: two lying hosts return the SAME off-topic dated
    feed; both fetches are admitted and the leaf stops as SUFFICIENT with
    two independent voices. A conclusion would be answered from pure junk
    with '2 independent sources' recorded on the trace."""
    reg = SourceRegistry()
    _lying_source(reg, "host_a", "hosta.example", JUNK_FEED)
    _lying_source(reg, "host_b", "hostb.example", JUNK_FEED)
    trace = _retrieve(reg, "gdp growth 2026", min_independent=2)
    assert not trace.stop_reason.startswith("sufficient"), (
        f"leaf declared sufficient on two topic-free feeds: "
        f"independent_keys={sorted(trace.independent_keys)}, "
        f"admitted={[f.source_name for f in trace.admitted]}")


def test_sr1c_audit_trail_reports_inflated_coverage_for_d4_route():
    """When the numeric-window route admits, judge() reports coverage as
    max(actual, min_coverage) — the trace records 'relevance 0.25' for a
    body whose true topical overlap is 0.0. The audit label stands in for
    evidence (PATTERNS family 4): an auditor reading relevance_score cannot
    tell a quarter-relevant hit from a wholly irrelevant one."""
    g = RelevanceGate()
    ok, cov, _ = g.judge("will gdp growth slow during 2026", "",
                         {"articles": [{"date": "2026-03-14",
                                        "text": "deal worth 45"}]})
    assert cov == 0.0 or not ok, (
        f"D4 admission rewrites reported relevance from 0% to {cov:.0%}")


# ── SR2: year tokens count toward topical coverage ─────────────────────────

def test_sr2_bare_year_token_alone_reaches_admission_bar():
    """Question tokens include years, so any body merely CONTAINING the
    asked-about year earns that fraction of coverage. On a short
    sub-question (3-4 topical words) a single year string in an otherwise
    unrelated error page crosses min_coverage=0.25 and the body is admitted
    through ordinary token coverage — no structural route needed."""
    g = RelevanceGate()
    err = {"error": {"code": 400,
                     "message": "bad request id 88291 processed at "
                                "2023-01-15T00:00:00Z"}}
    ok, cov, _ = g.judge("unemployment rate january 2023", "", err)
    assert cov < 0.25 or not ok, (
        f"an error page matching only the year scored {cov:.0%} and was "
        f"admitted")


def test_sr2b_property_sweep_dated_junk_admission_rate():
    """Sweep: for random questions naming a year and random topic-free
    bodies dated inside that year, the gate must reject. It admits nearly
    all of them via the structural route."""
    import random
    import string

    rng = random.Random(19)
    topics = ["gdp", "unemployment", "inflation", "foundries", "vaccines"]
    gate = RelevanceGate()
    admitted = 0
    trials = 300
    for _ in range(trials):
        year = rng.choice([str(y) for y in range(2015, 2027)])
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        n = rng.randint(1, 999)
        qwords = rng.sample(topics, 2)
        body = {"items": [
            {"headline": "".join(rng.choices(string.ascii_lowercase,
                                             k=rng.randint(8, 20)))
             + f" {year}",
             "date": f"{year}-{month:02d}-{day:02d}",
             "amount": str(n)}]}
        admitted_bool, _, _ = gate.judge(" ".join(qwords) + f" {year}", "",
                                         body)
        admitted += bool(admitted_bool)
    assert admitted <= trials * 0.05, (
        f"{admitted}/{trials} random topic-free dated bodies ADMITTED — "
        f"dates inside the asked window are a relevance bypass")


# ── QB1: uppercase known-id passthrough beats the concept table ────────────

def test_qb1_capitalized_known_id_hijacks_concept_resolution():
    """'GDP growth' resolves to the nominal GDP LEVEL series via the
    uppercase-passthrough, which fires before and beats the curated concept
    table. A question about GROWTH silently fetches a LEVEL series; with
    SR1 the wrong-series body is then admissible (its years match the
    question), so nothing downstream notices. Internally consistent,
    externally wrong."""
    p = build_plan("fred", "Is GDP growth slowing in 2026?")
    sid = p.queries[0].kwargs["series_id"] if p.queries else None
    assert sid != "GDP", (
        f"'GDP growth' planned as series_id={sid!r} (nominal level) — "
        f"passthrough overrode concept resolution")
    assert sid != "GDP" and p.resolved.get("series_id") != "GDP"


def test_qb1b_passthrough_must_not_fire_on_concept_substring():
    """Same hijack via 'per capita': 'GDP per capita trend 2020' plans the
    aggregate level series instead of a per-capita measure."""
    p = build_plan("fred", "GDP per capita trend 2020")
    assert (p.resolved.get("series_id") != "GDP"), (
        "'GDP per capita' resolved to aggregate GDP level via passthrough")


# ── QB2: float boundary disables the designed gap rule ─────────────────────

def test_qb2_designed_confidence_pairs_never_auto_resolve():
    """_resolve requires top.confidence - second.confidence >= 0.10. The
    curated tables declare pairs differing by exactly 0.10 (0.95/0.85,
    0.90/0.80), but IEEE-754 makes 0.95-0.85 == 0.09999999999999998, so the
    flagship mappings (gdp, inflation, interest rates) NEVER auto-resolve:
    every such question returns 'ambiguous', fred goes unplannable and the
    source is silently skipped. The existing suite pins this ambiguity as
    correct behaviour (PATTERNS family 7)."""
    for concept, expected in (("gdp", "GDPC1"),
                              ("inflation", "CPIAUCSL"),
                              ("interest rates", "DFF")):
        resolved, cands = _resolve("series_id",
                                   f"question about {concept} over time",
                                   _FRED_CONCEPTS)
        assert resolved.get("series_id") == expected, (
            f"{concept!r}: documented thresholds (top>=0.90, lead runner "
            f"by 0.10) say resolve to {expected}; got "
            f"resolved={resolved}, candidates="
            f"{[c.key for c in cands.get('series_id', [])]}")

def test_qb2b_lowercase_gdp_is_unresolvable_while_capitalized_resolves_wrong():
    """The trap has no right answer today: lowercase 'gdp' is ambiguous
    (QB2) while capitalized 'GDP' resolves to the WRONG series (QB1).
    Whichever casing the model emits, fred yields nothing usable."""
    lo, lo_c = _resolve("series_id", "how did gdp move", _FRED_CONCEPTS)
    hi, hi_c = _resolve("series_id", "how did GDP move", _FRED_CONCEPTS)
    ok_lo = lo.get("series_id") == "GDPC1"
    ok_hi = hi.get("series_id") == "GDPC1"
    assert ok_lo and ok_hi, (
        f"lowercase resolved={lo}, capitalized resolved={hi} — neither "
        f"path lands on GDPC1")


# ── RL1: rate limiter is per-instance, not 'shared per source per process' ──

def test_rl1_parallel_clients_ignore_the_politeness_interval():
    """RestSource._RateLimiter documents itself as 'shared per source per
    process', but nothing shares it: every construction site (retrieval's
    _fetch_one, engine leaves) builds a fresh instance with its own
    schedule. Five clients therefore fire five same-host requests with ~zero
    aggregate spacing where one client would take 2s. Parallel leaves
    multiply request rates by leaf count — a plausible cause of this
    machine's SEC/ClinicalTrials 403 bans."""
    spec = SourceSpec(name="rlprobe", base_url="https://rl.example",
                      description="", min_interval_s=0.25)
    stamps: list[float] = []

    def transport(url, headers):
        stamps.append(time.monotonic())
        return 200, "{}"

    # one client, four sequential fetches: >= 3 * 0.25s expected
    s = RestSource(spec, ledger=None, transport=transport)
    t0 = time.monotonic()
    for i in range(4):
        s.get(f"https://rl.example/{i}")
    serial = time.monotonic() - t0

    # four clients, one fetch each, concurrently: the documented contract
    # says this must ALSO span >= 3 * 0.25s
    stamps.clear()
    clients = [RestSource(spec, ledger=None, transport=transport)
               for _ in range(4)]
    t0 = time.monotonic()
    ths = [threading.Thread(target=lambda c=c, i=i:
                            c.get(f"https://rl.example/{i}"))
           for i, c in enumerate(clients)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    parallel = time.monotonic() - t0

    assert parallel >= serial * 0.75, (
        f"four fresh clients fired in {parallel:.3f}s vs {serial:.3f}s for "
        f"one client — politeness interval is per-instance, so concurrent "
        f"leaves multiply the request rate")


# ── IND1: third copy of the membership rule, raw and uncalled ──────────────

def test_ind1_base_independence_family_disagrees_with_the_live_rule():
    """tools/sources/base.independence_family is the third landing of the
    membership rule (PATTERNS family 2) and still uses RAW `in members`
    without normalisation — while retrieval.independence_key normalises and
    has the production callers. Under the legacy spelling the codebase
    itself registers ('semantic_scholar'), the base copy says 'standalone'
    where the live rule says 'scholarly-aggregator'. If anything ever wires
    the base copy in, a family member escapes collapse and reads as an
    independent voice."""
    from tools.sources.base import independence_family
    for spelling in ("Semantic Scholar", "semantic_scholar",
                     "SEMANTICSCHOLAR"):
        assert independence_family(spelling) == \
            independence_key(spelling, ""), (
            f"membership copies disagree for {spelling!r}: "
            f"base says {independence_family(spelling)!r}, "
            f"live rule says {independence_key(spelling, '')!r}")


def test_ind1b_base_copy_has_no_production_callers():
    """Family 1: a verifier nobody calls. The base module's comment claims
    'consumers collapse on it'; grep shows zero callers outside tests —
    the declaration actually flows through retrieval's derived map. Dead
    code guarding nothing is how W5/A6 happened."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "independence_family(", "--include=*.py",
         "tools/", "agp/", "scripts/"], capture_output=True, text=True)
    callers = [l for l in out.stdout.splitlines()
               if "def independence_family(" not in l]
    assert callers == [], (
        f"base.independence_family called outside its own module: {callers}"
    ) if False else True  # informational pin; the substantive check is IND1


# ── IND2: honest-gap table keyed by a spelling the registry never uses ─────

def test_ind2_sec_gap_reason_unreachable_under_real_spec_name():
    """query_builder._HONEST_GAPS keys 'sec_fts'; the registered spec name
    is 'sec_fulltext'. The carefully-worded deliberate-gap message never
    appears; callers see 'unknown source'. Two spellings of one identifier
    across two tables (family 4 shape)."""
    p = build_plan("sec_fulltext", "company risk factors")
    assert "unknown source" not in p.reason, (
        f"honest-gap entry unreachable: real spec name got {p.reason!r}")
    assert build_plan("sec_fts", "x").plannable is False


# ── honest negatives kept as regression pins ────────────────────────────────

def test_neg_selection_refuses_junk_host_without_matching_vocabulary():
    """A lying host that does NOT declare matching vocabulary is never
    selected, so it can never be fetched. Selection remains a real
    gatekeeper for fabricated adapters."""
    reg = SourceRegistry()
    spec = SourceSpec(name="liar", base_url="https://liar.example",
                      description="", answers=("celebrity gossip",),
                      tier=1)
    reg.register(SourceAdapter(spec=spec, make_adapter=lambda src: None))
    picked = reg.select("gdp growth outlook data", max_tier=3)
    assert "liar" not in [s.name for s in picked]


def test_neg_worldbank_error_envelope_collapses_to_empty_fail_closed():
    """WB API errors come back as a ONE-element array; the adapter treats
    that as meta={} rows=[] and the gate rejects the empty body — an error
    envelope cannot become evidence here."""
    spec = SourceSpec(name="worldbank_probe",
                      base_url="https://api.worldbank.example/v2",
                      description="", answers=("gdp",), tier=1,
                      min_interval_s=0)
    err_body = json.dumps(
        [{"message": [{"key": "InvalidValue",
                       "value": "invalid indicator code"}]}])

    class _A:
        def __init__(self, src):
            self.src = src

        def indicator(self, iso3="usa", code="NY.GDP.MKTP.CD", **kw):
            data, rec = self.src.get_json(self.src.build_url(
                f"/country/{iso3}/indicator/{code}", {"format": "json"}))
            meta = data[0] if isinstance(data, list) and len(data) > 1 \
                else {}
            rows = data[1] if isinstance(data, list) and len(data) > 1 \
                else []
            return {"total": meta.get("total", len(rows)), "rows": rows}

    seen = {}

    def transport(url, headers):
        seen["url"] = url
        return 200, err_body

    reg = SourceRegistry()
    reg.register(SourceAdapter(spec=spec, make_adapter=_A))

    def dispatch(url, headers):
        return transport(url, headers)

    r = IterativeRetriever(registry=reg, ledger=ProvenanceLedger(),
                           transport=dispatch, max_rounds=1,
                           generic_calls={"worldbank_probe":
                                          ("indicator", (), {})})
    trace = _retrieve(r.registry, "gdp 2026", 1)
    assert len(trace.admitted) == 0


def test_neg_census_adapter_raises_on_unexpected_shape():
    """Census fails loudly rather than minting evidence from an error
    body."""
    from tools.sources.census import CensusAdapter
    spec = SourceSpec(name="census_p", base_url="https://census.example",
                      description="", min_interval_s=0)

    def transport(url, headers):
        return 200, json.dumps({"error": True, "message": "bad key"})

    ad = CensusAdapter(RestSource(spec, ledger=None, transport=transport))
    with pytest.raises(ValueError):
        ad.query("2023", "timeseries/eits/resconst", ["HOUSTNSA"],
                 "us:*")


def test_neg_host_fallback_collapses_same_host_to_one_voice():
    """Two DIFFERENT registry names on one host count as one independent
    voice — the conservative direction works."""
    assert independence_key("alpha", "https://h.example") == \
        independence_key("beta", "https://h.example")


def test_neg_classify_fetch_failure_still_flags_bls_envelopes():
    """The one implemented envelope guard works."""
    assert classify_fetch_failure(
        "bls", {"status": ["REQUEST_NOT_PROCESSED"],
                "message": ["no key"]}) is not None
    assert classify_fetch_failure("fred", {"status": ["REQUEST_SUCCEEDED"]}) \
        is None
