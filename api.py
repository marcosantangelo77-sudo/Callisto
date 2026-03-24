"""
FastAPI REST layer for Callisto.

Endpoints for task submission, session retrieval, world queries, and health checks.
Runs on port 8420.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agp import Domain
from logging_config import setup_logging
from memory import MemoryStore
from monitor import HealthMonitor
from orchestrator import Orchestrator
from task_queue import TaskQueue
from tools.line_monitor import LineMonitor
from tools.clv_tracker import CLVTracker
from tools.autonomous import AutonomousLoop, ResearchLoop
from tools import telegram
from tools.telegram import TelegramListener
from tools.schema import ensure_schema
from tools.hypothesis import HypothesisManager
from tools.historical_odds import HistoricalOddsFetcher
from tools.backtest import BacktestEngine
from tools.embeddings import VectorStore
from tools.hypothesis_generator import HypothesisGenerator
from tools.data_collector import DataCollector
from tools.health import SystemHealth
from tools.pipeline_integrity import get_checker as get_integrity_checker, initialize as init_integrity

load_dotenv()

setup_logging()
logger = logging.getLogger("callisto.api")

CALLISTO_PORT = int(os.getenv("CALLISTO_PORT", "8420"))

# Shared state
memory: Optional[MemoryStore] = None
queue: Optional[TaskQueue] = None
orchestrator_instance: Optional[Orchestrator] = None
monitor: Optional[HealthMonitor] = None
line_monitor: Optional[LineMonitor] = None
clv_tracker: Optional[CLVTracker] = None
autonomous: Optional[AutonomousLoop] = None
telegram_listener: Optional[TelegramListener] = None
hypothesis_manager: Optional[HypothesisManager] = None
historical_fetcher: Optional[HistoricalOddsFetcher] = None
backtest_engine: Optional[BacktestEngine] = None
vector_store: Optional[VectorStore] = None
hypothesis_generator: Optional[HypothesisGenerator] = None
data_collector: Optional[DataCollector] = None
research_loop: Optional[ResearchLoop] = None
system_health: Optional[SystemHealth] = None
worker_task: Optional[asyncio.Task] = None


async def task_worker():
    """Background worker: polls task queue and runs AGP sessions."""
    while True:
        try:
            task = await queue.get_next()
            if task is None:
                await asyncio.sleep(2)
                continue

            task_id = task["task_id"]
            logger.info(f"Worker picked up task {task_id}: {task['query']}")

            try:
                result = await orchestrator_instance.run_session(task["query"])
                session_id = result.get("session_id")
                await queue.complete_task(task_id, result, session_id=session_id)
                logger.info(f"Task {task_id} completed, session {session_id}")
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                await queue.fail_task(task_id, str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle manager."""
    global memory, queue, orchestrator_instance, monitor, line_monitor, clv_tracker, autonomous, telegram_listener, hypothesis_manager, historical_fetcher, backtest_engine, vector_store, hypothesis_generator, data_collector, research_loop, system_health, worker_task

    # Startup — ensure DB schema is up to date
    await ensure_schema()

    memory = MemoryStore()
    await memory.initialize()

    queue = TaskQueue()
    await queue.initialize()

    orchestrator_instance = Orchestrator(memory)
    monitor = HealthMonitor()
    await monitor.start()

    # Line movement monitor — autonomous odds tracking
    line_monitor = LineMonitor()
    await line_monitor.initialize()
    await line_monitor.start()

    # CLV tracker — bet tracking and closing line value measurement
    clv_tracker = CLVTracker()
    await clv_tracker.initialize()

    # Hypothesis testing framework
    hypothesis_manager = HypothesisManager()
    await hypothesis_manager.initialize()
    historical_fetcher = HistoricalOddsFetcher()
    await historical_fetcher.initialize()
    backtest_engine = BacktestEngine(
        hypothesis_manager=hypothesis_manager,
        historical_fetcher=historical_fetcher,
    )
    await backtest_engine.initialize()

    # Vector store + hypothesis generator + data collector
    vector_store = VectorStore()
    await vector_store.initialize()
    hypothesis_generator = HypothesisGenerator(
        hypothesis_manager=hypothesis_manager,
        vector_store=vector_store,
    )
    await hypothesis_generator.initialize()
    data_collector = DataCollector()
    await data_collector.initialize()

    # Autonomous reasoning loop — proactive edge analysis
    autonomous = AutonomousLoop(orchestrator_instance, line_monitor)
    await autonomous.start()

    # Research loop — 24/7 hypothesis machine
    research_loop = ResearchLoop(
        hypothesis_manager=hypothesis_manager,
        hypothesis_generator=hypothesis_generator,
        backtest_engine=backtest_engine,
        data_collector=data_collector,
        vector_store=vector_store,
        orchestrator=orchestrator_instance,
    )
    await research_loop.start()

    # Pipeline integrity checker — detects silent failures
    await init_integrity()

    # System health monitor — Layer 2 resilience
    system_health = SystemHealth()
    system_health.research_loop = research_loop
    system_health.autonomous_loop = autonomous
    await system_health.start()

    # Telegram listener — bidirectional communication from phone
    telegram_listener = TelegramListener(
        orchestrator=orchestrator_instance,
        line_monitor=line_monitor,
        clv_tracker=clv_tracker,
    )
    await telegram_listener.start()

    worker_task = asyncio.create_task(task_worker())
    logger.info(f"Callisto API started on port {CALLISTO_PORT}")

    # Notify on Telegram
    sports = line_monitor.get_status().get("monitored_sports", [])
    await telegram.alert_system(
        f"API started on port {CALLISTO_PORT}\n"
        f"Monitoring: {', '.join(sports)}\n"
        f"Agents: Architect, Manager, Sentinel\n"
        f"Autonomous reasoning: ACTIVE\n"
        f"Research loop: ACTIVE (24/7 hypothesis machine)"
    )

    yield

    # Shutdown
    await telegram.alert_system("Callisto shutting down.", is_error=True)
    await telegram_listener.stop()
    if system_health:
        system_health.write_health_file()
        await system_health.stop()
    if research_loop:
        await research_loop.stop()
    await autonomous.stop()
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    if data_collector:
        await data_collector.close()
    if hypothesis_generator:
        await hypothesis_generator.close()
    if vector_store:
        await vector_store.close()
    await backtest_engine.close()
    await historical_fetcher.close()
    await hypothesis_manager.close()
    await clv_tracker.close()
    await line_monitor.stop()
    await monitor.stop()
    await queue.close()
    await memory.close()
    # Close search backend clients
    from tools.search import close_all_clients
    await close_all_clients()
    # Close odds API client
    from tools.odds_api import close_client as close_odds_client
    await close_odds_client()
    # Close contextual data client
    from tools.contextual_data import close_client as close_ctx_client
    await close_ctx_client()
    # Close embedding client
    from tools.embeddings import close_client as close_embed_client
    await close_embed_client()
    # Close data collector client
    from tools.data_collector import close_client as close_dc_client
    await close_dc_client()
    # Close DK scraper client
    from tools.dk_scraper import close_client as close_dk_client
    await close_dk_client()
    logger.info("Callisto API shut down")


app = FastAPI(
    title="Callisto",
    description="Autonomous multi-agent reasoning system governed by the Aluft Gianne Protocol",
    version="0.1.0",
    lifespan=lifespan,
)


class TaskSubmission(BaseModel):
    query: str
    priority: int = 0


class TaskResponse(BaseModel):
    task_id: int


@app.post("/task", response_model=TaskResponse)
async def submit_task(submission: TaskSubmission):
    """Submit a query for AGP session processing."""
    task_id = await queue.submit_task(submission.query, submission.priority)
    return TaskResponse(task_id=task_id)


@app.get("/task/{task_id}")
async def get_task(task_id: int):
    """Get task status and result."""
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Get a sealed AGP session with full provenance."""
    session = await memory.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/world/{domain}")
async def query_world(
    domain: str,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    limit: int = 50,
):
    """Query a domain world."""
    try:
        domain_enum = Domain(domain.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {[d.value for d in Domain]}",
        )
    results = await memory.query_world(
        domain_enum, keyword=keyword, min_confidence=min_confidence, limit=limit
    )
    return {"domain": domain_enum.value, "count": len(results), "entries": results}


# --- Betting / Odds endpoints ---

@app.get("/odds/movements")
async def get_movements(sport: Optional[str] = None, limit: int = 20):
    """Get recent line movements detected by the monitor."""
    movements = await line_monitor.get_recent_movements(sport=sport, limit=limit)
    return {"count": len(movements), "movements": movements}


@app.get("/odds/opportunities")
async def get_opportunities(status: str = "open", limit: int = 20):
    """Get current +EV betting opportunities."""
    opps = await line_monitor.get_ev_opportunities(status=status, limit=limit)
    return {"count": len(opps), "opportunities": opps}


@app.get("/odds/snapshots/{sport}")
async def get_snapshots(sport: str, limit: int = 10):
    """Get snapshot history for a sport."""
    snaps = await line_monitor.get_snapshot_history(sport=sport, limit=limit)
    return {"sport": sport, "count": len(snaps), "snapshots": snaps}


@app.post("/odds/snapshot/{sport}")
async def force_snapshot(sport: str):
    """Force an immediate odds snapshot for a sport."""
    result = await line_monitor.force_snapshot(sport)
    return {
        "sport": sport,
        "game_count": result.get("game_count", 0),
        "credits": result.get("credits", {}),
    }


@app.get("/odds/edges")
async def get_edges(sport: Optional[str] = None):
    """Get latest cross-book edges, sharp money signals, and low-vig opportunities."""
    report = line_monitor.get_edge_report(sport=sport)
    return report


@app.post("/odds/parlay-scan/{sport}")
async def parlay_scan(sport: str):
    """Scan for correlated parlay edges on a sport. Pulls odds + alternates."""
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.parlay_scanner import find_correlated_parlay_edges

    # Get standard odds
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

    all_edges = []
    # Scan first 5 games (credit budget awareness)
    for game in odds_data.get("games", [])[:5]:
        event_id = game.get("id", "")
        if not event_id:
            continue
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            continue
        edges = find_correlated_parlay_edges(game, alt_data)
        all_edges.extend(edges)

    return {
        "sport": sport,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "edges_found": len(all_edges),
        "edges": all_edges,
        "credits": odds_data.get("credits", {}),
    }


@app.get("/odds/props/{sport}/{event_id}")
async def scan_props(sport: str, event_id: str, target_book: str = "draftkings", threshold: float = 0.015):
    """
    Scan player props for +EV edges on target book.

    Full pipeline: pull props -> devig each book -> average fair values -> flag edges.
    This is the single-call prop scanner that makes Callisto autonomous.
    """
    from tools.prop_scanner import scan_props_ev
    return await scan_props_ev(sport, event_id, target_book=target_book, edge_threshold=threshold)


@app.get("/odds/dk-props/{sport}")
async def dk_props(sport: str):
    """
    Scrape DraftKings player props for all games in a sport — FREE, no API credits.

    Returns all available player props (points, rebounds, assists, threes, PRA)
    directly from DraftKings' undocumented API. Useful for:
    - Checking current DK prop lines from your phone
    - Feeding the prop scanner with target book data
    - Finding props to cross-reference against other books
    """
    from tools.dk_scraper import scrape_dk_odds, scrape_dk_props

    # First get game list
    games_data = await scrape_dk_odds(sport)
    if games_data.get("error"):
        return {"error": games_data["error"], "games": []}

    results = []
    for game in games_data.get("games", []):
        event_id = game.get("id", "")
        if not event_id:
            continue
        props = await scrape_dk_props(sport, event_id)
        if props.get("player_count", 0) > 0:
            results.append({
                "game": f"{game.get('away_team', '')} @ {game.get('home_team', '')}",
                "event_id": event_id,
                "commence_time": game.get("commence_time", ""),
                **props,
            })

    return {
        "sport": sport,
        "games_with_props": len(results),
        "total_players": sum(r.get("player_count", 0) for r in results),
        "source": "draftkings_scraper",
        "credits_used": 0,
        "games": results,
    }


@app.get("/odds/status")
async def odds_status():
    """Get line monitor status and credit info."""
    return line_monitor.get_status() if line_monitor else {"error": "Monitor not initialized"}


# --- Bet Tracking & CLV ---

class BetSubmission(BaseModel):
    sport: str
    game_description: str
    team: str
    market: str
    bookmaker: str
    placement_odds: int
    placement_point: Optional[float] = None
    stake: float = 100
    event_id: str = ""
    edge_estimate: Optional[float] = None
    notes: str = ""


class BetResolution(BaseModel):
    result: str  # won, lost, push
    payout: Optional[float] = None


@app.post("/bets/record")
async def record_bet(bet: BetSubmission):
    """Record a bet at placement time for CLV tracking."""
    bet_id = await clv_tracker.record_bet(
        sport=bet.sport,
        game_description=bet.game_description,
        team=bet.team,
        market=bet.market,
        bookmaker=bet.bookmaker,
        placement_odds=bet.placement_odds,
        placement_point=bet.placement_point,
        stake=bet.stake,
        event_id=bet.event_id,
        edge_estimate=bet.edge_estimate,
        notes=bet.notes,
    )
    return {"bet_id": bet_id}


@app.post("/bets/{bet_id}/resolve")
async def resolve_bet(bet_id: int, resolution: BetResolution):
    """Resolve a bet as won/lost/push."""
    return await clv_tracker.resolve_bet(bet_id, resolution.result, resolution.payout)


@app.get("/bets/clv-report")
async def clv_report(sport: Optional[str] = None):
    """Get CLV performance report — THE metric for edge measurement."""
    return await clv_tracker.get_clv_report(sport=sport)


@app.get("/bets")
async def list_bets(result: Optional[str] = None, sport: Optional[str] = None, limit: int = 50):
    """Get bet history."""
    return await clv_tracker.get_all_bets(result=result, sport=sport, limit=limit)


@app.get("/bets/bankroll")
async def bankroll_history(limit: int = 50):
    """Get bankroll balance history."""
    return await clv_tracker.get_bankroll_history(limit=limit)


@app.post("/bets/bankroll/init")
async def init_bankroll(balance: float):
    """Set initial bankroll balance."""
    await clv_tracker.set_initial_bankroll(balance)
    return {"balance": balance}


# --- Market Structure Analysis ---

@app.get("/odds/market-analysis/{sport}")
async def market_analysis(sport: str):
    """Full market structure analysis — key numbers, stale lines, Pinnacle benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import full_market_analysis

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

    analysis = full_market_analysis(odds_data.get("games", []), sport)
    analysis["credits"] = odds_data.get("credits", {})
    return analysis


@app.get("/odds/stale-lines/{sport}")
async def stale_lines(sport: str):
    """Find retail book lines that are stale vs sharp benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import find_stale_lines

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

    stale = find_stale_lines(odds_data.get("games", []))
    return {"count": len(stale), "stale_lines": stale, "credits": odds_data.get("credits", {})}


# --- Simulation & Contextual Data ---

class SimulationRequest(BaseModel):
    home_name: str
    away_name: str
    home_off_eff: float = 105.0
    home_def_eff: float = 100.0
    away_off_eff: float = 105.0
    away_def_eff: float = 100.0
    home_pace: float = 70.0
    away_pace: float = 70.0
    home_injuries_impact: float = 0.0
    away_injuries_impact: float = 0.0
    iterations: int = 10000
    sport: str = "basketball_ncaab"
    event_id: str = ""


@app.post("/simulate/basketball")
async def simulate_basketball_game(req: SimulationRequest):
    """Run Monte Carlo simulation and compare against market odds."""
    from tools.simulation import simulate_basketball, compare_to_market, TeamProfile

    home = TeamProfile(
        name=req.home_name,
        offensive_efficiency=req.home_off_eff,
        defensive_efficiency=req.home_def_eff,
        pace=req.home_pace,
        injuries_impact=req.home_injuries_impact,
    )
    away = TeamProfile(
        name=req.away_name,
        offensive_efficiency=req.away_off_eff,
        defensive_efficiency=req.away_def_eff,
        pace=req.away_pace,
        injuries_impact=req.away_injuries_impact,
    )

    sim = simulate_basketball(home, away, iterations=req.iterations)

    result = {
        "simulation": {
            "home_avg_score": round(sim.home_avg_score, 1),
            "away_avg_score": round(sim.away_avg_score, 1),
            "fair_spread": round(sim.fair_spread, 1),
            "fair_total": round(sim.fair_total, 1),
            "home_win_pct": round(sim.home_win_pct * 100, 1),
            "away_win_pct": round(sim.away_win_pct * 100, 1),
            "iterations": sim.iterations,
        },
    }

    # Compare to market if we have an event_id
    if req.event_id:
        from tools.odds_api import get_event_odds
        market = await get_event_odds(
            sport=req.sport, event_id=req.event_id,
            markets="h2h,spreads,totals",
        )
        if not market.get("error"):
            edges = compare_to_market(sim, market)
            result["market_edges"] = edges
            result["edge_count"] = len([e for e in edges if e["ev"]["is_positive_ev"]])

    return result


class PoissonRequest(BaseModel):
    home_expected: float
    away_expected: float
    sport: str = "soccer_epl"
    event_id: str = ""


@app.post("/simulate/poisson")
async def simulate_poisson_game(req: PoissonRequest):
    """Run Poisson simulation for low-scoring sports."""
    from tools.simulation import simulate_poisson
    return simulate_poisson(req.home_expected, req.away_expected)


@app.get("/data/injuries/{sport}")
async def get_injuries(sport: str):
    """Get current injury report from ESPN."""
    from tools.contextual_data import get_injuries as _get_injuries
    return await _get_injuries(sport)


@app.get("/data/scoreboard/{sport}")
async def get_scoreboard(sport: str):
    """Get live scoreboard from ESPN."""
    from tools.contextual_data import get_scoreboard as _get_scoreboard
    return await _get_scoreboard(sport)


@app.get("/data/weather")
async def get_weather(latitude: float, longitude: float, venue: str = ""):
    """Get weather forecast for a venue."""
    from tools.contextual_data import get_weather as _get_weather
    return await _get_weather(latitude, longitude, venue_name=venue)


@app.get("/data/referee")
async def referee_info(refs: str, sport: str = "basketball_nba"):
    """Get referee tendency adjustments. Pass refs as comma-separated names."""
    from tools.contextual_data import get_referee_adjustment
    ref_list = [r.strip() for r in refs.split(",")]
    return get_referee_adjustment(ref_list, sport)


# --- Line Gap Analysis ---

@app.get("/odds/line-gaps/{sport}")
async def line_gaps(sport: str, event_id: str = "", market: str = "alternate_spreads"):
    """Scan alternate lines for gaps — missing points that reveal risk concentration."""
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.line_gaps import scan_line_gaps

    if event_id:
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            return {"error": alt_data["error"]}
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        return {"event_id": event_id, "market": market, "gap_count": len(gaps), "gaps": gaps}

    # No event_id — scan first 5 games
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

    all_gaps = []
    for game in odds_data.get("games", [])[:5]:
        eid = game.get("id", "")
        if not eid:
            continue
        alt_data = await _get_alt(sport=sport, event_id=eid)
        if alt_data.get("error"):
            continue
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
            g["event_id"] = eid
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "market": market,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "exploitable": len([g for g in all_gaps if g.get("exploitable")]),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }


@app.get("/odds/prop-gaps/{sport}")
async def prop_gaps(sport: str, event_id: str = ""):
    """Scan player props for line gaps across bookmakers."""
    from tools.odds_api import get_odds as _get_odds, get_player_props as _get_props
    from tools.line_gaps import scan_prop_gaps

    if event_id:
        prop_data = await _get_props(sport=sport, event_id=event_id)
        if prop_data.get("error"):
            return {"error": prop_data["error"]}
        gaps = scan_prop_gaps(prop_data)
        return {"event_id": event_id, "gap_count": len(gaps), "gaps": gaps}

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

    all_gaps = []
    for game in odds_data.get("games", [])[:3]:
        eid = game.get("id", "")
        if not eid:
            continue
        prop_data = await _get_props(sport=sport, event_id=eid)
        if prop_data.get("error"):
            continue
        gaps = scan_prop_gaps(prop_data)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "games_scanned": min(3, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }


# --- Profit Boost Evaluator ---

class FixedBoostRequest(BaseModel):
    boosted_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class PctBoostRequest(BaseModel):
    boost_pct: float
    base_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class FreeBetRequest(BaseModel):
    free_bet_amount: float
    bet_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    stake_returned: bool = False
    description: str = ""
    book: str = ""


class HedgeRequest(BaseModel):
    boost_stake: float
    boosted_odds: int
    hedge_odds: int
    fair_probability: float


class DevigRequest(BaseModel):
    odds_a: int
    odds_b: int


@app.post("/boosts/evaluate-fixed")
async def eval_fixed_boost(req: FixedBoostRequest):
    """Evaluate a fixed profit boost — devig, compare to fair, calculate edge."""
    from tools.boost_evaluator import evaluate_fixed_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_fixed_boost(
        boosted_odds=req.boosted_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/evaluate-percentage")
async def eval_pct_boost(req: PctBoostRequest):
    """Evaluate a percentage profit boost token."""
    from tools.boost_evaluator import evaluate_percentage_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_percentage_boost(
        boost_pct=req.boost_pct,
        base_odds=req.base_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/evaluate-free-bet")
async def eval_free_bet(req: FreeBetRequest):
    """Evaluate a free bet or no-sweat bet."""
    from tools.boost_evaluator import evaluate_free_bet, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_free_bet(
        free_bet_amount=req.free_bet_amount,
        bet_odds=req.bet_odds,
        fair_probability=fair_prob,
        stake_returned=req.stake_returned,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/hedge")
async def hedge_calc(req: HedgeRequest):
    """Calculate optimal hedge for guaranteed profit."""
    from tools.boost_evaluator import calculate_hedge

    return calculate_hedge(
        boost_stake=req.boost_stake,
        boosted_odds=req.boosted_odds,
        hedge_odds=req.hedge_odds,
        fair_probability=req.fair_probability,
    )


@app.post("/boosts/devig")
async def devig(req: DevigRequest):
    """Devig a two-way market using multiplicative method."""
    from tools.boost_evaluator import devig_multiplicative, devig_additive

    mult_a, mult_b = devig_multiplicative(req.odds_a, req.odds_b)
    add_a, add_b = devig_additive(req.odds_a, req.odds_b)

    return {
        "multiplicative": {"side_a": mult_a, "side_b": mult_b},
        "additive": {"side_a": add_a, "side_b": add_b},
        "recommended": "multiplicative",
    }


# --- Hypothesis Testing & Backtesting ---

class HypothesisCreate(BaseModel):
    name: str
    thesis: str
    sport: str
    market_type: str
    hypothesis_model_config: dict
    edge_threshold: float = 0.02
    min_sample_size: int = 1000
    significance_level: float = 0.05
    notes: str = ""


class BacktestRequest(BaseModel):
    hypothesis_id: str
    start_date: str
    end_date: str
    credit_budget: int = 50


@app.post("/hypothesis")
async def create_hypothesis(req: HypothesisCreate):
    """Create a new testable betting hypothesis."""
    hid = await hypothesis_manager.create_hypothesis(
        name=req.name,
        thesis=req.thesis,
        sport=req.sport,
        market_type=req.market_type,
        model_config=req.hypothesis_model_config,
        edge_threshold=req.edge_threshold,
        min_sample_size=req.min_sample_size,
        significance_level=req.significance_level,
        notes=req.notes,
    )
    return {"hypothesis_id": hid}


@app.get("/hypothesis")
async def list_hypotheses(status: Optional[str] = None):
    """List all hypotheses, optionally filtered by status."""
    hypotheses = await hypothesis_manager.list_hypotheses(status=status)
    return {"count": len(hypotheses), "hypotheses": hypotheses}


@app.get("/hypothesis/{hypothesis_id}")
async def get_hypothesis(hypothesis_id: str):
    """Get hypothesis details."""
    h = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    return h


@app.get("/hypothesis/{hypothesis_id}/report")
async def hypothesis_report(hypothesis_id: str):
    """Full statistical report across all stages."""
    return await hypothesis_manager.get_hypothesis_report(hypothesis_id)


@app.get("/hypothesis/{hypothesis_id}/significance")
async def hypothesis_significance(hypothesis_id: str, stage: str = "backtest"):
    """Run significance tests on a hypothesis at a given stage."""
    return await hypothesis_manager.evaluate_significance(hypothesis_id, stage)


@app.post("/hypothesis/{hypothesis_id}/promote")
async def promote_hypothesis(hypothesis_id: str):
    """Check readiness and promote to next stage if criteria are met."""
    readiness = await hypothesis_manager.check_promotion_readiness(hypothesis_id)
    if readiness.get("ready"):
        result = await hypothesis_manager.auto_promote(hypothesis_id)
        return {"promoted": True, **result}
    return {"promoted": False, **readiness}


@app.post("/backtest/run")
async def run_backtest(req: BacktestRequest):
    """Start a backtest run on a hypothesis against historical data."""
    return await backtest_engine.run_backtest(
        hypothesis_id=req.hypothesis_id,
        start_date=req.start_date,
        end_date=req.end_date,
        credit_budget=req.credit_budget,
    )


@app.get("/backtest/run/{run_id}")
async def get_backtest_results(run_id: str):
    """Get backtest results for a run."""
    return await backtest_engine.get_run_results(run_id)


@app.post("/backtest/resolve/{run_id}")
async def resolve_backtest(run_id: str, sport: str = "basketball_nba"):
    """Resolve backtest events against actual game results."""
    return await backtest_engine.resolve_with_scores(run_id, sport)


@app.get("/historical/cache")
async def historical_cache_stats():
    """Get historical odds cache statistics."""
    return await historical_fetcher.get_cache_stats()


@app.post("/historical/fetch")
async def fetch_historical(
    sport: str,
    start_date: str,
    end_date: str,
    credit_budget: int = 50,
):
    """Fetch historical odds for a date range (cached after first fetch)."""
    return await historical_fetcher.bulk_fetch_date_range(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        credit_budget=credit_budget,
    )


# ── Research Loop Endpoints ──

@app.get("/research/status")
async def research_status():
    """Get research loop status."""
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return research_loop.get_status()


@app.post("/research/collect")
async def research_collect(sport: str = "basketball_nba", date: Optional[str] = None):
    """Manually trigger data collection for a sport."""
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    scores = await data_collector.collect_scores(sport, date)
    box = await data_collector.collect_box_scores(sport, date)
    return {"scores": scores, "box_scores": box}


@app.post("/research/generate")
async def research_generate(sport: str = "basketball_nba", max_hypotheses: int = 20):
    """Manually trigger hypothesis generation."""
    if not hypothesis_generator:
        raise HTTPException(status_code=503, detail="Hypothesis generator not initialized")
    created = await hypothesis_generator.generate_from_templates(
        sport=sport, max_hypotheses=max_hypotheses,
    )
    return {"generated": len(created), "hypotheses": created}


@app.get("/embeddings/stats")
async def embedding_stats(collection: Optional[str] = None):
    """Get embedding store statistics."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.get_collection_stats(collection)


@app.post("/embeddings/search")
async def embedding_search(
    collection: str,
    query: str,
    top_k: int = 10,
):
    """Search embeddings by text similarity."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.search_text(collection, query, top_k)


@app.get("/data/stats")
async def data_collection_stats():
    """Get data collection statistics."""
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    return await data_collector.get_collection_stats()


@app.get("/health")
async def health_check():
    """
    Comprehensive health check — Layer 2.
    Returns all subsystem statuses, circuit breaker states, error rates,
    and pipeline integrity (is the system producing expected output).
    The sentinel (Layer 3) polls this to detect problems.

    A system with broken pipelines should NOT report "ok" — the pipeline
    integrity checker downgrades the healthy flag if critical issues exist.
    """
    if not system_health:
        return {"healthy": False, "error": "Health monitor not initialized"}
    report = system_health.get_full_report()

    # Pipeline integrity — use cached results from the last run (fast)
    try:
        checker = get_integrity_checker()
        integrity = checker.get_latest_report()
        report["pipeline_integrity"] = integrity
        # Degrade overall healthy flag if pipeline has critical issues
        if not integrity.get("healthy", True):
            report["healthy"] = False
            report["pipeline_broken"] = True
    except Exception as e:
        logger.error(f"Pipeline integrity report failed: {e}", exc_info=True)
        report["pipeline_integrity"] = {
            "status": "error",
            "error": f"integrity check failed: {e}",
        }

    # Write health file for sentinel to read if HTTP is down
    system_health.write_health_file()
    return report


@app.get("/health/deep")
async def health_deep():
    """
    Full pipeline integrity suite — runs ALL checks on demand.
    Slower than /health (queries multiple tables). Use this for
    debugging pipeline issues, not for polling.

    Returns: complete integrity check results + subsystem health.
    """
    try:
        checker = get_integrity_checker()
        result = await checker.run_all_checks()
    except Exception as e:
        logger.error(f"Deep health check failed: {e}", exc_info=True)
        result = {"error": f"deep check failed: {e}"}

    # Include Layer 2 subsystem status for complete picture
    if system_health:
        result["subsystems"] = system_health.get_full_report()

    return result


@app.get("/health/integrity/history")
async def integrity_history(limit: int = 50):
    """Get recent pipeline integrity check history."""
    try:
        checker = get_integrity_checker()
        history = await checker.get_history(limit=limit)
        return {"count": len(history), "checks": history}
    except Exception as e:
        logger.error(f"Integrity history fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/claude/status")
async def claude_status():
    """Get Claude Code availability and usage stats."""
    from tools.claude_code import get_usage_stats
    return get_usage_stats()


@app.get("/system/full-status")
async def full_system_status():
    """
    Single endpoint for checking everything from your phone.
    Returns all subsystem statuses in one call.
    Pipeline integrity is front-and-center so DEGRADED/BROKEN status
    is immediately visible in every Claude Code session start.
    """
    from tools.claude_code import get_usage_stats as claude_stats

    status = {
        "timestamp": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    }

    # Pipeline integrity first — this is the most important signal
    try:
        checker = get_integrity_checker()
        integrity = checker.get_latest_report()
        status["pipeline_integrity"] = integrity
    except Exception as e:
        logger.error(f"Pipeline integrity report failed in full-status: {e}", exc_info=True)
        status["pipeline_integrity"] = {
            "status": "error",
            "error": f"integrity check failed: {e}",
        }

    status["autonomous_loop"] = autonomous.get_status() if autonomous else None
    status["research_loop"] = research_loop.get_status() if research_loop else None
    status["claude_code"] = claude_stats()
    status["line_monitor"] = line_monitor.get_status() if line_monitor else None

    # Add hypothesis summary
    if hypothesis_manager:
        try:
            all_h = await hypothesis_manager.list_hypotheses()
            status["hypotheses"] = {
                "total": len(all_h),
                "draft": sum(1 for h in all_h if h["status"] == "draft"),
                "backtesting": sum(1 for h in all_h if h["status"] == "backtesting"),
                "paper_trading": sum(1 for h in all_h if h["status"] == "paper_trading"),
                "live": sum(1 for h in all_h if h["status"] == "live"),
                "rejected": sum(1 for h in all_h if h["status"] == "rejected"),
                "retired": sum(1 for h in all_h if h["status"] == "retired"),
            }
        except Exception as e:
            logger.warning(f"Failed to get hypothesis summary for full-status: {e}")

    # Add embedding stats
    if vector_store:
        try:
            status["embeddings"] = await vector_store.get_collection_stats()
        except Exception as e:
            logger.warning(f"Failed to get embedding stats for full-status: {e}")

    # Add data collection stats
    if data_collector:
        try:
            status["data"] = await data_collector.get_collection_stats()
        except Exception as e:
            logger.warning(f"Failed to get data collection stats for full-status: {e}")

    # Layer 2 health subsystems
    if system_health:
        try:
            health_report = system_health.get_full_report()
            status["system_health"] = {
                "healthy": health_report.get("healthy"),
                "uptime_hours": health_report.get("uptime_hours"),
                "stalled_phases": health_report.get("stalled_phases", []),
            }
        except Exception as e:
            logger.warning(f"Failed to get system health for full-status: {e}")

    return status


# ---------------------------------------------------------------------------
# Task listing & context sync
# ---------------------------------------------------------------------------

@app.get("/tasks")
async def list_tasks(status: Optional[str] = None, limit: int = 10):
    """List recent tasks from the queue."""
    rows = await queue._db.execute_fetchall(
        """SELECT task_id, query, status, priority, session_id,
                  created_at, started_at, completed_at
           FROM task_queue
           ORDER BY created_at DESC LIMIT ?""",
        (limit,)
    )
    columns = ["task_id", "query", "status", "priority", "session_id",
               "created_at", "started_at", "completed_at"]
    tasks = [dict(zip(columns, row)) for row in rows]
    if status:
        tasks = [t for t in tasks if t["status"] == status.upper()]
    return {"count": len(tasks), "tasks": tasks}


class ContextSync(BaseModel):
    session_summary: str
    actionable_queries: list[str] = []

@app.post("/context/sync")
async def sync_context(ctx: ContextSync):
    """Receive context from a Claude Code session. Queues actionable items."""
    submitted = []
    for q in ctx.actionable_queries:
        task_id = await queue.submit_task(q, priority=1)
        submitted.append(task_id)
    return {
        "received": True,
        "tasks_submitted": len(submitted),
        "task_ids": submitted,
    }


@app.post("/admin/restart")
async def admin_restart():
    """Graceful restart — exits process, watchdog brings it back with new code.

    This is how Claude Code (or any session) triggers a code reload
    without Marco needing to be physically present. The watchdog.bat
    restarts within 15 seconds.
    """
    logger.info("RESTART REQUESTED via /admin/restart — shutting down gracefully")
    send_msg = "Callisto restarting (code reload requested)"
    try:
        await telegram.send_message(send_msg)
    except Exception as e:
        logger.info(f"Telegram restart notification failed (non-critical): {e}")

    # Give time for this response to be sent, then exit
    async def _delayed_exit():
        await asyncio.sleep(1)
        logger.info("Exiting for restart...")
        os._exit(0)

    asyncio.create_task(_delayed_exit())
    return {"status": "restarting", "message": "Watchdog will restart with new code in ~15 seconds"}


# ---------------------------------------------------------------------------
# Bet executor endpoints
# ---------------------------------------------------------------------------
_executor = None


async def _get_executor():
    global _executor
    if _executor is None:
        from tools.bet_executor import BetExecutor
        _executor = BetExecutor()
        await _executor.initialize()
    return _executor


@app.get("/executor/status")
async def executor_status():
    """Get bet executor status."""
    ex = await _get_executor()
    return await ex.status()


@app.post("/executor/enable")
async def executor_enable():
    """Enable the bet executor — live bets will be placed."""
    ex = await _get_executor()
    ex.enable()
    # Wire into research loop if available
    if hasattr(app.state, "research_loop"):
        app.state.research_loop._bet_executor = ex
    return {"status": "enabled", "message": "Bet executor is now LIVE — bets will be placed automatically"}


@app.post("/executor/disable")
async def executor_disable():
    """Disable the bet executor — no bets will be placed."""
    ex = await _get_executor()
    ex.disable()
    return {"status": "disabled", "message": "Bet executor disabled — no bets will be placed"}


@app.post("/executor/login")
async def executor_login():
    """Launch browser for DraftKings login. Browser opens visible for manual login."""
    ex = await _get_executor()
    logged_in = await ex.ensure_logged_in()
    if logged_in:
        return {"status": "logged_in", "message": "DraftKings session active"}
    else:
        return {
            "status": "login_required",
            "message": "Browser opened — please log into DraftKings manually. Session will persist.",
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=CALLISTO_PORT, reload=False)
