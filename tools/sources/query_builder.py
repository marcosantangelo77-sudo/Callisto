"""Query authoring — turning a sub-question into a well-formed source query.

The live end-to-end run (MORNING_REPORT, "THE REAL BACKLOG" #2) showed the
pipeline can SELECT the right sources but cannot ASK them anything good: the
raw sub-question text was passed as the search string, and fred/bls/treasury/
wikidata were not generically callable at all. This module is the seam that
fixes both halves:

1. `core_query(text)` — extract the searchable core of a sub-question: drop
   interrogative scaffolding ("what does recent research say about..."),
   stopwords, and filler; keep the topical noun phrase.

2. Per-source planners — each planner is a pure function from a sub-question
   to a `PlannedQuery`: the exact adapter method, its arguments, and an
   explanation. Nothing here touches the network, so every plan is testable
   against fixtures before a single byte is fetched. `build_plan(source,
   question)` dispatches on registry name; unknown sources get an honest
   `PlanResult(plannable=False)` rather than a guessed call.

3. Entity resolution — where a source demands identifiers ("unemployment"
   -> UNRATE, "Apple" -> CIK), resolve first. Resolution NEVER silently
   guesses: one clear winner above threshold resolves; otherwise the result
   carries ranked CANDIDATES with confidences for the caller (or model) to
   disambiguate. A wrong series id produces confident nonsense — the worst
   failure this system can have (mandate property 4).

Domain vocabulary lives in small declarative tables at module top so adding
an entity or a concept mapping is a data edit, not code surgery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ── core-query extraction ────────────────────────────────────────────────

# Interrogative / scaffolding openers and filler that make keyword searches
# worse. "what does recent research say about semiconductor supply chain
# resilience" should search as "semiconductor supply chain resilience".
_SCAFFOLD = re.compile(
    r"^(?:please\s+)?"
    r"(?:what|which|who|whom|whose|when|where|why|how|is|are|was|were|do|does"
    r"|did|can|could|should|would|will|has|have|had|list|find|show|tell me)\b"
    r"[^a-z0-9]*",
    re.IGNORECASE,
)
_FILLER = {
    # generic research-process words that carry no topical signal
    "recent", "latest", "current", "say", "says", "said", "tell", "about",
    "regarding", "concerning", "research", "studies", "study", "literature",
    "papers", "evidence", "question", "questions", "sub-question",
    "answer", "answers", "data", "information", "sources", "source",
    "please", "kindly", "good", "well", "right", "now", "today", "there",
    "does", "do", "did", "any", "some", "much", "many", "more", "most",
    # plain function words that survive the scaffolding regex mid-sentence
    "the", "and", "for", "with", "into", "from", "which", "that", "this",
    "what", "when", "where", "why", "how", "are", "was", "were", "has",
    "have", "had", "will", "would", "could", "should", "can",
    # more research-process vocabulary and scaffolding verbs
    "scholarly", "peer", "reviewed", "fetch", "give", "get", "find",
    # light verbs that carry relation, not topic
    "face", "faces", "facing", "affect", "affects", "affected",
    "address", "addresses", "impact", "impacts", "doing", "happening",
    "going", "become", "becomes", "come", "comes", "make", "makes",
}
_KEEP_MIN_LEN = 3


def core_query(text: str, *, extra_stop: set[str] | None = None) -> str:
    """The searchable core of a sub-question.

    Strips interrogative scaffolding and process filler, keeps the topical
    words in their original order. `extra_stop` lets a planner drop words
    that are its own domain vocabulary ('clinical trials' for
    ClinicalTrials.gov) rather than the topic. Returns '' when nothing
    survives (the planner will then report the question as unsearchable
    rather than send an empty query).
    """
    t = text.strip()
    prev = None
    while prev != t:
        prev = t
        t = _SCAFFOLD.sub("", t.strip())
    stop = _FILLER | (extra_stop or set())
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']*", t)
    kept = [w for w in words if w.lower() not in stop
            and len(w) >= _KEEP_MIN_LEN]
    return " ".join(kept)


# ── plan types ───────────────────────────────────────────────────────────

@dataclass
class PlannedQuery:
    """One concrete adapter call: fully-formed arguments, no guessing."""
    source: str
    method: str                 # adapter attribute to call
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    rationale: str = ""         # why these parameters

    def describe(self) -> str:
        a = ", ".join(repr(x) for x in self.args)
        k = ", ".join(f"{k}={v!r}" for k, v in self.kwargs.items())
        return f"{self.source}.{self.method}({', '.join(filter(None, [a, k]))})"


@dataclass
class Candidate:
    """A possible entity resolution with its confidence."""
    key: Any                    # the identifier (series id, CIK, Q-number...)
    label: str
    confidence: float           # 0..1
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label,
                "confidence": round(self.confidence, 3),
                "detail": self.detail}


@dataclass
class PlanResult:
    """What query authoring produced for one (source, sub-question) pair.

    plannable=False means this source honestly cannot serve the question
    (no capability match); candidates non-empty means resolution was
    AMBIGUOUS and the caller must pick — never treat those two states as
    interchangeable.
    """
    plannable: bool
    queries: list[PlannedQuery] = field(default_factory=list)
    resolved: dict[str, Any] = field(default_factory=dict)   # slot -> key
    candidates: dict[str, list[Candidate]] = field(
        default_factory=dict)                                # slot -> ranked
    reason: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"plannable": self.plannable,
                             "reason": self.reason,
                             "resolved": dict(self.resolved)}
        if self.queries:
            d["queries"] = [{"method": q.method, "args": list(q.args),
                             "kwargs": dict(q.kwargs),
                             "rationale": q.rationale}
                            for q in self.queries]
        if self.candidates:
            d["candidates"] = {slot: [c.to_dict() for c in cands]
                               for slot, cands in self.candidates.items()}
        return d


def execute(adapter: Any, plan: PlanResult) -> list[dict]:
    """Run a plan's queries against an already-instantiated adapter.

    Returns the parsed bodies in order. Raises AttributeError naming the
    missing method if a plan is stale relative to the adapter — plans are
    authored against the adapter surface declared here, and drift should be
    loud.
    """
    out = []
    for q in plan.queries:
        fn = getattr(adapter, q.method)
        out.append(fn(*q.args, **q.kwargs))
    return out


# ── entity-resolution tables ─────────────────────────────────────────────

#: macro concept -> FRED series ids (best-first). Curated, high-confidence
#: mappings only; anything else goes through FRED's own series_search.
_FRED_CONCEPTS: dict[str, list[Candidate]] = {
    "unemployment": [
        Candidate("UNRATE", "Civilian Unemployment Rate", 0.95),
        Candidate("CIVPART", "Labor Force Participation Rate", 0.6),
    ],
    "inflation": [
        Candidate("CPIAUCSL", "CPI All Urban Consumers", 0.9),
        Candidate("CPILFESL", "CPI Core (ex food & energy)", 0.8),
        Candidate("PCEPI", "PCE Price Index", 0.7),
    ],
    "cpi": [
        Candidate("CPIAUCSL", "CPI All Urban Consumers", 0.95),
    ],
    "gdp": [
        Candidate("GDPC1", "Real GDP", 0.95),
        Candidate("GDP", "Nominal GDP", 0.85),
    ],
    "interest rates": [
        Candidate("DFF", "Effective Federal Funds Rate", 0.9),
        Candidate("FEDFUNDS", "Monthly Federal Funds Rate", 0.8),
    ],
    "federal funds": [
        Candidate("DFF", "Effective Federal Funds Rate", 0.95),
    ],
}

#: exact series ids pass through untouched
_FRED_ID_RE = re.compile(r"^[A-Z0-9_]{4,}$")

#: BLS concepts -> series ids (seasonally adjusted national where sensible)
_BLS_CONCEPTS: dict[str, list[Candidate]] = {
    "unemployment rate": [
        Candidate("LNS14000000", "Unemployment rate (SA, 16+)", 0.95),
    ],
    "unemployment": [
        Candidate("LNS14000000", "Unemployment rate (SA, 16+)", 0.9),
    ],
    "payrolls": [
        Candidate("CES0000000001", "Total nonfarm employment (SA)", 0.95),
    ],
    "employment": [
        Candidate("CES0000000001", "Total nonfarm employment (SA)", 0.9),
    ],
    "cpi": [
        Candidate("CUSR0000SA0", "CPI-U all items (SA)", 0.9),
    ],
    "inflation": [
        Candidate("CUSR0000SA0", "CPI-U all items (SA)", 0.85),
    ],
}

_BLS_ID_RE = re.compile(r"^[A-Z]{2,3}[A-Z0-9]{6,}$")

#: Treasury Fiscal Data datasets by topic. The catalog has ~1000 datasets;
#: we map the ones whose names appear in ordinary questions and otherwise
#: declare the gap.
_TREASURY_DATASETS: dict[str, list[Candidate]] = {
    "yield curve": [
        Candidate("v2/accounting/od/avg_interest_rates",
                  "Average interest rates & yield curve", 0.75),
    ],
    "interest rates": [
        Candidate("v2/accounting/od/avg_interest_rates",
                  "Average interest rates", 0.7),
    ],
    "national debt": [
        Candidate("v2/debt/mspd/mspd_table_1", "Debt by instrument", 0.8),
    ],
    "debt": [
        Candidate("v2/debt/mspd/mspd_table_1", "Debt by instrument", 0.75),
    ],
}

#: Wikidata properties/classes used to assemble SPARQL from plain nouns.
_WIKIDATA_HINTS: dict[str, str] = {
    "company": "Q4830453", "companies": "Q4830453",
    "country": "Q3624078", "countries": "Q3624078",
    "person": "Q5", "people": "Q5",
    "drug": "Q12140", "medication": "Q12140",
}


# ── resolution semantics ────────────────────────────────────────────────

_RESOLVE_AUTO = 0.90     # single candidate >= this resolves automatically
_RESOLVE_GAP = 0.10      # ...and must lead runner #2 by this much


def _resolve(slot: str, question: str,
             table: dict[str, list[Candidate]]) -> tuple[
             dict[str, Any], dict[str, list[Candidate]]]:
    """Resolve one slot against a concept table.

    Returns ({slot: key}, {slot: candidates}). A key lands in `resolved`
    ONLY when the best-matching concept's top candidate clears the auto
    threshold AND beats the runner-up by the gap; otherwise the full
    candidate list comes back for explicit disambiguation. Exact-id tokens
    bypass the table entirely (they are their own answer).
    """
    # exact known id as its own token passes through ("Fetch M2SL data").
    # A bare word like WHAT must never resolve: require the token to appear
    # FULLY UPPERCASE in the original text AND either contain a digit or be
    # in the curated known-id set.
    upper_tokens = {m.group(0) for m in
                    re.finditer(r"\b[A-Z0-9][A-Z0-9_]+\b", question)}
    known_ids = {c.key for cands in table.values() for c in cands}
    for tok in re.findall(r"[A-Za-z0-9_]+", question):
        up = tok.upper()
        if up not in upper_tokens:
            continue
        if any(ch.isdigit() for ch in up) or up in known_ids:
            return {slot: up}, {}
    matched: list[tuple[int, list[Candidate]]] = []
    for concept, cands in table.items():
        if concept in question.lower():
            matched.append((len(concept), cands))   # longer = more specific
    if not matched:
        return {}, {}
    matched.sort(key=lambda p: -p[0])
    cands = matched[0][1]
    top, second = cands[0], (cands[1] if len(cands) > 1 else None)
    confident = (
        top.confidence >= _RESOLVE_AUTO
        and (second is None or top.confidence - second.confidence >= _RESOLVE_GAP)
    )
    if confident:
        return {slot: top.key}, {}
    return {}, {slot: list(cands)}


# ── per-source planners ──────────────────────────────────────────────────

def _plan_openalex(question: str) -> PlanResult:
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core in question")
    return PlanResult(True, queries=[PlannedQuery(
        source="openalex", method="works_search",
        kwargs={"query": core, "limit": 10},
        rationale="keyword search over titles/abstracts/concepts using "
                  "the extracted core")],
        reason=f"searched as '{core}'")


def _plan_semantic_scholar(question: str) -> PlanResult:
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core")
    return PlanResult(True, queries=[PlannedQuery(
        source="semantic_scholar", method="paper_search",
        kwargs={"query": core, "limit": 10},
        rationale="paper keyword search using the extracted core")],
        reason=f"searched as '{core}'")


def _plan_clinicaltrials(question: str) -> PlanResult:
    core = core_query(
        question,
        extra_stop={"clinical", "trials", "trial", "patients", "patient",
                    "drug", "drugs"})
    if not core:
        return PlanResult(False, reason="no topical core beyond trial "
                          "vocabulary")
    kw = {"query_term": core, "limit": 20}
    low = question.lower()
    # structured filters where the question offers them
    for status_word, status in (("recruiting", "RECRUITING"),
                                ("completed", "COMPLETED"),
                                ("terminated", "TERMINATED")):
        if status_word in low:
            kw["status"] = status
            break
    return PlanResult(True, queries=[PlannedQuery(
        source="clinicaltrials", method="search_studies", kwargs=kw,
        rationale="ClinicalTrials.gov v2 query.term over the extracted core"
                  + (f"; overallStatus={kw['status']}" if kw.get("status")
                     else ""))], reason=f"searched as '{core}'")


def _plan_federalregister(question: str) -> PlanResult:
    core = core_query(
        question,
        extra_stop={"proposed", "rules", "rule", "final", "notice",
                    "notices", "executive", "orders", "order", "address",
                    "regulation", "regulations", "federal"})
    if not core:
        return PlanResult(False, reason="no topical core beyond document-type "
                          "vocabulary")
    extra: dict = {}
    low = question.lower()
    # docket/document-type vocabulary native to FR
    doc_type = None
    for word, dt in (("executive order", "PRESDOCU"),
                     ("presidential", "PRESDOCU"),
                     ("proposed rule", "PRORULE"),
                     ("final rule", "RULE"), ("rule", "RULE")):
        if word in low:
            doc_type = dt
            break
    if doc_type:
        extra["conditions[type][]"] = [doc_type]
    return PlanResult(True, queries=[PlannedQuery(
        source="federalregister", method="search",
        kwargs={"query_term": core, "limit": 20, "extra_params": extra},
        rationale="FR full-text conditions search over the extracted core"
                  + (f"; type={doc_type}" if doc_type else ""))],
        reason=f"searched as '{core}'"
               + (f" filtered to {doc_type}" if doc_type else ""))


def _plan_gdelt(question: str) -> PlanResult:
    core = core_query(
        question,
        extra_stop={"news", "coverage", "reported", "reporting", "media",
                    "press", "outlets", "outlet"})
    if not core:
        return PlanResult(False, reason="no topical core beyond coverage "
                          "vocabulary")
    # GDELT treats multi-word phrases loosely; quote the core as a phrase
    # so coverage volume reflects the topic, not any single word.
    q = f'"{core}"'
    mode = "artlist"
    low = question.lower()
    if any(w in low for w in ("coverage", "attention", "volume", "trend",
                              "over time")):
        mode = "timelinevol"
    return PlanResult(True, queries=[PlannedQuery(
        source="gdelt", method="doc_query",
        kwargs={"query": q, "mode": mode,
                "timespan": "1y" if mode == "timelinevol" else "",
                "limit": 75},
        rationale=f"phrase-quoted core in {mode} mode")],
        reason=f"phrase query {q!r}, mode={mode}")


def _plan_fred(question: str) -> PlanResult:
    resolved, cands = _resolve("series_id", question, _FRED_CONCEPTS)
    core = core_query(question)
    if "series_id" in resolved:
        sid = resolved["series_id"]
        return PlanResult(True, queries=[PlannedQuery(
            source="fred", method="series_observations",
            kwargs={"series_id": sid, "limit": 120},
            rationale=f"concept resolved to series {sid}")],
            resolved=resolved,
            reason=f"'{core or question}' -> {sid}")
    if "series_id" in cands:
        return PlanResult(False, reason="ambiguous macro concept; "
                          "disambiguate before fetching",
                          candidates=cands)
    if not core:
        return PlanResult(False, reason="no searchable core and no known "
                          "series id in question")
    # fall back to FRED's own full-text series search — it returns real
    # candidate series ids the pipeline can rank, instead of us guessing.
    return PlanResult(True, queries=[PlannedQuery(
        source="fred", method="series_search",
        kwargs={"query": core, "limit": 10},
        rationale="full-text series search; results carry series ids for "
                  "a follow-up observations fetch")],
        reason=f"no curated concept matched; searched FRED series as '{core}'")


def _plan_bls(question: str) -> PlanResult:
    resolved, cands = _resolve("series_id", question, _BLS_CONCEPTS)
    if "series_id" in resolved:
        sid = resolved["series_id"]
        import datetime
        end = datetime.date.today().year
        return PlanResult(True, queries=[PlannedQuery(
            source="bls", method="timeseries",
            kwargs={"series_ids": [sid], "start_year": end - 2,
                    "end_year": end},
            rationale=f"concept resolved to BLS series {sid}; no-key tier "
                      "caps history at 3 years")],
            resolved=resolved, reason=f"resolved to {sid}")
    if "series_id" in cands:
        return PlanResult(False, reason="ambiguous BLS concept; "
                          "disambiguate before fetching", candidates=cands)
    return PlanResult(False, reason=(
        "BLS API v2 has no free-text search endpoint; a published series id "
        "is required. Add the concept to _BLS_CONCEPTS or supply an id."))


_TREASURY_DATASET_RE = re.compile(r"^v\d/[a-z0-9/_-]+$")


def _plan_treasury(question: str) -> PlanResult:
    low = question.lower()
    # an explicit dataset path passes straight through
    for token in re.findall(r"v\d/[a-z0-9/_-]+", low):
        return PlanResult(True, queries=[PlannedQuery(
            source="treasury", method="query", kwargs={"dataset": token},
            rationale="explicit dataset path supplied in question")],
            resolved={"dataset": token}, reason=f"dataset {token}")
    resolved, cands = _resolve("dataset", question, _TREASURY_DATASETS)
    if "dataset" in resolved:
        ds = resolved["dataset"]
        kw: dict = {"dataset": ds, "limit": 100}
        m = re.search(r"(20\d{2})-(\d{2})-\d{2}", question)
        if m:
            kw["filters"] = f"record_date:gte:{m.group(0)}"
        return PlanResult(True, queries=[PlannedQuery(
            source="treasury", method="query", kwargs=kw,
            rationale=f"topic resolved to catalog dataset {ds}")],
            resolved=resolved, reason=f"resolved to dataset {ds}")
    if "dataset" in cands:
        return PlanResult(False, reason="ambiguous treasury dataset; "
                          "the catalog has ~1000 entries — disambiguate",
                          candidates=cands)
    core = core_query(question)
    return PlanResult(False, reason=(
        f"Fiscal Data needs a dataset name from its ~1000-dataset catalog; "
        f"no curated mapping matched '{core}'. Browse "
        f"fiscaldata.treasury.gov/datasets and supply a dataset path."))


def _plan_wikidata(question: str) -> PlanResult:
    """Wikidata gets SPARQL assembled from the core plus known classes.

    Generic template: find entities labelled like the core terms, with
    labels and descriptions. This answers 'what is X'/'which entities are
    Y' shape questions without inventing ontology knowledge we don't have;
    deeper graph questions need the model to write real SPARQL, and the
    plan says so honestly.
    """
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core")
    terms = [w for w in core.split() if w.lower() not in _WIKIDATA_HINTS]
    subject = terms[0] if terms else core.split()[0]
    sparql = (
        "SELECT ?item ?itemLabel ?itemDescription WHERE {\n"
        f'  SERVICE wikibase:mwapi {{ bd:serviceParam wikibase:endpoint '
        f'"www.wikidata.org"; wikibase:api "EntitySearch"; '
        f'mwapi:search "{subject}"; mwapi:language "en". '
        f"?item wikibase:apiOutputItem mwapi:item. }}\n"
        "  SERVICE wikibase:label { bd:serviceParam wikibase:language "
        '"en". }\n'
        "} LIMIT 20"
    )
    return PlanResult(True, queries=[PlannedQuery(
        source="wikidata", method="sparql", args=(sparql,),
        rationale="EntitySearch-backed SPARQL over the leading core term; "
                  "graph traversal beyond lookup needs model-authored "
                  "SPARQL")],
        reason=f"entity lookup for '{subject}'")


# keyword-capable adapters: same shape, source-specific knobs
_KEYWORD_PLANNERS = {
    "openalex": _plan_openalex,
    "semantic_scholar": _plan_semantic_scholar,
    "clinicaltrials": _plan_clinicaltrials,
    "federalregister": _plan_federalregister,
    "gdelt": _plan_gdelt,
    "fred": _plan_fred,
    "bls": _plan_bls,
    "treasury": _plan_treasury,
    "wikidata": _plan_wikidata,
}

#: SEC deliberately unplannable here — the machine is rate-limited/403'd and
#: the mandate forbids hitting it; planning would invite fetching.
_HONEST_GAPS = {
    "sec_fts": "SEC full-text search requires a declared contact and this "
               "host is currently 403'd; query authoring deferred until "
               "access is restored (deliberate, not forgotten).",
    "courtlistener": "requires CALLISTO_COURTLISTENER_API_KEY; planner is "
                     "trivial (keyword) but left to the access owner.",
    "uspto_odp": "requires CALLISTO_USPTO_API_KEY; keyword planner deferred.",
    "bea": "needs dataset+tablename pairs from BEA's parameter catalogue; "
           "no free-text search exists in the API.",
    "census": "Census API queries are year+dataset+get-vars tuples with no "
              "text search; authoring requires a variable catalogue.",
    "eia": "requires CALLISTO_EIA_API_KEY; series routing needs EIA's facet "
           "browser, not keywords.",
    "fdic": "FDIC BankFind filters are field=value predicates; keyword "
            "mapping deferred to the FDIC owner.",
    "cftc": "CFTC COT needs market-code/dataset selection; no text search.",
    "worldbank": "indicator codes need the WB indicator catalogue; keyword "
                 "mapping deferred.",
    "wayback": "Wayback queries take a URL, not a topic; nothing to author "
               "without knowing which page is in question.",
}


def build_plan(source_name: str, question: str) -> PlanResult:
    """Author the query(ies) for one source given a sub-question. Pure."""
    planner = _KEYWORD_PLANNERS.get(source_name)
    if planner is not None:
        return planner(question)
    if source_name in _HONEST_GAPS:
        return PlanResult(False, reason=_HONEST_GAPS[source_name])
    return PlanResult(False, reason=f"unknown source {source_name!r}")


def plannable_sources() -> list[str]:
    return sorted(_KEYWORD_PLANNERS)


def honest_gaps() -> dict[str, str]:
    return dict(_HONEST_GAPS)
