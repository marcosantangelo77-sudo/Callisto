"""PubMed via NCBI E-utilities. Tier 4 (secondary analysis).

Sources (no key, free; E-utilities stated limit is 3 req/s without an API
key — we self-limit to ~1 req/s, min_interval_s=1.0):
  eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi   (pmid search)
  eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi  (metadata per PMID)

Provenance class: SECONDARY to the trial itself (tier 4). A published
paper reporting a phase-2 outcome is analysis OF the experiment;
ClinicalTrials.gov (tier 1) remains the primary record of the trial.
A PubMed hit corroborates but must never outrank the registry entry.

Answers: which peer-reviewed papers exist on a topic, with journal,
date, authors, DOI, and publication-type tags (Randomized Controlled
Trial, Review...) — i.e. RESULTS coverage complementing trial REGISTRATION.
Cannot answer: full text or abstracts in this adapter (esummary carries
metadata only), unpublished/negative results that were never written up,
FDA approval status, individual participant data, and it cannot
guarantee a paper corresponds to any specific NCT registration.

Rate limits: <=3 req/s without an API key per NCBI policy; declared here
and enforced by min_interval_s=1.0 through RestSource's limiter.
"""

from __future__ import annotations

from tools.sources.base import RestSource, SourceSpec

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

SPEC = SourceSpec(
    name="pubmed",
    base_url=EUTILS,
    description="PubMed (NCBI E-utilities): peer-reviewed literature "
                "search and citation metadata",
    answers=(
        "peer-reviewed papers on a biomedical topic (titles, journals, "
        "dates, DOIs)",
        "publication types tagging results papers (randomized controlled "
        "trial, review, meta-analysis)",
        "literature coverage complementing ClinicalTrials.gov registrations",
    ),
    cannot_answer=(
        "full text or abstracts (metadata only in this adapter)",
        "unpublished or never-written-up results",
        "FDA approval decisions",
        "individual participant data",
    ),
    tier=4,
    min_interval_s=1.0,
    terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
)


class PubMedAdapter:
    def __init__(self, source: RestSource):
        self.source = source

    def search(self, query: str, limit: int = 10) -> dict:
        """esearch -> {'count', 'pmids', 'query_translation'}."""
        url = self.source.build_url("/esearch.fcgi", {
            "db": "pubmed", "term": query,
            "retmax": min(int(limit), 100),
            "retmode": "json", "sort": "date",
        })
        data = self.source.get_json(url)[0]
        res = data.get("esearchresult") or {}
        if not res:
            raise ValueError(
                f"pubmed: esearch returned no esearchresult "
                f"(keys={sorted(data)[:8]})")
        return {
            "count": int(res.get("count", 0)),
            "pmids": list(res.get("idlist", [])),
            "query_translation": res.get("querytranslation", ""),
            "_fetch": self._fetch_meta(),
        }

    def summarize(self, pmids: list[str]) -> dict:
        """esummary -> {pmid: {title, journal, pubdate, doi, pubtypes,
        authors, lastauthor}} for each id present in the response."""
        ids = ",".join(p.strip() for p in pmids if p.strip())
        if not ids:
            return {}
        url = self.source.build_url("/esummary.fcgi", {
            "db": "pubmed", "id": ids, "retmode": "json"})
        data = self.source.get_json(url)[0]
        result = data.get("result") or {}
        out: dict = {}
        for uid in result.get("uids", []):
            rec = result.get(str(uid)) or {}
            doi = ""
            for aid in rec.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break
            out[str(uid)] = {
                "title": rec.get("title", ""),
                "journal": rec.get("source", ""),
                "pubdate": rec.get("pubdate", ""),
                "epubdate": rec.get("epubdate", ""),
                "doi": doi,
                "pubtypes": list(rec.get("pubtype", [])),
                "authors": [a.get("name", "") for a in rec.get("authors", [])],
                "lastauthor": rec.get("lastauthor", ""),
            }
        out["_fetch"] = self._fetch_meta()
        return out

    def _fetch_meta(self) -> dict:
        rec = self.source.last_record
        if rec is None:
            return {}
        return {"url": rec.url, "sha256": rec.content_sha256,
                "fetched_at": rec.fetched_at}
