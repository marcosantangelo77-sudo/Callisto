"""
Generation pipeline for the hypothesis generator: template expansion,
cluster discovery, ladder-based generation, cluster analysis, and the
wiki-grounded variance-enforced generator.

Extracted from tools/hypothesis_generator.py as part of the hypgen split.
Each public function takes the owning HypothesisGenerator (as ``gen``)
plus its arguments; the facade methods are thin wrappers over these.

Write-safety contract (mirrors tools/hypgen/persistence.py):
  * Hypothesis creation goes exclusively through
    HypothesisManager.create_hypothesis.
  * This module performs NO direct SQL writes at all.
  * Never arms live betting; no "live" status is referenced here.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.embeddings import embed_batch
from tools.hypgen.persistence import compute_temporal_metadata
from tools.hypgen.prompts import (
    avg_pairwise_distance,
    build_claude_prompt,
    build_grounded_prompt,
    enforce_variance,
    parse_candidates,
    parse_json_array,
)
from tools.hypgen.seeds import pick_unexplored_seeds
from tools.hypgen.templates import (
    HYPOTHESIS_TEMPLATES,
    NEGATIVE_EXAMPLES_N,
    expand_variables,
)

logger = logging.getLogger("callisto.hypgen.generation")


# ──────────────────────────────────────────────────────────────
# Template-driven generation
# ──────────────────────────────────────────────────────────────

async def generate_from_templates(
    gen,
    sport: str,
    max_hypotheses: int = 50,
    training_cutoff_date: Optional[str] = None,
) -> list[dict]:
    """
    Generate hypotheses from templates for a given sport.
    Expands variable combinations and creates draft hypotheses.
    Skips combinations that already exist.

    Args:
        gen: owning HypothesisGenerator instance.
        sport: Sport key (e.g., "basketball_nba")
        max_hypotheses: Max hypotheses to create this call
        training_cutoff_date: ISO date string (YYYY-MM-DD). Data up to this
            date is the training set; backtests will use data after this date.
            Defaults to 30 days before today.

    Returns list of created hypothesis summaries.
    """
    manager = gen.hypothesis_manager
    existing_names = await manager.get_all_names()

    temporal = compute_temporal_metadata(training_cutoff_date)
    training_period_start = temporal["training_period_start"]
    training_period_end = temporal["training_period_end"]
    forward_test_start = temporal["forward_test_start"]

    created = []

    for template in HYPOTHESIS_TEMPLATES:
        if sport not in template["sport_filter"]:
            continue

        # Player prop templates now supported — prop_snapshots provides
        # multi-book data and BacktestEngine._process_prop_snapshots handles devig.

        # Generate all variable combinations
        combos = expand_variables(template["variables"])

        for combo in combos:
            if len(created) >= max_hypotheses:
                break

            # Fill template
            name = template["name"].format(**combo)
            if name in existing_names:
                continue

            thesis = template["thesis"].format(**combo)
            market_type = template["market_type"].format(**combo)
            edge_threshold = combo.get("min_edge", 2) / 100.0

            # Build model config with temporal metadata
            model_config = {}
            for k, v in template["model_config"].items():
                if isinstance(v, str) and "{" in v:
                    model_config[k] = v.format(**combo)
                else:
                    model_config[k] = v

            # Convert string numbers to int
            if "consensus_min_books" in model_config:
                try:
                    model_config["consensus_min_books"] = int(
                        model_config["consensus_min_books"]
                    )
                except (ValueError, TypeError):
                    pass

            # Attach temporal isolation metadata
            model_config["training_period_start"] = training_period_start
            model_config["training_period_end"] = training_period_end
            model_config["forward_test_start"] = forward_test_start

            try:
                hid = await manager.create_hypothesis(
                    name=name,
                    thesis=thesis,
                    sport=sport,
                    market_type=market_type,
                    model_config=model_config,
                    edge_threshold=edge_threshold,
                    notes=(
                        f"Auto-generated from template '{template['id']}'. "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": name,
                    "template": template["id"],
                    "variables": combo,
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                })
                existing_names.add(name)
            except Exception as e:
                logger.warning(f"Failed to create hypothesis '{name}': {e}")

    logger.info(
        f"Generated {len(created)} hypotheses for {sport} "
        f"from {len(HYPOTHESIS_TEMPLATES)} templates "
        f"(training cutoff: {training_period_end})"
    )
    return created


# ──────────────────────────────────────────────────────────────
# Cluster-driven generation
# ──────────────────────────────────────────────────────────────

async def generate_from_clusters(
    gen,
    collection: str = "prop_outcomes",
    similarity_threshold: float = 0.85,
    min_cluster_size: int = 10,
    min_hit_rate_delta: float = 0.05,
    data_period: str | None = None,
) -> list[dict]:
    """
    Analyze embedding clusters to discover data-driven hypotheses.

    For each cluster of similar prop outcomes:
      1. Check if the cluster has a statistically interesting hit rate
      2. If hit rate diverges from expected, generate a hypothesis
      3. Extract common features from the cluster as context factors

    Args:
        gen: owning HypothesisGenerator instance.
        collection: which embedding collection to cluster
        similarity_threshold: min cosine similarity for clustering
        min_cluster_size: ignore clusters smaller than this
        min_hit_rate_delta: min deviation from expected to generate hypothesis
        data_period: 'historical' = cluster only on historical data (for backtesting),
                     'recent' = only recent data, None = all data (for live trading)

    Returns list of created hypothesis summaries.
    """
    manager = gen.hypothesis_manager
    clusters = await gen.vector_store.cluster_by_similarity(
        collection, threshold=similarity_threshold, data_period=data_period
    )

    created = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue

        # Analyze cluster
        analysis = analyze_cluster(cluster)
        if not analysis:
            continue

        hit_rate = analysis["hit_rate"]
        expected_rate = analysis["expected_rate"]
        delta = hit_rate - expected_rate

        if abs(delta) < min_hit_rate_delta:
            continue

        # Generate hypothesis from cluster pattern
        side = "Over" if delta > 0 else "Under"
        common = analysis["common_features"]
        sport = common.get("sport", "basketball_nba")
        market = common.get("market", "player_points")

        name = (
            f"Cluster-discovered: {market.replace('player_', '')} "
            f"{side} edge ({common.get('pattern_desc', 'unknown pattern')})"
        )

        thesis = (
            f"In situations matching this cluster pattern "
            f"(N={len(cluster)}, hit_rate={hit_rate:.1%} vs "
            f"expected {expected_rate:.1%}), {side} bets on "
            f"{market} show a {abs(delta)*100:.1f}% edge. "
            f"Pattern features: {common.get('pattern_desc', 'see metadata')}."
        )

        # Tag which embedding data the hypothesis was derived from
        period_label = data_period or "all"

        # Compute temporal isolation metadata for cluster-derived hypotheses
        today = datetime.now(timezone.utc).date()
        training_cutoff = today - timedelta(days=30)
        training_period_start = "2023-01-01"
        training_period_end = str(training_cutoff)
        forward_test_start = str(training_cutoff + timedelta(days=1))

        try:
            hid = await manager.create_hypothesis(
                name=name,
                thesis=thesis,
                sport=sport,
                market_type=market,
                model_config={
                    "type": "cluster_derived",
                    "devig_method": "power",
                    "target_book": "draftkings",
                    "consensus_min_books": 3,
                    "cluster_features": common,
                    "source_cluster_size": len(cluster),
                    "source_data_period": period_label,
                    "training_period_start": training_period_start,
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                },
                edge_threshold=abs(delta),
                notes=(
                    f"Auto-discovered from {collection} cluster "
                    f"(N={len(cluster)}, data_period={period_label}). "
                    f"Train: [{training_period_start}..{training_period_end}], "
                    f"forward-test from {forward_test_start}."
                ),
            )
            created.append({
                "hypothesis_id": hid,
                "name": name,
                "cluster_size": len(cluster),
                "hit_rate": round(hit_rate, 4),
                "expected_rate": round(expected_rate, 4),
                "delta": round(delta, 4),
                "data_period": period_label,
                "training_period_end": training_period_end,
                "forward_test_start": forward_test_start,
            })
        except Exception as e:
            logger.warning(f"Failed to create cluster hypothesis: {e}")

    logger.info(
        f"Generated {len(created)} hypotheses from {len(clusters)} clusters "
        f"in '{collection}'"
    )
    return created


def analyze_cluster(cluster: list[dict]) -> Optional[dict]:
    """
    Analyze a cluster of prop outcomes to find patterns.
    Returns analysis dict with hit_rate, expected_rate, common_features.
    """
    hits = 0
    total = 0
    edges = []
    sports = []
    markets = []
    players = []

    for item in cluster:
        meta = item.get("metadata") or {}
        if meta.get("hit") is not None:
            total += 1
            if meta["hit"]:
                hits += 1
        if meta.get("edge") is not None:
            edges.append(meta["edge"])
        if meta.get("sport"):
            sports.append(meta["sport"])
        if meta.get("market"):
            markets.append(meta["market"])
        if meta.get("player"):
            players.append(meta["player"])

    if total < 5:
        return None

    hit_rate = hits / total
    # Expected rate from book implied probabilities
    expected_probs = [
        item.get("metadata", {}).get("book_implied_over", 0.5)
        for item in cluster
        if item.get("metadata", {}).get("book_implied_over") is not None
    ]
    expected_rate = (
        sum(expected_probs) / len(expected_probs)
        if expected_probs
        else 0.5
    )

    # Find most common features
    def mode(lst):
        if not lst:
            return None
        return max(set(lst), key=lst.count)

    common_sport = mode(sports)
    common_market = mode(markets)

    # Build pattern description
    pattern_parts = []
    if common_sport:
        pattern_parts.append(common_sport.replace("basketball_", ""))
    if common_market:
        pattern_parts.append(common_market.replace("player_", ""))
    avg_edge = sum(edges) / len(edges) if edges else 0
    if avg_edge:
        pattern_parts.append(f"avg_edge={avg_edge:.1%}")
    pattern_desc = " ".join(pattern_parts) if pattern_parts else "mixed"

    return {
        "hit_rate": hit_rate,
        "expected_rate": expected_rate,
        "total_resolved": total,
        "avg_edge": avg_edge,
        "common_features": {
            "sport": common_sport,
            "market": common_market,
            "pattern_desc": pattern_desc,
            "unique_players": len(set(players)),
        },
    }


# ──────────────────────────────────────────────────────────────
# Ladder-driven generation
# ──────────────────────────────────────────────────────────────

async def generate_from_ladder(
    gen,
    sport: str,
    data_summary: str,
) -> list[dict]:
    """
    Ask the hypothesis_gen ladder (qwen36 primary, Claude last) to
    generate novel hypotheses from a data summary.

    The historical name of this entry point was generate_from_claude;
    it is kept on the facade for call-site compatibility, but the ladder
    picks the best available model per task_type and respects
    CALLISTO_LOCAL_ONLY + Claude Max hours demotion.
    """
    from inference import escalate_with_ladder

    result = await escalate_with_ladder(
        prompt=build_claude_prompt(sport, data_summary),
        system_context="Callisto hypothesis generation — return structured JSON only.",
        task_type="hypothesis_gen",
        timeout=120,
        hermes_caller="hypothesis_gen",
    )

    if result.get("error"):
        logger.error(f"Hypothesis generation ladder failed: {result['error']}")
        return []

    # Parse response
    hypotheses_raw = parse_json_array(result.get("content", ""))
    if not hypotheses_raw:
        logger.warning("Could not find JSON array in Claude response")
        return []

    # Temporal metadata for ladder-generated hypotheses
    temporal = compute_temporal_metadata(None)
    training_period_start = temporal["training_period_start"]
    training_period_end = temporal["training_period_end"]
    forward_test_start = temporal["forward_test_start"]

    created = []
    for h_raw in hypotheses_raw:
        try:
            mc = h_raw.get("model_config", {
                "type": "consensus_devig",
                "devig_method": "power",
                "target_book": "draftkings",
                "consensus_min_books": 3,
            })
            # Inject temporal isolation metadata
            mc["training_period_start"] = training_period_start
            mc["training_period_end"] = training_period_end
            mc["forward_test_start"] = forward_test_start

            hid = await gen.hypothesis_manager.create_hypothesis(
                name=h_raw.get("name", "Unnamed"),
                thesis=h_raw.get("thesis", ""),
                sport=sport,
                market_type=h_raw.get("market_type", "spreads"),
                model_config=mc,
                edge_threshold=float(h_raw.get("edge_threshold", 0.02)),
                notes=(
                    f"Auto-generated by Claude Code hypothesis engine. "
                    f"Train: [{training_period_start}..{training_period_end}], "
                    f"forward-test from {forward_test_start}."
                ),
            )
            created.append({
                "hypothesis_id": hid,
                "name": h_raw.get("name"),
                "source": "claude_code",
                "training_period_end": training_period_end,
                "forward_test_start": forward_test_start,
            })
        except Exception as e:
            logger.warning(f"Failed to create Claude hypothesis: {e}")

    logger.info(f"Claude Code generated {len(created)} hypotheses for {sport}")
    return created


# ──────────────────────────────────────────────────────────────
# Wiki-grounded, variance-enforced generation
# ──────────────────────────────────────────────────────────────

async def generate_wiki_grounded(
    gen,
    sport: str,
    focus_market: Optional[str] = None,
    n_candidates: int = 8,
    max_keep: int = 5,
    include_seeds: bool = True,
) -> dict:
    """
    Retrieve wiki articles + rejection examples, then call the hypothesis_gen
    ladder to produce N candidates, embed them, enforce diversity vs each
    other AND vs prior corpus, and persist the survivors as draft hypotheses.

    Returns a dict:
      {
        "generated": [<hyp dict>, ...],     # final survivors, persisted
        "rejected": [{"reason", "candidate"}, ...],
        "wiki_context_topics": [...],
        "seeds_used": [...],
        "model_used": <str>,
        "diversity_metric": float,          # avg pairwise cosine distance
                                            # (1 - sim) among survivors
      }
    """
    from inference import escalate_with_ladder

    manager = gen.hypothesis_manager

    # 1. Retrieve wiki articles related to the sport/market ------------
    wiki_articles = await gen._retrieve_wiki_context(sport, focus_market)

    # 2. Pull a handful of rejected hypotheses as negative examples ----
    rejected_examples = await gen._retrieve_rejection_examples(
        sport, focus_market, limit=NEGATIVE_EXAMPLES_N
    )

    # 3. Pick underexplored seeds --------------------------------------
    seeds: list[dict] = []
    if include_seeds:
        try:
            existing_names = await manager.get_all_names()
            existing_theses = await gen._recent_theses(sport)
            seeds = pick_unexplored_seeds(
                existing_names, existing_theses, sport=sport, max_seeds=3,
            )
        except Exception as e:
            logger.debug(f"Seed retrieval failed (non-fatal): {e}")
            seeds = []

    # 4. Build the grounding prompt ------------------------------------
    prompt = build_grounded_prompt(
        sport, focus_market, wiki_articles, rejected_examples, seeds, n_candidates
    )

    # 5. Call the ladder ------------------------------------------------
    result = await escalate_with_ladder(
        prompt=prompt,
        system_context=(
            "You are Callisto's hypothesis engine. Produce specific, "
            "SQL-filterable, backtest-able hypotheses. Return JSON ONLY."
        ),
        task_type="hypothesis_gen",
        timeout=180,
        hermes_caller="hypothesis_gen_wiki",
    )
    model_used = result.get("model_used", "unknown")
    content = result.get("content", "")
    empty_result = {
        "generated": [], "rejected": [],
        "wiki_context_topics": [a.get("topic") for a in wiki_articles],
        "seeds_used": [s["seed_id"] for s in seeds],
        "model_used": model_used, "diversity_metric": 0.0,
    }
    if result.get("error"):
        logger.error(f"grounded generator ladder error: {result['error']}")
        return dict(empty_result)

    candidates = parse_candidates(content)
    if not candidates:
        logger.warning("grounded generator returned no parseable candidates")
        return dict(empty_result)

    # 6. Embed candidate thesis statements in ONE batch -----------------
    thesis_texts = [
        (c.get("thesis_statement")
         or c.get("thesis")
         or c.get("name")
         or "").strip()
        for c in candidates
    ]
    try:
        # Resolve embed_batch through the facade module so tests/callers can
        # monkeypatch tools.hypothesis_generator.embed_batch.
        import sys

        facade = sys.modules.get("tools.hypothesis_generator")
        batch_fn = getattr(facade, "embed_batch", None) or embed_batch
        cand_embs = await batch_fn(thesis_texts)
    except Exception as e:
        logger.warning(f"embed_batch failed ({e}); skipping variance step")
        cand_embs = []

    # 7. Variance-enforce vs each other and vs prior corpus --------------
    kept_indices, drop_reasons = await enforce_variance(
        candidates, cand_embs, wiki_articles
    )
    kept_indices = kept_indices[:max_keep]

    # 8. Persist survivors as draft hypotheses ---------------------------
    temporal = compute_temporal_metadata(None)
    training_period_start = temporal["training_period_start"]
    training_period_end = temporal["training_period_end"]
    forward_test_start = temporal["forward_test_start"]

    created: list[dict] = []
    rejected_log = drop_reasons[:]
    for i in kept_indices:
        c = candidates[i]
        try:
            mc = c.get("model_config") or {
                "type": "consensus_devig",
                "devig_method": "power",
                "target_book": "draftkings",
                "consensus_min_books": 3,
            }
            mc["training_period_start"] = training_period_start
            mc["training_period_end"] = training_period_end
            mc["forward_test_start"] = forward_test_start
            mc["grounding"] = {
                "source": "wiki_grounded_v1",
                "wiki_topics": [a.get("topic") for a in wiki_articles][:5],
                "seed_ids": [s["seed_id"] for s in seeds],
                "ladder_model": model_used,
            }

            thesis_txt = (
                c.get("thesis_statement")
                or c.get("thesis")
                or c.get("signal_logic")
                or ""
            )
            name = c.get("name", f"auto_{sport}_{i}")
            market = (c.get("market_type") or c.get("market")
                      or focus_market or "spreads")
            edge = c.get("edge_threshold")
            if edge is None:
                edge = c.get("ic_prior_estimate", 0.02)
            try:
                edge = float(edge)
            except (TypeError, ValueError):
                edge = 0.02

            hid = await manager.create_hypothesis(
                name=name,
                thesis=thesis_txt,
                sport=sport,
                market_type=market,
                model_config=mc,
                edge_threshold=edge,
                notes=(
                    f"Wiki-grounded generation (model={model_used}). "
                    f"Train: [{training_period_start}..{training_period_end}], "
                    f"forward-test from {forward_test_start}."
                ),
            )
            created.append({
                "hypothesis_id": hid,
                "name": name,
                "thesis": thesis_txt,
                "market_type": market,
                "source": "wiki_grounded",
            })
        except Exception as e:
            logger.warning(f"grounded generator persist failed: {e}")
            rejected_log.append({"reason": f"persist_error: {e}",
                                 "candidate": c})

    # 9. Diversity metric on the survivors ------------------------------
    kept_embs = [cand_embs[i] for i in kept_indices if i < len(cand_embs)]
    diversity = avg_pairwise_distance(kept_embs)

    logger.info(
        f"grounded generator: sport={sport} survivors={len(created)} "
        f"dropped={len(rejected_log)} diversity={diversity:.3f} "
        f"model={model_used}"
    )
    return {
        "generated": created,
        "rejected": rejected_log,
        "wiki_context_topics": [a.get("topic") for a in wiki_articles],
        "seeds_used": [s["seed_id"] for s in seeds],
        "model_used": model_used,
        "diversity_metric": round(diversity, 4),
    }
