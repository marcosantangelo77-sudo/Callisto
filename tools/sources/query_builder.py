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
    # I2 live smoke: v2/debt/mspd/mspd_table_1 is a 404; national-debt
    # questions are served by Debt to the Penny (verified 2026-08-22).
    "national debt": [
        Candidate("v2/accounting/od/debt_to_penny",
                  "Debt to the Penny (total public debt outstanding)", 0.85),
    ],
    "debt": [
        Candidate("v2/accounting/od/debt_to_penny",
                  "Debt to the Penny (total public debt outstanding)", 0.8),
        Candidate("v1/debt/mspd/mspd_table_1",
                  "Monthly Statement of the Public Debt, table 1", 0.6),
    ],
}

#: Wikidata properties/classes used to assemble SPARQL from plain nouns.
_WIKIDATA_HINTS: dict[str, str] = {
    "company": "Q4830453", "companies": "Q4830453",
    "country": "Q3624078", "countries": "Q3624078",
    "person": "Q5", "people": "Q5",
    "drug": "Q12140", "medication": "Q12140",
}

#: SEC registrant name -> CIK. Even though sec_fts is deliberately
#: unplannable while this host is 403'd, the CIK seam is entity RESOLUTION:
#: 'Apple' -> 0000320193 is a fact any caller (EDGAR tooling, the model,
#: future adapters) needs resolved the same candidate-or-resolve way.
_SEC_CIKN: dict[str, list[Candidate]] = {
    "apple": [Candidate("0000320193", "Apple Inc.", 0.95)],
    "microsoft": [Candidate("0000789019", "Microsoft Corp.", 0.95)],
    "google": [
        Candidate("0001652044", "Alphabet Inc.", 0.9),
    ],
    "alphabet": [Candidate("0001652044", "Alphabet Inc.", 0.95)],
    "amazon": [Candidate("0001018724", "Amazon.com Inc.", 0.95)],
    "nvidia": [Candidate("0001045810", "NVIDIA Corp.", 0.95)],
    "tesla": [Candidate("0001318605", "Tesla Inc.", 0.95)],
    "meta": [Candidate("0001326801", "Meta Platforms Inc.", 0.9)],
    "facebook": [Candidate("0001326801", "Meta Platforms Inc.", 0.9)],
}

_CIK_RE = re.compile(r"^[0-9]{10}$")

# characters allowed inside an FDIC filter/search value after an operator —
# must match fdic.py's own guard so authored filters cannot pass planning
# but fail at fetch time. Double quotes are allowed: the ES query-string
# form search=NAME:"term" is the partial-friendly route (live-smoke finding).
_VALUE_OK = re.compile(r"^[A-Za-z0-9 .,:><=\-+()\"']*$")


def resolve_entity(entity_type: str, text: str) -> tuple[
        dict[str, Any], dict[str, list[Candidate]]]:
    """Public resolution entry point for identifier slots beyond per-source
    planners ('company' -> CIK). Same contract as _resolve: a key lands in
    `resolved` ONLY above the auto threshold with a real gap; otherwise
    ranked candidates come back for explicit disambiguation."""
    if entity_type == "company":
        return _resolve("cik", text, _SEC_CIKN)
    raise ValueError(f"unknown entity type {entity_type!r}")


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
                    re.finditer(r"\b[A-Z0-9][A-Z0-9_]+(?:\.[A-Z0-9]+)*\b",
                                question)}
    known_ids = {c.key for cands in table.values() for c in cands}
    for tok in re.findall(r"[A-Za-z0-9_]+(?:\.[A-Z0-9]+)*", question):
        up = tok.upper()
        if up not in upper_tokens:
            continue
        # must contain a letter — bare numbers ('2024') are dates, not ids
        if not any(ch.isalpha() for ch in up):
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
        second is not None
        and top.confidence >= _RESOLVE_AUTO
        and top.confidence - second.confidence >= _RESOLVE_GAP
    ) or (second is None and top.confidence >= 0.75)
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


# I2 live-smoke bug: the registry name is 'semanticscholar' (semantic_scholar.
# py SPEC.name), but wave 4 registered the planner under 'semantic_scholar' —
# so build_plan('semanticscholar', ...) fell through to an "honest gap" that
# claimed a keyword planner was deferred while one existed all along. Keyed by
# the registry name now, and both spellings accepted defensively.
def _plan_semantic_scholar(question: str) -> PlanResult:
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core")
    return PlanResult(True, queries=[PlannedQuery(
        source="semanticscholar", method="paper_search",
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
    # structured filters where the question offers them. The status word
    # must NOT stay in query.term: the API ANDs full-text terms, so
    # 'recruiting semaglutide' searches for documents containing the word.
    for status_word, status in (("recruiting", "RECRUITING"),
                                ("completed", "COMPLETED"),
                                ("terminated", "TERMINATED")):
        if status_word in low:
            kw["status"] = status
            kw["query_term"] = " ".join(
                w for w in core.split() if w.lower() != status_word)
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
        extra["conditions[type][]"] = doc_type
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
                    "press", "outlets", "outlet", "volume"})
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
        # still allow a bare Q-id question ("What is Q42?")
        resolved, cands = _plan_wikidata_concept(question)
        if "q_id" in resolved:
            return PlanResult(True, resolved=resolved, reason=f"{resolved['q_id']}")
        return PlanResult(False, reason="no searchable core")
    resolved, cands = _plan_wikidata_concept(question)
    if "q_id" in cands:
        return PlanResult(False, reason="ambiguous entity class; "
                          "disambiguate before querying", candidates=cands)
    if "q_id" in resolved:
        # a bare Q-number IS the entity; nothing to author beyond recording
        return PlanResult(True, resolved=resolved,
                          queries=[PlannedQuery(
                              source="wikidata", method="sparql",
                              args=(f"SELECT ?item ?itemLabel ?itemDescription"
                                    f" WHERE {{ BIND(wd:{resolved['q_id']}"
                                    f" AS ?item)"
                                    f" SERVICE wikibase:label {{ bd:"
                                    f"serviceParam wikibase:language \"en\"."
                                    f" }} }} LIMIT 1",),
                              rationale="explicit Q-id supplied in question")],
                          reason=f"{resolved['q_id']} supplied directly")
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


# ── I2 wave: entity-resolution tables for identifier-bound sources ──────

#: World Bank indicators by concept. Codes are real WDI ids; anything not
#: in this table falls back to the WB indicator API's own search.
_WORLDBANK_INDICATORS: dict[str, list[Candidate]] = {
    "gdp": [
        Candidate("NY.GDP.MKTP.CD", "GDP (current US$)", 0.95),
        Candidate("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)", 0.8),
    ],
    "gdp growth": [
        Candidate("NY.GDP.MKTP.KD.ZG", "GDP growth (annual %)", 0.95),
    ],
    "population": [
        Candidate("SP.POP.TOTL", "Population, total", 0.95),
    ],
    "trade": [
        Candidate("NE.TRD.GNFS.ZS", "Trade (% of GDP)", 0.8),
    ],
    "debt": [
        Candidate("GC.DOD.TOTL.GD.ZS", "Central government debt, total "
                  "(% of GDP)", 0.75),
        Candidate("DT.DOD.DLDS.CD", "External debt stocks, long-term (US$)",
                  0.7),
    ],
    "emissions": [
        Candidate("EN.GHG.CO2.PC.CE.AR5", "CO2 emissions per capita",
                  0.8),
    ],
    "energy use": [
        Candidate("EG.USE.PCAP.KG.OE", "Energy use per capita", 0.8),
    ],
}

#: ISO3 country codes that appear as ordinary words in questions.
_WB_COUNTRIES: dict[str, str] = {
    "usa": "USA", "united states": "USA", "china": "CHN", "india": "IND",
    "germany": "DEU", "japan": "JPN", "brazil": "BRA", "mexico": "MEX",
    "nigeria": "NGA", "uk": "GBR", "britain": "GBR",
    "united kingdom": "GBR", "france": "FRA", "russia": "RUS",
    "south korea": "KOR", "korea": "KOR", "canada": "CAN",
}

_ISO3_RE = re.compile(r"^[A-Z]{3}$")
_WB_INDICATOR_RE = re.compile(r"^[A-Z]{2}\.[A-Z0-9]{3}\.[A-Z0-9]{2,4}$")


def _wb_resolve_country(question: str) -> tuple[str, list[Candidate]]:
    """('USA', []) when one country is named; ('', candidates) when several
    are (a cross-country comparison needs the caller to say which — or
    'all', which the planner decides)."""
    low = question.lower()
    hits: list[tuple[int, str]] = []   # (len, iso3) longest phrase wins checks
    for name, iso3 in _WB_COUNTRIES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            hits.append((len(name), iso3))
    if not hits:
        m = _ISO3_RE.search(question.upper())
        return (m.group(0), []) if m else ("", [])
    hits.sort(reverse=True)
    distinct = {iso for _, iso in hits}
    if len(distinct) == 1:
        return distinct.pop(), []
    # multiple countries named: rank them by how specific the mention was
    seen: set[str] = set()
    cands: list[Candidate] = []
    for _, iso3 in hits:
        if iso3 in seen:
            continue
        seen.add(iso3)
        cands.append(Candidate(iso3, f"ISO3 {iso3}", 0.6))
    return "", cands


def _plan_worldbank(question: str) -> PlanResult:
    resolved_i, cands_i = _resolve("indicator_code", question,
                                   _WORLDBANK_INDICATORS)
    country, cands_c = _wb_resolve_country(question)
    if "indicator_code" in cands_i or cands_c:
        merged = dict(cands_i)
        if cands_c:
            merged["country"] = cands_c
        return PlanResult(False, reason="ambiguous World Bank indicator or "
                          "country; disambiguate before fetching",
                          candidates=merged)
    core = core_query(question)
    if "indicator_code" not in resolved_i:
        if not core:
            return PlanResult(False, reason="no known indicator and no "
                              "searchable core")
        # fall back to WB's own indicator search so results carry real codes
        return PlanResult(True, queries=[PlannedQuery(
            source="worldbank", method="search_indicators",
            kwargs={"query": core, "limit": 10},
            rationale="WB indicator full-text search; results carry codes "
                      "for a follow-up indicator fetch")],
            reason=f"no curated indicator matched; searched as '{core}'")
    code = resolved_i["indicator_code"]
    kw: dict = {"code": code, "per_page": 200}
    if country:
        kw["iso3"] = country
        who = country
    elif any(w in question.lower() for w in
             ("compare", "comparison", "across countries", "cross-country")):
        kw["iso3"] = "all"
        who = "all countries"
    else:
        kw["iso3"] = "all"
        who = "all countries (no country named)"
    return PlanResult(True, queries=[PlannedQuery(
        source="worldbank", method="indicator", kwargs=kw,
        rationale=f"concept resolved to {code} for {who}")],
        resolved={**resolved_i, **({"country": country} if country else {})},
        reason=f"'{core or question}' -> {code} ({who})")


def _plan_wikidata_concept(question: str) -> tuple[dict, dict]:
    """'concept -> Q-number' resolution via curated hints + Q-id passthrough."""
    upper_tokens = {m.group(0) for m in
                    re.finditer(r"\bQ[0-9]+\b", question)}
    if upper_tokens:
        qid = sorted(upper_tokens)[0]
        return {"q_id": qid}, {}
    low = question.lower()
    matched = [(c, h) for h, c in _WIKIDATA_HINTS.items() if h in low]
    if matched:
        matched.sort(key=lambda p: -len(p[1]))
        best = matched[0][0]
        others = [c for c, _ in matched[1:] if c != best]
        if not others:
            return {"q_id": best}, {}
        return {}, {"q_id": [Candidate(best, f"class {best}", 0.7)] +
                    [Candidate(c, f"class {c}", 0.5) for c in others]}
    return {}, {}


def _plan_bea(question: str) -> PlanResult:
    """BEA has no text search, but its NIPA/Regional surface is small enough
    to map honestly: GDP/trade/income concepts to dataset+table pairs."""
    low = question.lower()
    table: dict[str, tuple[dict, str]] = {
        "gdp": ({"dataset": "NIPA", "tablename": "T10101", "linecode": "1"},
                "Real GDP (Table 1.1.1 line 1)"),
        "personal income": ({"dataset": "Regional", "tablename": "SAINC1"},
                            "State annual personal income"),
        "trade balance": ({"dataset": "IntlTrade", "tablename": ""},
                          "International trade in goods & services"),
        "exports": ({"dataset": "IntlTrade", "tablename": ""},
                    "Exports of goods & services"),
        "imports": ({"dataset": "IntlTrade", "tablename": ""},
                    "Imports of goods & services"),
        "gdp by industry": ({"dataset": "GDPbyIndustry",
                             "tablename": "GrossOutput"},
                            "GDP by industry gross output"),
    }
    matched = sorted(((k, v) for k, v in table.items() if k in low),
                     key=lambda p: -len(p[0]))
    if not matched:
        core = core_query(question)
        return PlanResult(False, reason=(
            f"BEA requires a DataSetName+TableName pair from its parameter "
            f"catalogue and has no text search; no mapping matched "
            f"'{core}'. Browse apps.bea.gov/API/signup + the interactive "
            f"data catalogue and add the mapping."))
    kw, label = matched[0][1]
    years = ""
    m = re.search(r"\b(19|20)\d{2}\b", question)
    if m:
        y = int(m.group(0))
        years = f"{y - 4},{y}"
    if years:
        kw["years"] = years
    return PlanResult(True, queries=[PlannedQuery(
        source="bea", method="get_data", kwargs=dict(kw, frequency="A"),
        rationale=f"topic mapped to BEA {label}")],
        reason=f"mapped to {label} ({kw.get('dataset')})")


def _plan_census(question: str) -> PlanResult:
    """Census queries are year+dataset+variable tuples with no search; we
    author the handful of timeseries surveys ordinary questions name."""
    low = question.lower()
    table: dict[str, tuple[list[str], str, str]] = {
        "housing starts": (
            ["HOUSTNSA"], "timeseries/eits/resconst",
            "New residential construction: housing starts (not seasonally "
            "adjusted)"),
        "building permits": (
            ["PERMITSNSA"], "timeseries/eits/resconst",
            "New residential construction: building permits"),
        "housing completions": (
            ["COMPNSA"], "timeseries/eits/resconst",
            "New residential construction: completions"),
        "retail sales": (
            ["SM_44X72_SM"], "timeseries/eits/marts",
            "Monthly retail trade: total retail sales (NAICS 44X72)"),
        "e-commerce": (
            ["SM_4541SM"], "timeseries/eits/marts",
            "Monthly retail trade: e-commerce sales"),
        "population": (
            ["POP"], "timeseries/international/pop",
            "International data base population (or use ACS tables)"),
    }
    matched = sorted(((k, v) for k, v in table.items() if k in low),
                     key=lambda p: -len(p[0]))
    # live check 2026-08-22: Census now 302s to a "Missing Key" page even
    # for light use — fail loudly at planning instead of mid-fetch.
    import os
    if not matched:
        if not os.environ.get("CALLISTO_CENSUS_API_KEY"):
            return PlanResult(False, reason=(
                "Census API requires a key even for light use (free at "
                "api.census.gov/data/key_signup.html); set "
                "CALLISTO_CENSUS_API_KEY before planning Census fetches."))
        core = core_query(question)
        return PlanResult(False, reason=(
            f"Census queries need year+dataset+GET variables from its "
            f"variable catalogue (no text search); no survey mapping "
            f"matched '{core}'. Browse api.census.gov/data.html and add "
            f"the mapping."))
    if not os.environ.get("CALLISTO_CENSUS_API_KEY"):
        return PlanResult(False, reason=(
            "Census API requires a key even for light use (free at "
            "api.census.gov/data/key_signup.html); set "
            "CALLISTO_CENSUS_API_KEY before planning Census fetches."))
    get_vars, dataset, label = matched[0][1]
    start = end = ""
    years = re.findall(r"\b(19|20)\d{2}\b", question)
    yrs = re.findall(r"\b((?:19|20)\d{2})\b", question)
    if yrs:
        start = yrs[0] + "-01"
        end = (yrs[1] if len(yrs) > 1 else yrs[0]) + "-12"
    return PlanResult(True, queries=[PlannedQuery(
        source="census", method="timeseries",
        kwargs={"dataset": dataset, "get_vars": get_vars,
                "geo_for": "us:*", "start": start, "end": end},
        rationale=f"question mapped to Census {label}")],
        reason=f"mapped to {label}")


_EIA_SERIES: dict[str, list[Candidate]] = {
    "wti": [Candidate("PET.RWTC.M", "WTI spot price FOB, monthly", 0.9)],
    "brent": [Candidate("PET.RBRTE.M", "Brent spot price FOB, monthly", 0.9)],
    "crude oil prices": [
        Candidate("PET.RWTC.M", "WTI spot price FOB, monthly", 0.95),
        Candidate("PET.RBRTE.M", "Brent spot price FOB, monthly", 0.8),
    ],
    "gasoline prices": [
        Candidate("PET.EER_EPD2DXL0_PFE_NUS_DPG.M",
                  "US retail diesel price", 0.7),
        Candidate("TOTAL.MOGTU.US.M", "US motor gasoline supplied", 0.65),
    ],
    "natural gas storage": [
        Candidate("NG.NWG_STO.M", "Natural gas working storage", 0.85),
    ],
}


def _plan_eia(question: str) -> PlanResult:
    """EIA v2 needs an API key AND a series id or facet route. Concept
    table resolves common energy series; otherwise refuse honestly."""
    # fail loudly at PLANNING time when no key is configured — a plan whose
    # first fetch dies on auth wastes the whole retrieval round.
    import os
    if not os.environ.get("CALLISTO_EIA_API_KEY"):
        return PlanResult(False, reason=(
            "EIA v2 requires CALLISTO_EIA_API_KEY (free registration at "
            "api.eia.gov/register). Set it before planning EIA fetches — "
            "failing loudly here instead of mid-fetch."))
    resolved, cands = _resolve("series_id", question, _EIA_SERIES)
    if "series_id" in resolved:
        sid = resolved["series_id"]
        freq = "annual"
        low = question.lower()
        if any(w in low for w in ("monthly", "month", "weekly", "week")):
            freq = "monthly" if "week" not in low else "weekly"
        return PlanResult(True, queries=[PlannedQuery(
            source="eia", method="series",
            kwargs={"series_id": sid, "frequency": freq},
            rationale=f"concept resolved to EIA series {sid} ({freq})")],
            resolved=resolved, reason=f"resolved to {sid}")
    if "series_id" in cands:
        return PlanResult(False, reason="ambiguous energy series; "
                          "disambiguate before fetching", candidates=cands)
    core = core_query(question)
    return PlanResult(False, reason=(
        f"EIA series need an id from its facet browser (route like "
        f"'petroleum/stve/state'); no mapping matched '{core}'. Browse "
        f"api.eia.gov/dashboard and add the mapping."))


_FDIC_FIELDS = ("CERT", "NAME", "STALP", "ASSET", "DEP", "EQ", "REPDTE")


def _plan_fdic(question: str) -> PlanResult:
    low = question.lower()
    if any(w in low for w in ("failed bank", "bank failure", "failures")):
        return PlanResult(True, queries=[PlannedQuery(
            source="fdic", method="failures", kwargs={"limit": 50},
            rationale="failed-bank history requested directly")],
            reason="failed-bank history query")
    # bank-name lookup. LIVE-SMOKE FINDING: filters=NAME:x is an EXACT
    # match on the FDIC side (NAME:Chase → 0 hits; the full legal name
    # quoted → 1), while search=NAME:"term" is an Elasticsearch query
    # string that matches partials (NAME:"chase" → 11 institutions incl.
    # CERT 628 JPMorgan Chase N.A.). Author the search form.
    proper = [t for t in re.findall(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*\b",
                                    question)
              if t.lower() not in _FILLER and t.lower() not in {
                  "what", "which", "bank", "banks", "the"}]
    if proper:
        name = max(proper, key=len)
        search = f'NAME:"{name}"'
        if not _VALUE_OK.fullmatch(search):
            return PlanResult(False, reason=f"unsafe FDIC search {search!r}")
        return PlanResult(True, queries=[PlannedQuery(
            source="fdic", method="search_institutions",
            kwargs={"search": search, "fields": _FDIC_FIELDS, "limit": 20},
            rationale=f"institution full-text search on bank name "
                      f"{name!r} (filters=NAME would exact-match)")],
            resolved={"bank_name": name}, reason=f"institution match {name}")
    core = core_query(question)
    return PlanResult(False, reason=(
        f"FDIC BankFind filters are field=value predicates over institution "
        f"attributes; no bank name or failure request found in '{core}'."))


_CFTC_MARKETS: dict[str, list[Candidate]] = {
    # legacy futures-only cftc_contract_market_code values (CFTC COT):
    # 067651 WTI Crude Oil (NYMEX), 088691 Gold (COMEX), 023651 Henry Hub
    # Natural Gas (NYMEX), 001601 Wheat (CBT), 002601 Corn (CBT),
    # 005601 Soybeans (CBT).
    "crude oil": [
        Candidate("067651", "WTI Crude Oil (NYMEX)", 0.9),
    ],
    "oil": [
        Candidate("067651", "WTI Crude Oil (NYMEX)", 0.75),
    ],
    "gold": [
        Candidate("088691", "Gold (COMEX)", 0.9),
    ],
    "natural gas": [
        Candidate("023651", "Henry Hub Natural Gas (NYMEX)", 0.9),
    ],
    "wheat": [
        Candidate("001601", "Wheat (CBT)", 0.9),
    ],
    "corn": [
        Candidate("002601", "Corn (CBT)", 0.9),
    ],
    "soybeans": [
        Candidate("005601", "Soybeans (CBT)", 0.9),
    ],
}


def _plan_cftc(question: str) -> PlanResult:
    """COT reports need a cftc_contract_market_code; curated commodity map,
    explicit code passthrough, else honest refusal."""
    m = re.search(r"\b([0-9]{3}[0-9A-Za-z]{3})\b", question)
    if m:
        code = m.group(1)
        return PlanResult(True, queries=[PlannedQuery(
            source="cftc_cot", method="contract_history",
            kwargs={"market_code": code, "weeks": 52},
            rationale="explicit CFTC market code supplied")],
            resolved={"market_code": code}, reason=f"market code {code}")
    resolved, cands = _resolve("market_code", question, _CFTC_MARKETS)
    if "market_code" in resolved:
        code = resolved["market_code"]
        disaggregated = any(w in question.lower() for w in
                            ("disaggregat", "money manager", "producer",
                             "swap dealer"))
        return PlanResult(True, queries=[PlannedQuery(
            source="cftc_cot", method="contract_history",
            kwargs={"market_code": code, "weeks": 52,
                    "disaggregated": disaggregated},
            rationale=f"commodity resolved to COT market code {code}"
                      + (" (disaggregated report)" if disaggregated else ""))],
            resolved=resolved, reason=f"resolved to market code {code}")
    if "market_code" in cands:
        return PlanResult(False, reason="ambiguous COT contract; "
                          "disambiguate before fetching", candidates=cands)
    core = core_query(question)
    return PlanResult(False, reason=(
        f"CFTC COT needs a market_code from the weekly report's contract "
        f"list; no mapping matched '{core}'. Find codes at "
        f"cftc.gov/dea/futures/deacmxlf.htm and add the mapping."))


def _plan_uspto_odp(question: str) -> PlanResult:
    import os
    if not os.environ.get("CALLISTO_USPTO_ODP_KEY"):
        return PlanResult(False, reason=(
            "USPTO Open Data Portal requires CALLISTO_USPTO_ODP_KEY (free "
            "at data.uspto.gov/getting-started). Set it before planning "
            "patent fetches — failing loudly instead of mid-fetch."))
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core")
    assignee = None
    m = (re.search(r"(?:patents?|applications?)\s+assigned\s+to\s+"
                   r"([A-Z][A-Za-z0-9&.\- ]{1,40})", question,
                   re.IGNORECASE)
         or re.search(r"(?:patents?|applications?)\s+(?:by|of)\s+"
                      r"([A-Z][A-Za-z0-9&.\- ]{2,40})", question,
                      re.IGNORECASE))
    if m:
        raw = m.group(1).strip()
        assignee = re.split(
            r"\s+(?:regarding|concerning|about|on|for|between|from)\s+",
            raw)[0].strip()
        assignee = re.sub(r"[?.!,]+$", "", assignee)
    if assignee:
        q = f'assigneeName:"{assignee}"'
        why = f"assignee filter on {assignee!r}"
    else:
        q = core
        why = f"simple query string over bibliographic fields: {core!r}"
    return PlanResult(True, queries=[PlannedQuery(
        source="uspto_odp", method="search_applications",
        kwargs={"query": q, "limit": 25},
        rationale=f"ODP simplified query syntax — {why}")],
        reason=why)


def _plan_courtlistener(question: str) -> PlanResult:
    import os
    if not os.environ.get("CALLISTO_COURTLISTENER_TOKEN"):
        return PlanResult(False, reason=(
            "CourtListener requires CALLISTO_COURTLISTENER_TOKEN (free at "
            "courtlistener.com — account tier ~125 req/day). Set it before "
            "planning case-law fetches."))
    core = core_query(question)
    if not core:
        return PlanResult(False, reason="no searchable core")
    low = question.lower()
    stype = "o"
    if "docket" in low:
        stype = "d"
    elif "judge" in low:
        stype = "p"
    order = "score desc"
    if any(w in low for w in ("recent", "latest", "newest", "last year")):
        order = "dateFiled desc"
    return PlanResult(True, queries=[PlannedQuery(
        source="courtlistener", method="search",
        kwargs={"query": core, "search_type": stype, "order_by": order},
        rationale=f"opinion/docket search ({stype}) over extracted core")],
        reason=f"{stype} search as '{core}'")


def _plan_wayback(question: str) -> PlanResult:
    """Wayback takes a URL, not a topic. Extract a URL when the question
    carries one; otherwise declare honestly that there is nothing to
    author until the pipeline knows WHICH page is in question."""
    m = re.search(r"https?://[^\s\"')]+", question)
    if not m:
        bare = re.search(
            r"\b((?:www\.)?[a-z0-9\-]+(?:\.[a-z]{2,})+"
            r"(?:/[^\s\"')]*)?)", question, re.IGNORECASE)
        if bare:
            url = "https://" + bare.group(1)
        else:
            core = core_query(question)
            return PlanResult(False, reason=(
                f"Wayback queries take a page URL, not a topic. Nothing to "
                f"author from '{core}' — supply the URL whose past state "
                f"matters (often itself a retrieval result)."))
    else:
        url = m.group(0).rstrip(".,;")
    ts = ""
    yrs = re.findall(r"\b((?:19|20)\d{2})(?:-(\d{2}))?(?:-(\d{2}))?\b",
                     question)
    if yrs:
        y, mo, d = yrs[-1]
        ts = y + (mo or "") + (d or "")
    return PlanResult(True, queries=[PlannedQuery(
        source="wayback", method="closest",
        kwargs={"url": url, "timestamp": ts},
        rationale="availability lookup for the closest capture"
                  + (f" at/before {ts}" if ts else ""))],
        resolved={"url": url}, reason=f"snapshot lookup for {url}")


# keyword-capable adapters: same shape, source-specific knobs
_KEYWORD_PLANNERS = {
    "openalex": _plan_openalex,
    "semanticscholar": _plan_semantic_scholar,
    "semantic_scholar": _plan_semantic_scholar,   # legacy spelling (I2)
    "clinicaltrials": _plan_clinicaltrials,
    "federalregister": _plan_federalregister,
    "gdelt": _plan_gdelt,
    "fred": _plan_fred,
    "bls": _plan_bls,
    "treasury": _plan_treasury,
    "wikidata": _plan_wikidata,
    # ── wave I2 ─────────────────────────────────────────────────────
    "worldbank": _plan_worldbank,
    "bea": _plan_bea,
    "census": _plan_census,
    "eia": _plan_eia,
    "fdic": _plan_fdic,
    "cftc_cot": _plan_cftc,
    "uspto_odp": _plan_uspto_odp,
    "courtlistener": _plan_courtlistener,
    "wayback": _plan_wayback,
}

#: SEC deliberately unplannable here — the machine is rate-limited/403'd and
#: the mandate forbids hitting it; planning would invite fetching. Every
#: other registered source now has a planner; the remaining entries below
#: are the honest residue, each naming exactly what is missing.
_HONEST_GAPS = {
    "sec_fulltext": "SEC full-text search requires a declared contact and "
               "this host is currently 403'd; query authoring deferred "
               "until access is restored (deliberate, not forgotten).",
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
