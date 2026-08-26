"""
LLM prompt assembly, response parsing, and variance enforcement for the
hypothesis generator.

Extracted from tools/hypothesis_generator.py as part of the hypgen split.
No DB writes happen here — persistence lives in tools.hypgen.persistence.

NOTE on silent writes: the grounded prompt asks the LLM to emit
`edge_threshold` / `signal_generated` fields. Those are treated as
DIAGNOSE-ONLY metadata from the model response; this module never issues
any `signal_generated` or `edge_threshold` UPDATE statements against the
hypotheses table. Persistence of edge thresholds happens only through
HypothesisManager.create_hypothesis at draft creation time.
"""

import json
import logging
from typing import Optional

from tools.embeddings import embed_batch, cosine_similarity

from tools.hypgen.templates import (
    CANDIDATE_DEDUP_SIM,
    PRIOR_CORPUS_SIM,
    WIKI_CONTEXT_TOP_K,
)

logger = logging.getLogger("callisto.hypgen.prompts")


# ──────────────────────────────────────────────────────────────
# Legacy Claude prompt
# ──────────────────────────────────────────────────────────────

def build_claude_prompt(sport: str, data_summary: str) -> str:
    """Prompt for the generate_from_claude ladder call."""
    return (
        f"You are Callisto's hypothesis engine. Given the following data summary "
        f"for {sport}, generate 3-5 novel, testable betting hypotheses.\n\n"
        f"DATA SUMMARY:\n{data_summary}\n\n"
        f"For each hypothesis, return JSON with:\n"
        f"- name: short descriptive name\n"
        f"- thesis: detailed testable claim\n"
        f"- market_type: one of (spreads, totals, h2h, player_points, "
        f"player_rebounds, player_assists, player_threes, "
        f"player_points_rebounds_assists)\n"
        f"- edge_threshold: minimum edge to flag (decimal, e.g., 0.03)\n"
        f"- model_config: dict with devig_method, target_book, "
        f"consensus_min_books, and any context_factors\n\n"
        f"Return ONLY a JSON array. No explanation text."
    )


def parse_json_array(content: str) -> list[dict]:
    """Tolerant JSON-array extraction from an LLM response."""
    if not content:
        return []
    start = content.find("[")
    end = content.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(content[start:end])
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


# ──────────────────────────────────────────────────────────────
# Grounded prompt construction
# ──────────────────────────────────────────────────────────────

def build_grounded_prompt(
    sport: str,
    focus_market: Optional[str],
    wiki_articles: list[dict],
    rejected_examples: list[dict],
    seeds: list[dict],
    n_candidates: int,
) -> str:
    wiki_block = "\n".join(
        f"- [{a.get('topic')}] {a.get('title')}: "
        f"{(a.get('summary') or '')[:220]}"
        for a in wiki_articles[:WIKI_CONTEXT_TOP_K]
    ) or "(no prior wiki articles)"

    neg_block = "\n".join(
        f"- REJECTED: {r['name']} — {(r['thesis'] or '')[:180]}"
        for r in rejected_examples
    ) or "(no prior rejections for this cohort)"

    seed_block = "\n".join(
        f"- SEED {s['seed_id']} ({s['category']}, {s['market_type']}): "
        f"{s['thesis_template'][:180]}"
        for s in seeds
    ) or "(no seeds supplied)"

    mkt = focus_market or "any market (props/totals/spreads/h2h/live/parlay)"
    return (
        f"Sport: {sport}\nFocus market: {mkt}\n\n"
        f"THINGS THE WIKI ALREADY KNOWS "
        f"(do NOT propose re-discovery of these — propose COMPLEMENTARY "
        f"or ORTHOGONAL theses):\n{wiki_block}\n\n"
        f"RECENT FAILED HYPOTHESES IN THIS COHORT "
        f"(do NOT propose variations of these shape):\n{neg_block}\n\n"
        f"UNDEREXPLORED THESIS SPACES "
        f"(preferred starting points — specialize to a concrete "
        f"testable form using sport-specific names/markets):\n{seed_block}\n\n"
        f"Generate exactly {n_candidates} DISTINCT candidate hypotheses "
        f"as a JSON array. Each item MUST have:\n"
        f"  - name:               short unique slug\n"
        f"  - market:             specific market key\n"
        f"  - cohort_filter:      SQL-expressible WHERE clause over "
        f"game_contexts / player_stats\n"
        f"  - signal_logic:       why the edge exists, mechanism\n"
        f"  - min_signals:        integer ≥ 20\n"
        f"  - ic_prior_estimate:  float in [0.005, 0.08]\n"
        f"  - variance_justification: one sentence — why this is NOT a "
        f"duplicate of any wiki article or rejected hypothesis above\n"
        f"  - thesis_statement:   2-3 sentence backtestable claim\n"
        f"  - edge_threshold:     float (decimal, e.g., 0.02)\n"
        f"  - model_config:       dict (devig_method, target_book, "
        f"consensus_min_books, context_factors list)\n\n"
        f"HARD RULES:\n"
        f"1. Reject vague wording. 'Team plays better when rested' is "
        f"BANNED; say exactly which column, threshold, and side.\n"
        f"2. Every candidate must be DIFFERENT from the others — do not "
        f"vary only one numeric threshold.\n"
        f"3. Prefer specific official/umpire/ref/coach/venue/microstructure "
        f"triggers over blanket team-level claims.\n"
        f"4. Return ONLY the JSON array. No explanation text, no code "
        f"fences outside the JSON."
    )


# ──────────────────────────────────────────────────────────────
# Candidate parsing (tolerant, strips code fences)
# ──────────────────────────────────────────────────────────────

def parse_candidates(content: str) -> list[dict]:
    if not content:
        return []
    # Strip code fences if present.
    txt = content.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        # drop optional "json" language marker
        if txt.lstrip().lower().startswith("json"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt
    start = txt.find("[")
    end = txt.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(txt[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


# ──────────────────────────────────────────────────────────────
# Variance enforcement
# ──────────────────────────────────────────────────────────────

async def enforce_variance(
    candidates: list[dict],
    cand_embs: list[list[float]],
    wiki_articles: list[dict],
) -> tuple[list[int], list[dict]]:
    """Greedy selection that drops:
      (a) near-duplicate candidates (sim >= CANDIDATE_DEDUP_SIM)
      (b) candidates that cluster against a wiki article
          (sim >= PRIOR_CORPUS_SIM)

    Returns (kept_indices, drop_reasons)."""
    if not cand_embs or len(cand_embs) != len(candidates):
        # Embeddings unavailable — trust the LLM and accept all.
        return list(range(len(candidates))), []

    # Load wiki embeddings for articles we have summaries for.
    # We embed summaries once per call (cheap — typically 8 items).
    wiki_texts = [
        (a.get("summary") or a.get("title") or a.get("topic") or "")[:500]
        for a in wiki_articles
    ]
    wiki_texts = [t for t in wiki_texts if t]
    try:
        wiki_embs = await embed_batch(wiki_texts) if wiki_texts else []
    except Exception as e:
        logger.debug(f"wiki embed_batch failed: {e}")
        wiki_embs = []

    kept: list[int] = []
    drop_reasons: list[dict] = []

    # Score candidates by ic_prior as a quality signal.
    def _q(i: int) -> float:
        try:
            return float(candidates[i].get("ic_prior_estimate", 0.0))
        except (TypeError, ValueError):
            return 0.0

    order = sorted(range(len(candidates)), key=_q, reverse=True)

    for i in order:
        emb_i = cand_embs[i]

        # Drop vs already-kept candidates.
        dup = False
        for j in kept:
            sim = cosine_similarity(emb_i, cand_embs[j])
            if sim >= CANDIDATE_DEDUP_SIM:
                dup = True
                drop_reasons.append({
                    "reason": f"near_duplicate_of_candidate_{j} (sim={sim:.3f})",
                    "candidate": candidates[i],
                })
                break
        if dup:
            continue

        # Drop vs wiki articles already in the corpus.
        prior_hit = False
        for w_emb, w_meta in zip(wiki_embs, wiki_articles):
            sim = cosine_similarity(emb_i, w_emb)
            if sim >= PRIOR_CORPUS_SIM:
                prior_hit = True
                drop_reasons.append({
                    "reason": (
                        f"overlaps_wiki_{w_meta.get('topic')} "
                        f"(sim={sim:.3f})"
                    ),
                    "candidate": candidates[i],
                })
                break
        if prior_hit:
            continue

        kept.append(i)

    # Sort kept back into original order for stable output.
    kept.sort()
    return kept, drop_reasons


def avg_pairwise_distance(embs: list[list[float]]) -> float:
    """1 - mean cosine similarity across all pairs (higher = more diverse)."""
    n = len(embs)
    if n < 2:
        return 0.0
    sims: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            sims.append(cosine_similarity(embs[i], embs[j]))
    if not sims:
        return 0.0
    return 1.0 - (sum(sims) / len(sims))
