"""
Telegram notification system for Callisto.

Sends alerts directly to Marco's phone for:
- High-confidence edges (CORROBORATED+)
- Sharp money movements
- Line monitor alerts (significant moves)
- Bet resolutions (win/loss/CLV results)
- System status (startup, errors)

Uses httpx (already a dependency) — no extra packages needed.
"""

import asyncio
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.telegram")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Rate limiting — don't spam
_last_sent: dict[str, float] = {}  # key -> timestamp
MIN_INTERVAL_SECONDS = 900  # Same edge throttled to 15 min minimum


async def send_alert(
    message: str,
    silent: bool = False,
    parse_mode: str = "HTML",
    throttle_key: Optional[str] = None,
) -> bool:
    """
    Send an alert to Telegram.

    Args:
        message: The message text (supports HTML formatting).
        silent: If True, delivers without notification sound.
        parse_mode: "HTML" or "MarkdownV2".
        throttle_key: Optional key for rate limiting. Same key won't send
                     more than once per MIN_INTERVAL_SECONDS.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False

    # Throttle check
    if throttle_key:
        import time
        now = time.time()
        last = _last_sent.get(throttle_key, 0)
        if now - last < MIN_INTERVAL_SECONDS:
            logger.debug(f"Throttled alert: {throttle_key}")
            return False
        _last_sent[throttle_key] = now

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{API_BASE}/sendMessage",
                json={
                    "chat_id": CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_notification": silent,
                },
            )
            if resp.status_code == 200:
                logger.info(f"Telegram alert sent ({len(message)} chars)")
                return True
            else:
                logger.error(f"Telegram API error: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# --- Formatted alert builders ---

async def alert_edge(
    game: str,
    team: str,
    market: str,
    edge_pct: float,
    confidence_tier: str,
    confidence_score: float,
    best_book: str,
    best_price: int,
    reasoning: str = "",
) -> bool:
    """Send edge detection alert."""
    price_str = f"+{best_price}" if best_price > 0 else str(best_price)
    msg = (
        f"<b>Edge Detected</b>\n"
        f"\n"
        f"<b>{game}</b>\n"
        f"{team} — {market}\n"
        f"\n"
        f"Edge: <b>{edge_pct:.1f}%</b>\n"
        f"Confidence: <b>{confidence_tier}</b> ({confidence_score:.0%})\n"
        f"Best line: {best_book} {price_str}\n"
    )
    if reasoning:
        msg += f"\n<i>{reasoning}</i>"

    return await send_alert(msg, throttle_key=f"edge:{game}:{team}:{market}")


async def alert_sharp_move(
    game: str,
    team: str,
    market: str,
    moved_books: list[dict],
    stale_books: list[dict],
) -> bool:
    """Send sharp money movement alert."""
    movers = ", ".join(
        f"{m['bookmaker']} ({m['old_price']}→{m['new_price']})"
        for m in moved_books
    )
    stale = ", ".join(
        f"{s['bookmaker']} {s['price']}"
        for s in stale_books[:3]
    )
    msg = (
        f"<b>Sharp Money Alert</b>\n"
        f"\n"
        f"<b>{game}</b>\n"
        f"{team} — {market}\n"
        f"\n"
        f"Moved: {movers}\n"
        f"Stale: {stale}\n"
        f"\n"
        f"<i>Stale books may offer value before adjusting.</i>"
    )
    return await send_alert(msg, throttle_key=f"sharp:{game}:{team}:{market}")


async def alert_bet_result(
    bet_id: int,
    game: str,
    team: str,
    result: str,
    placement_odds: int,
    stake: float,
    payout: Optional[float] = None,
    clv_implied: Optional[float] = None,
) -> bool:
    """Send bet resolution alert."""
    icon = {"won": "W", "lost": "L", "push": "P"}.get(result, "?")
    price_str = f"+{placement_odds}" if placement_odds > 0 else str(placement_odds)

    msg = (
        f"<b>Bet #{bet_id} — {icon} {result.upper()}</b>\n"
        f"\n"
        f"{game}\n"
        f"{team} @ {price_str}\n"
        f"Stake: ${stake:.2f}"
    )
    if result == "won" and payout:
        msg += f" → <b>${payout:.2f}</b> (+${payout - stake:.2f})"
    elif result == "lost":
        msg += f" → <b>-${stake:.2f}</b>"

    if clv_implied is not None:
        direction = "+" if clv_implied > 0 else ""
        msg += f"\nCLV: {direction}{clv_implied:.1%}"

    return await send_alert(msg)


async def alert_prop_edges(edges: list[dict], sport: str = "") -> bool:
    """Send summary of prop edges found."""
    if not edges:
        return False

    header = f"<b>Prop Edges Found</b>"
    if sport:
        header += f" — {sport}"
    header += f"\n\n"

    lines = []
    for e in edges[:5]:  # Top 5
        player = e.get("player", "?")
        market = e.get("market", "?")
        side = e.get("side", "?")
        edge = e.get("edge_pct", 0)
        book = e.get("target_book", "?")
        price = e.get("target_price", 0)
        price_str = f"+{price}" if price > 0 else str(price)
        conf = e.get("confidence", {})
        tier = conf.get("tier", "?") if isinstance(conf, dict) else "?"

        lines.append(
            f"<b>{player}</b> {market} {side}\n"
            f"  Edge: {edge:.1f}% | {book} {price_str} | {tier}"
        )

    msg = header + "\n\n".join(lines)
    if len(edges) > 5:
        msg += f"\n\n<i>+{len(edges) - 5} more edges</i>"

    return await send_alert(msg, throttle_key=f"props:{sport}")


async def alert_system(message: str, is_error: bool = False) -> bool:
    """Send system status alert."""
    prefix = "<b>System Error</b>" if is_error else "<b>Callisto</b>"
    return await send_alert(f"{prefix}\n\n{message}", silent=not is_error)


# --- Telegram listener for bidirectional communication ---

class TelegramListener:
    """
    Polls for incoming Telegram messages and routes them to the orchestrator.

    Supports:
      /status  — system health check
      /bets    — show open bets
      /edges   — latest edge summary
      /bankroll — bankroll and P/L
      Any text — runs a full AGP session and returns the conclusion
    """

    def __init__(self, orchestrator=None, line_monitor=None, clv_tracker=None):
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor
        self.clv_tracker = clv_tracker
        self._running = False
        self._task = None
        self._last_update_id = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram listener started — accepting commands")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Telegram listener stopped")

    async def _poll_loop(self) -> None:
        """Long-poll Telegram for incoming messages."""
        while self._running:
            try:
                async with httpx.AsyncClient(timeout=35) as client:
                    resp = await client.get(
                        f"{API_BASE}/getUpdates",
                        params={
                            "offset": self._last_update_id + 1,
                            "timeout": 30,
                            "allowed_updates": '["message"]',
                        },
                    )
                    if resp.status_code != 200:
                        await asyncio.sleep(5)
                        continue

                    data = resp.json()
                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"]
                        msg = update.get("message", {})
                        chat_id = str(msg.get("chat", {}).get("id", ""))

                        # Only respond to Marco
                        if chat_id != CHAT_ID:
                            continue

                        text = msg.get("text", "").strip()
                        if text:
                            await self._handle_message(text)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(10)

    async def _handle_message(self, text: str) -> None:
        """Route an incoming message to the appropriate handler."""
        logger.info(f"Telegram received: {text}")
        cmd = text.lower().strip()

        # Commands that don't need the GPU — respond instantly
        INSTANT_COMMANDS = {
            "/start": self._cmd_help,
            "/status": self._cmd_status,
            "/bets": self._cmd_bets,
            "/edges": self._cmd_edges,
            "/bankroll": self._cmd_bankroll,
            "/help": self._cmd_help,
        }

        try:
            handler = INSTANT_COMMANDS.get(cmd)
            if handler:
                await handler()
            elif cmd in ("/research", "/progress"):
                await self._cmd_research_status()
            elif cmd in ("/best", "/top"):
                await self._cmd_best_edge()
            else:
                # EVERYTHING else → Claude Code (fast ~30s, smart)
                asyncio.create_task(self._cmd_smart_query(text))
        except Exception as e:
            logger.error(f"Telegram command error: {e}")
            await send_alert(f"Error: {str(e)[:200]}", parse_mode="")

    async def _cmd_smart_query(self, text: str) -> None:
        """
        Answer Telegram queries using live system data.

        Instead of spawning a stateless Claude subprocess (which has
        no Callisto context and times out), pull real data from the
        API and compose a direct answer.
        """
        try:
            import httpx as _hx

            # Gather all system data
            status_data = {}
            try:
                async with _hx.AsyncClient(timeout=5) as c:
                    status_data = (await c.get("http://localhost:8420/system/full-status")).json()
            except Exception:
                pass

            rl = status_data.get("research_loop", {})
            hy = status_data.get("hypotheses", {})
            cl = status_data.get("claude_code", {})
            lm = status_data.get("line_monitor", {})

            # Build a rich status response
            parts = [f"You asked: \"{text[:100]}\"\n"]

            parts.append(
                f"System: Running | "
                f"{rl.get('cycles_completed', 0)} research cycles | "
                f"{cl.get('total_successful', 0)} Claude calls"
            )
            parts.append(
                f"Hypotheses: {hy.get('total', 0)} total | "
                f"{hy.get('draft', 0)} draft | "
                f"{hy.get('backtesting', 0)} testing | "
                f"{hy.get('paper_trading', 0)} paper | "
                f"{hy.get('live', 0)} live"
            )
            parts.append(f"Backtests: {rl.get('backtests_run', 0)} completed")

            credits = lm.get("credits", {})
            parts.append(f"Odds API: {credits.get('remaining', '?')} credits left")

            # Add edge data if available
            if self.line_monitor:
                reports = self.line_monitor.get_edge_report()
                if isinstance(reports, dict):
                    for sport, report in reports.items():
                        if isinstance(report, dict):
                            total = report.get("total_edges", 0)
                            if total > 0:
                                parts.append(f"{sport}: {total} edges detected")

            parts.append(
                "\nUse /status /edges /research /best for specific data. "
                "Full analysis queries run through local models (~60s)."
            )

            await send_alert("\n".join(parts), parse_mode="")

        except Exception as e:
            logger.error(f"Smart query failed: {e}")
            await send_alert(f"System is running but query failed: {str(e)[:200]}", parse_mode="")

    async def _cmd_query_safe(self, text: str, timeout: int = 120) -> None:
        """Fallback: run query through local AGP orchestrator."""
        try:
            await self._cmd_query(text, timeout=timeout)
        except Exception as e:
            logger.error(f"Telegram query failed: {e}")
            await send_alert(f"Analysis failed: {str(e)[:200]}", parse_mode="")

    async def _cmd_help(self) -> None:
        await send_alert(
            "<b>Callisto Commands</b>\n\n"
            "/status — System health\n"
            "/bets — Open bets\n"
            "/edges — Latest edges\n"
            "/bankroll — Balance and P/L\n"
            "/help — This message\n\n"
            "Or type any question to run a full analysis.",
        )

    async def _cmd_status(self) -> None:
        parts = ["<b>System Status</b>\n"]

        if self.line_monitor:
            st = self.line_monitor.get_status()
            parts.append(f"Monitor: {'ON' if st['running'] else 'OFF'}")
            parts.append(f"Sports: {', '.join(st.get('monitored_sports', []))}")
            credits = st.get("credits", {})
            parts.append(f"API credits: {credits.get('remaining', '?')} remaining")

        # Edge reports
        if self.line_monitor:
            reports = self.line_monitor.get_edge_report()
            if isinstance(reports, dict):
                for sport, report in reports.items():
                    if isinstance(report, dict):
                        total = report.get("total_edges", 0)
                        parts.append(f"{sport}: {total} edges")

        await send_alert("\n".join(parts))

    async def _cmd_bets(self) -> None:
        if not self.clv_tracker:
            await send_alert("CLV tracker not available.")
            return

        bets = await self.clv_tracker.get_all_bets(limit=10)
        if not bets:
            await send_alert("No bets recorded.")
            return

        lines = ["<b>Recent Bets</b>\n"]
        for b in bets:
            odds = b.get("placement_odds", 0)
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            status = b.get("result", "pending").upper()
            lines.append(
                f"#{b['id']} {status}: {b.get('team', '?')} "
                f"{b.get('market', '')} {odds_str} "
                f"(${b.get('stake', 0):.0f} @ {b.get('bookmaker', '?')})"
            )
        await send_alert("\n".join(lines))

    async def _cmd_edges(self) -> None:
        if not self.line_monitor:
            await send_alert("Line monitor not available.")
            return

        reports = self.line_monitor.get_edge_report()
        if not isinstance(reports, dict) or not reports:
            await send_alert("No edge data yet.")
            return

        lines = ["<b>Latest Edges</b>\n"]
        for sport, report in reports.items():
            if not isinstance(report, dict):
                continue
            total = report.get("total_edges", 0)
            lines.append(f"\n<b>{sport}</b>: {total} edges")

            # Top cross-book edges
            for mkey in ["cross_book_h2h", "cross_book_spreads"]:
                for edge in report.get(mkey, [])[:2]:
                    team = edge.get("team", "?")
                    implied = edge.get("implied_range", 0)
                    soft = edge.get("soft_book_edges", [])
                    best_se = max(soft, key=lambda s: s.get("edge_vs_sharp", 0)) if soft else {}
                    se_pct = best_se.get("edge_vs_sharp", 0) * 100
                    se_book = best_se.get("bookmaker", "?")
                    lines.append(
                        f"  {team} ({mkey.split('_')[-1]}): "
                        f"{implied:.1%} range, best {se_pct:.1f}% @ {se_book}"
                    )

        await send_alert("\n".join(lines))

    async def _cmd_research_status(self) -> None:
        """Quick research loop status — no GPU needed."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://localhost:8420/system/full-status")
                d = r.json()
                rl = d.get("research_loop", {})
                cl = d.get("claude_code", {})
                h = d.get("hypotheses", {})

                msg = (
                    f"<b>Research Status</b>\n\n"
                    f"Cycles: {rl.get('cycles_completed', 0)}\n"
                    f"Hypotheses: {h.get('total', 0)} total\n"
                    f"  Draft: {h.get('draft', 0)} | Testing: {h.get('backtesting', 0)}\n"
                    f"  Paper: {h.get('paper_trading', 0)} | Live: {h.get('live', 0)}\n"
                    f"Backtests: {rl.get('backtests_run', 0)}\n"
                    f"Claude calls: {cl.get('total_successful', 0)} successful\n"
                    f"Promotions: {rl.get('promotions', 0)}\n"
                )
                await send_alert(msg)
        except Exception as e:
            await send_alert(f"Research status unavailable: {str(e)[:100]}")

    async def _cmd_bankroll(self) -> None:
        if not self.clv_tracker:
            await send_alert("CLV tracker not available.")
            return

        history = await self.clv_tracker.get_bankroll_history(limit=1)
        balance = history[0]["balance"] if history else "unknown"

        bets = await self.clv_tracker.get_all_bets(limit=100)
        won = sum(1 for b in bets if b.get("result") == "won")
        lost = sum(1 for b in bets if b.get("result") == "lost")
        pending = sum(1 for b in bets if b.get("result") == "pending")

        msg = (
            f"<b>Bankroll</b>\n\n"
            f"Balance: <b>${balance}</b>\n"
            f"Record: {won}W-{lost}L ({pending} pending)\n"
            f"Total bets: {len(bets)}"
        )
        await send_alert(msg)

    async def _cmd_best_edge(self) -> None:
        """Return the best current edge from cached data — no GPU needed."""
        if not self.line_monitor:
            await send_alert("Line monitor not available.")
            return

        reports = self.line_monitor.get_edge_report()
        if not isinstance(reports, dict) or not reports:
            await send_alert("No edge data yet. Wait for next snapshot cycle.")
            return

        # Find the single best soft book edge across all sports
        best = None
        best_pct = 0

        for sport, report in reports.items():
            if not isinstance(report, dict):
                continue
            for mkey in ["cross_book_h2h", "cross_book_spreads", "cross_book_totals"]:
                for edge in report.get(mkey, []):
                    for se in edge.get("soft_book_edges", []):
                        pct = se.get("edge_vs_sharp", 0) * 100
                        if pct > best_pct:
                            best_pct = pct
                            best = {
                                "sport": sport,
                                "game": edge.get("game", "?"),
                                "team": edge.get("team", "?"),
                                "market": mkey.replace("cross_book_", ""),
                                "edge_pct": pct,
                                "bookmaker": se.get("bookmaker", "?"),
                                "price": se.get("price", 0),
                                "ev": se.get("ev", {}),
                                "sharp_consensus": edge.get("sharp_consensus"),
                                "num_books": edge.get("num_bookmakers", 0),
                                "implied_range": edge.get("implied_range", 0),
                            }

        if not best or best_pct < 1.0:
            await send_alert("No actionable edges right now. Markets are tight.")
            return

        price = best["price"]
        price_str = f"+{price}" if price > 0 else str(price)
        ev_data = best.get("ev", {})
        ev_val = ev_data.get("expected_value", 0) if isinstance(ev_data, dict) else 0
        kelly = ev_data.get("kelly_fraction", 0) if isinstance(ev_data, dict) else 0

        msg = (
            f"<b>Best Edge Right Now</b>\n\n"
            f"<b>{best['game']}</b>\n"
            f"{best['team']} — {best['market']}\n\n"
            f"Edge: <b>{best_pct:.1f}%</b> vs sharp consensus\n"
            f"Line: {best['bookmaker']} {price_str}\n"
            f"Books compared: {best['num_books']}\n"
            f"Cross-book range: {best['implied_range']:.1%}\n"
        )
        if ev_val:
            msg += f"EV: ${ev_val:.2f} per $100\n"
        if kelly:
            msg += f"Kelly: {kelly:.1%}\n"
        if best.get("sharp_consensus"):
            msg += f"Sharp consensus: {best['sharp_consensus']:.1%}\n"

        # Check if it's on DK or Fanatics
        bm = best["bookmaker"].lower()
        if "draftkings" in bm or "fanatics" in bm:
            msg += f"\n<b>Available on your book.</b>"
        else:
            msg += f"\n<i>Not on DK/Fanatics — check if available.</i>"

        await send_alert(msg)

    async def _cmd_query(self, text: str, timeout: int = 120) -> None:
        """Run a free-text query through the AGP orchestrator."""
        if not self.orchestrator:
            await send_alert("Orchestrator not available.")
            return

        await send_alert("Analyzing with local models...", silent=True, parse_mode="")

        try:
            result = await asyncio.wait_for(
                self.orchestrator.run_session(text),
                timeout=timeout,
            )
            summary = result.get("summary", {})
            conclusion = summary.get("conclusion", "No conclusion")
            confidence = summary.get("confidence_score", 0)
            tier = summary.get("confidence_tier", "UNVERIFIED")

            # Truncate for Telegram's 4096 char limit
            if len(conclusion) > 3500:
                conclusion = conclusion[:3500] + "..."

            msg = (
                f"<b>{tier}</b> ({confidence:.0%})\n\n"
                f"{conclusion}"
            )
            await send_alert(msg, parse_mode="HTML")

        except asyncio.TimeoutError:
            await send_alert(f"Analysis timed out ({timeout}s limit).", parse_mode="")
        except Exception as e:
            await send_alert(f"Analysis failed: {str(e)[:200]}", parse_mode="")
