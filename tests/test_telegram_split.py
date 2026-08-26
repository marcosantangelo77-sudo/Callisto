"""
Tests for the tools/tg split of the Telegram subsystem.

Covers:
  - facade compatibility (tools.telegram re-exports everything)
  - config module values
  - send_alert behaviour with a mocked httpx transport (no real messages)
  - throttle logic
  - alert builders' message formatting
  - TelegramListener command routing / handlers with fake dependencies

No test here sends a real Telegram message: BOT_TOKEN/CHAT_ID are forced to
test fixtures and all HTTP traffic goes through mocked transports or fakes.
"""

import asyncio
import json
import time

import httpx
import pytest

import tools.telegram as telegram_facade
from tools.tg import (
    MIN_INTERVAL_SECONDS,
    TelegramListener,
    alert_bet_result,
    alert_edge,
    alert_prop_edges,
    alert_sharp_move,
    alert_system,
    reset_throttle_cache,
    send_alert,
)
from tools.tg import client as tg_client
from tools.tg import config as tg_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Never let any test hit the real Telegram API."""
    monkeypatch.setattr(tg_config, "BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setattr(tg_config, "CHAT_ID", "TEST_CHAT")
    monkeypatch.setattr(
        tg_client, "API_BASE", "https://api.telegram.org/botTEST_TOKEN"
    )
    monkeypatch.setattr(tg_client, "BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setattr(tg_client, "CHAT_ID", "TEST_CHAT")
    # Facade re-exports point at the same strings; keep them consistent too.
    monkeypatch.setattr(telegram_facade, "BOT_TOKEN", "TEST_TOKEN")
    monkeypatch.setattr(telegram_facade, "CHAT_ID", "TEST_CHAT")
    reset_throttle_cache()
    yield
    reset_throttle_cache()


def _mock_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test patching


# ---------------------------------------------------------------------------
# Facade compatibility
# ---------------------------------------------------------------------------

class TestFacadeCompatibility:
    def test_facade_reexports_send_alert(self):
        assert telegram_facade.send_alert is send_alert

    def test_facade_reexports_listener(self):
        assert telegram_facade.TelegramListener is TelegramListener

    @pytest.mark.parametrize(
        "name",
        [
            "alert_edge",
            "alert_sharp_move",
            "alert_bet_result",
            "alert_prop_edges",
            "alert_system",
        ],
    )
    def test_facade_reexports_alerts(self, name):
        assert hasattr(telegram_facade, name)

    def test_facade_reexports_constants(self):
        assert telegram_facade.MIN_INTERVAL_SECONDS == MIN_INTERVAL_SECONDS
        assert isinstance(telegram_facade.API_BASE, str)
        assert telegram_facade.API_BASE.startswith("https://api.telegram.org/bot")

    def test_facade_exposes_throttle_cache(self):
        assert isinstance(telegram_facade._last_sent, dict)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_api_base_embeds_token(self):
        # API_BASE is computed at import from the real env; token may be empty.
        assert tg_config.API_BASE.startswith("https://api.telegram.org/bot")

    def test_defaults_present(self):
        assert hasattr(tg_config, "BOT_TOKEN")
        assert hasattr(tg_config, "CHAT_ID")


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_first_send_allowed(self):
        assert tg_client._throttle("edge:a:b:c") is True

    def test_second_send_within_interval_blocked(self):
        assert tg_client._throttle("k1") is True
        assert tg_client._throttle("k1") is False

    def test_distinct_keys_independent(self):
        assert tg_client._throttle("k2") is True
        assert tg_client._throttle("k3") is True

    def test_old_timestamp_allows_send(self):
        tg_client._last_sent["old"] = time.time() - MIN_INTERVAL_SECONDS - 1
        assert tg_client._throttle("old") is True

    def test_cache_capped(self):
        tg_client._last_sent.clear()
        now = time.time()
        for i in range(tg_client._LAST_SENT_MAX + 50):
            tg_client._last_sent[f"k{i}"] = now - i  # older keys first
        tg_client._throttle("trigger")
        assert len(tg_client._last_sent) <= tg_client._LAST_SENT_MAX + 1

    def test_reset_clears(self):
        tg_client._throttle("reset-me")
        reset_throttle_cache()
        assert tg_client._last_sent == {}


# ---------------------------------------------------------------------------
# send_alert
# ---------------------------------------------------------------------------

class TestSendAlert:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    @staticmethod
    def _client_factory(handler):
        """Fresh client per call (send_alert opens one per send)."""
        return lambda *a, **k: _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler))

    def test_success_returns_true_and_posts_payload(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["payload"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))

        assert self._run(send_alert("<b>hi</b>", parse_mode="HTML")) is True
        assert "/sendMessage" in captured["url"]
        p = captured["payload"]
        assert p["text"] == "<b>hi</b>"
        assert p["parse_mode"] == "HTML"
        assert p["chat_id"] == "TEST_CHAT"
        assert p["disable_notification"] is False

    def test_silent_flag_passed(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["payload"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))
        assert self._run(send_alert("x", silent=True)) is True
        assert captured["payload"]["disable_notification"] is True

    def test_empty_parse_mode_omits_field(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["payload"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))
        assert self._run(send_alert("x", parse_mode="")) is True
        assert "parse_mode" not in captured["payload"]

    def test_api_error_returns_false(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error_code": 400})

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))
        assert self._run(send_alert("x")) is False

    def test_network_exception_returns_false(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))
        assert self._run(send_alert("x")) is False

    def test_unconfigured_skips_send(self, monkeypatch):
        monkeypatch.setattr(tg_client, "BOT_TOKEN", "")
        monkeypatch.setattr(tg_client, "CHAT_ID", "")
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200)

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))
        assert self._run(send_alert("x")) is False
        assert called == []

    def test_throttled_key_not_sent_twice(self, monkeypatch):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200)

        monkeypatch.setattr(httpx, "AsyncClient",
                            self._client_factory(handler))

        ok1, ok2 = self._run(asyncio.gather(
            send_alert("a", throttle_key="dup"),
            send_alert("b", throttle_key="dup"),
        ))
        # gather order preserved; throttle guarantees only one POST
        assert sorted([ok1, ok2], reverse=True) == [True, False]
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Alert builders (message formatting via captured payloads)
# ---------------------------------------------------------------------------

class PayloadCapture:
    def __init__(self):
        self.payloads = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def capture(monkeypatch):
    cap = PayloadCapture()

    async def fake_send(message, silent=False, parse_mode="HTML",
                        throttle_key=None):
        cap.payloads.append({
            "text": message,
            "silent": silent,
            "parse_mode": parse_mode,
            "throttle_key": throttle_key,
        })
        return True

    monkeypatch.setattr(tg_client, "send_alert", fake_send)
    # builders imported send_alert into their own namespace
    from tools.tg import alerts as alerts_mod
    monkeypatch.setattr(alerts_mod, "send_alert", fake_send)
    yield cap


class TestAlertEdge:
    def test_positive_price_gets_plus_sign(self, capture):
        asyncio.run(alert_edge(
            game="LAL vs BOS", team="LAL", market="h2h",
            edge_pct=4.2, confidence_tier="CORROBORATED",
            confidence_score=0.82, best_book="pinnacle", best_price=150,
        ))
        text = capture.payloads[0]["text"]
        assert "LAL vs BOS" in text
        assert "pinnacle +150" in text
        assert "4.2%" in text
        assert "CORROBORATED" in text
        assert capture.payloads[0]["throttle_key"] == "edge:LAL vs BOS:LAL:h2h"

    def test_negative_price_no_plus(self, capture):
        asyncio.run(alert_edge(
            game="G1", team="A", market="spreads", edge_pct=1.0,
            confidence_tier="UNVERIFIED", confidence_score=0.5,
            best_book="dk", best_price=-110,
        ))
        assert "-110" in capture.payloads[0]["text"]

    def test_reasoning_appended_italic(self, capture):
        asyncio.run(alert_edge(
            game="G", team="T", market="m", edge_pct=2.0,
            confidence_tier="X", confidence_score=0.6,
            best_book="b", best_price=100, reasoning="sharp consensus moved",
        ))
        assert "<i>sharp consensus moved</i>" in capture.payloads[0]["text"]

    def test_no_reasoning_no_italic_block(self, capture):
        asyncio.run(alert_edge(
            game="G", team="T", market="m", edge_pct=2.0,
            confidence_tier="X", confidence_score=0.6,
            best_book="b", best_price=100,
        ))
        assert "\n<i>" not in capture.payloads[0]["text"]


class TestAlertSharpMove:
    def test_lists_moved_and_stale_books(self, capture):
        asyncio.run(alert_sharp_move(
            game="LAL vs BOS", team="LAL", market="h2h",
            moved_books=[
                {"bookmaker": "fanduel", "old_price": 120, "new_price": 135},
                {"bookmaker": "mgm", "old_price": -105, "new_price": -115},
            ],
            stale_books=[
                {"bookmaker": "dk", "price": 140},
                {"bookmaker": "fanatics", "price": 142},
                {"bookmaker": "caesars", "price": 138},
                {"bookmaker": "pointsbet", "price": 145},
            ],
        ))
        text = capture.payloads[0]["text"]
        assert "Sharp Money Alert" in text
        assert "fanduel (120→135)" in text
        assert "mgm (-105→-115)" in text
        # stale books capped at 3
        assert "pointsbet" not in text
        assert "caesars" in text
        assert capture.payloads[0]["throttle_key"] == "sharp:LAL vs BOS:LAL:h2h"


class TestAlertBetResult:
    def test_won_with_payout_and_clv(self, capture):
        asyncio.run(alert_bet_result(
            bet_id=7, game="LAL vs BOS", team="LAL", result="won",
            placement_odds=150, stake=100.0, payout=250.0, clv_implied=0.021,
        ))
        text = capture.payloads[0]["text"]
        assert "Bet #7" in text
        assert "W WON" in text
        assert "$250.00" in text
        assert "+$150.00" in text
        assert "CLV: +2.1%" in text

    def test_lost_shows_negative_stake(self, capture):
        asyncio.run(alert_bet_result(
            bet_id=8, game="G", team="T", result="lost",
            placement_odds=-110, stake=50.0,
        ))
        text = capture.payloads[0]["text"]
        assert "L LOST" in text
        assert "-$50.00" in text
        assert "CLV" not in text

    def test_push_icon(self, capture):
        asyncio.run(alert_bet_result(
            bet_id=9, game="G", team="T", result="push",
            placement_odds=100, stake=25.0,
        ))
        text = capture.payloads[0]["text"]
        assert "P PUSH" in text

    def test_unknown_result_question_mark(self, capture):
        asyncio.run(alert_bet_result(
            bet_id=10, game="G", team="T", result="voided",
            placement_odds=100, stake=10.0,
        ))
        assert "? VOIDED" in capture.payloads[0]["text"]

    def test_negative_clv_has_no_double_plus(self, capture):
        asyncio.run(alert_bet_result(
            bet_id=11, game="G", team="T", result="won",
            placement_odds=120, stake=10.0, clv_implied=-0.03,
        ))
        assert "CLV: -3.0%" in capture.payloads[0]["text"]


class TestAlertPropEdges:
    def test_empty_edges_short_circuits_false(self, capture):
        assert asyncio.run(alert_prop_edges([])) is False
        assert capture.payloads == []

    def test_lists_top_five_and_overflow_count(self, capture):
        edges = [
            {
                "player": f"P{i}", "market": "pts", "side": "over",
                "edge_pct": 5 + i, "target_book": "dk",
                "target_price": -110, "confidence": {"tier": "B"},
            }
            for i in range(7)
        ]
        asyncio.run(alert_prop_edges(edges, sport="NBA"))
        text = capture.payloads[0]["text"]
        assert "Prop Edges Found" in text
        assert "— NBA" in text
        assert "+2 more edges" in text
        assert "P6" not in text  # only top 5 shown

    def test_non_dict_confidence_tolerated(self, capture):
        edges = [{
            "player": "P", "market": "pts", "side": "over",
            "edge_pct": 3.0, "target_book": "mgm",
            "target_price": 105, "confidence": "not-a-dict",
        }]
        asyncio.run(alert_prop_edges(edges))
        assert "?" in capture.payloads[0]["text"]

    def test_throttle_key_includes_sport(self, capture):
        asyncio.run(alert_prop_edges([
            {"player": "P", "market": "pts", "side": "over",
             "edge_pct": 1, "target_book": "b", "target_price": 100},
        ], sport="NFL"))
        assert capture.payloads[0]["throttle_key"] == "props:NFL"


class TestAlertSystem:
    def test_info_is_silent_callisto_prefix(self, capture):
        asyncio.run(alert_system("all good"))
        p = capture.payloads[0]
        assert p["text"].startswith("<b>Callisto</b>")
        assert p["silent"] is True

    def test_error_loud_system_error_prefix(self, capture):
        asyncio.run(alert_system("db down", is_error=True))
        p = capture.payloads[0]
        assert p["text"].startswith("<b>System Error</b>")
        assert p["silent"] is False


# ---------------------------------------------------------------------------
# TelegramListener
# ---------------------------------------------------------------------------

class FakeLineMonitor:
    def __init__(self, reports=None, status=None):
        self._reports = reports if reports is not None else {}
        self._status = status or {"running": True}

    def get_edge_report(self):
        return self._reports

    async def get_status(self):
        return self._status


class FakeCLVTracker:
    def __init__(self, bets=None, history=None):
        self._bets = bets or []
        self._history = history or [{"balance": 1234.56}]

    async def get_all_bets(self, limit=10):
        return self._bets[:limit]

    async def get_bankroll_history(self, limit=1):
        return self._history[:limit]


class RecordingSender:
    """Replaces send_alert inside the listener module."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message, silent=False, parse_mode="HTML",
                       throttle_key=None):
        self.messages.append({
            "text": message,
            "silent": silent,
            "parse_mode": parse_mode,
        })
        return True


@pytest.fixture
def sender(monkeypatch):
    rec = RecordingSender()

    async def fake_send(message, **kwargs):
        return await rec(message, **kwargs)

    from tools.tg import listener as listener_mod
    monkeypatch.setattr(listener_mod, "send_alert", fake_send)
    yield rec


def make_listener(sender_fixture=None, **kw):
    return TelegramListener(**kw)


class TestListenerLifecycle:
    def test_start_sets_running_and_stop_cancels(self):
        started = []

        async def poll_loop(self_loop):
            started.append(True)

        listener = TelegramListener()
        listener._poll_loop = poll_loop.__get__(listener)

        async def main():
            await listener.start()
            assert listener._running is True
            await asyncio.sleep(0.01)  # let the task actually run
            await listener.stop()
            assert listener._running is False

        asyncio.run(main())
        assert started == [True]

    def test_double_start_is_noop(self):
        listener = TelegramListener()

        async def main():
            await listener.start()
            task1 = listener._task
            await listener.start()
            assert listener._task is task1
            await listener.stop()

        asyncio.run(main())


class TestListenerRouting:
    def test_help_command_routes_to_cmd_help(self, sender):
        listener = TelegramListener()

        async def main():
            await listener._handle_message("/help")

        asyncio.run(main())
        assert any("Callisto Commands" in m["text"] for m in sender.messages)

    def test_start_alias_routes_to_help(self, sender):
        listener = TelegramListener()

        async def main():
            await listener._handle_message("/start")

        asyncio.run(main())
        assert sender.messages

    def test_status_routes_to_line_monitor(self, sender):
        lm = FakeLineMonitor(status={"running": True})
        listener = TelegramListener(line_monitor=lm)

        async def main():
            await listener._handle_message("/status")

        asyncio.run(main())
        assert any("System Status" in m["text"] for m in sender.messages)
        assert any("Monitor: ON" in m["text"] for m in sender.messages)

    def test_bets_without_tracker_reports_unavailable(self, sender):
        listener = TelegramListener()

        async def main():
            await listener._handle_message("/bets")

        asyncio.run(main())
        assert any("CLV tracker not available" in m["text"]
                   for m in sender.messages)

    def test_bankroll_with_tracker(self, sender):
        tracker = FakeCLVTracker(bets=[
            {"id": 1, "result": "won"}, {"id": 2, "result": "lost"},
            {"id": 3, "result": "pending"},
        ])
        listener = TelegramListener(clv_tracker=tracker)

        async def main():
            await listener._handle_message("/bankroll")

        asyncio.run(main())
        text = sender.messages[-1]["text"]
        assert "$1234.56" in text
        assert "1W-1L" in text
        assert "(1 pending)" in text

    def test_unknown_text_spawns_smart_query_task(self, sender, monkeypatch):
        from tools.tg import listener as listener_mod
        done = []

        async def fake_smart(text_self, t):
            done.append(t)

        monkeypatch.setattr(
            TelegramListener, "_cmd_smart_query", fake_smart
        )
        listener = TelegramListener()

        async def main():
            await listener._handle_message("why did LAL cover?")

        asyncio.run(main())
        assert done == ["why did LAL cover?"]
        # no immediate reply — handled in background task
        assert sender.messages == []


class TestListenerStatusHandlers:
    def test_cmd_status_with_edge_report(self, sender):
        lm = FakeLineMonitor(reports={
            "nba": {"total_edges": 3},
            "nfl": {"total_edges": 0},
        })
        listener = TelegramListener(line_monitor=lm)
        asyncio.run(listener._cmd_status())
        text = "\n".join(m["text"] for m in sender.messages)
        assert "nba: 3 edges" in text

    def test_cmd_bets_formats_rows(self, sender):
        tracker = FakeCLVTracker(bets=[{
            "id": 42, "team": "BOS", "market": "h2h",
            "placement_odds": -120, "stake": 33.0,
            "bookmaker": "dk", "result": "pending",
        }])
        listener = TelegramListener(clv_tracker=tracker)
        asyncio.run(listener._cmd_bets())
        text = sender.messages[-1]["text"]
        assert "#42 PENDING: BOS h2h -120" in text
        assert "$33 @ dk" in text

    def test_cmd_bets_empty(self, sender):
        listener = TelegramListener(clv_tracker=FakeCLVTracker(bets=[]))
        asyncio.run(listener._cmd_bets())
        assert any("No bets recorded." == m["text"] for m in sender.messages)

    def test_cmd_edges_no_monitor(self, sender):
        listener = TelegramListener()
        asyncio.run(listener._cmd_edges())
        assert any("Line monitor not available" in m["text"]
                   for m in sender.messages)

    def test_cmd_edges_with_cross_book_report(self, sender):
        lm = FakeLineMonitor(reports={
            "nba": {
                "total_edges": 2,
                "cross_book_h2h": [{
                    "team": "LAL",
                    "implied_range": 0.04,
                    "soft_book_edges": [
                        {"edge_vs_sharp": 0.031, "bookmaker": "dk"},
                        {"edge_vs_sharp": 0.052, "bookmaker": "fanatics"},
                    ],
                }],
            },
        })
        listener = TelegramListener(line_monitor=lm)
        asyncio.run(listener._cmd_edges())
        text = "\n".join(m["text"] for m in sender.messages)
        assert "<b>nba</b>: 2 edges" in text
        assert "best 5.2% @ fanatics" in text

    def test_cmd_edges_empty_reports(self, sender):
        listener = TelegramListener(line_monitor=FakeLineMonitor({}))
        asyncio.run(listener._cmd_edges())
        assert any("No edge data yet." == m["text"] for m in sender.messages)


class TestBestEdge:
    def _reports(self):
        return {
            "nba": {
                "cross_book_h2h": [{
                    "game": "LAL vs BOS",
                    "team": "LAL",
                    "num_bookmakers": 8,
                    "implied_range": 0.05,
                    "sharp_consensus": 0.62,
                    "soft_book_edges": [
                        {
                            "edge_vs_sharp": 0.064, "bookmaker": "DraftKings",
                            "price": -105,
                            "ev": {"expected_value": 12.5,
                                   "kelly_fraction": 0.02},
                        },
                    ],
                }],
                "cross_book_spreads": [],
                "cross_book_totals": [],
            },
        }

    def test_best_edge_found_on_dk(self, sender):
        listener = TelegramListener(
            line_monitor=FakeLineMonitor(self._reports()))
        asyncio.run(listener._cmd_best_edge())
        text = "\n".join(m["text"] for m in sender.messages)
        assert "Best Edge Right Now" in text
        assert "Edge: <b>6.4%</b>" in text
        assert "Available on your book." in text
        assert "EV: $12.50 per $100" in text
        assert "Kelly: 2.0%" in text

    def test_offshore_book_hint(self, sender):
        reports = self._reports()
        se = reports["nba"]["cross_book_h2h"][0]["soft_book_edges"][0]
        se["bookmaker"] = "betonline"
        listener = TelegramListener(line_monitor=FakeLineMonitor(reports))
        asyncio.run(listener._cmd_best_edge())
        assert any("Not on DK/Fanatics" in m["text"] for m in sender.messages)

    def test_below_threshold_reports_tight_markets(self, sender):
        reports = self._reports()
        se = reports["nba"]["cross_book_h2h"][0]["soft_book_edges"][0]
        se["edge_vs_sharp"] = 0.002  # 0.2%
        listener = TelegramListener(line_monitor=FakeLineMonitor(reports))
        asyncio.run(listener._cmd_best_edge())
        assert any("Markets are tight" in m["text"] for m in sender.messages)

    def test_no_monitor(self, sender):
        listener = TelegramListener()
        asyncio.run(listener._cmd_best_edge())
        assert any("Line monitor not available" in m["text"]
                   for m in sender.messages)

    def test_empty_reports(self, sender):
        listener = TelegramListener(line_monitor=FakeLineMonitor({}))
        asyncio.run(listener._cmd_best_edge())
        assert any("No edge data yet" in m["text"] for m in sender.messages)


class TestQueryCommand:
    def test_query_without_orchestrator(self, sender):
        listener = TelegramListener()
        asyncio.run(listener._cmd_query("anything"))
        assert any("Orchestrator not available" in m["text"]
                   for m in sender.messages)

    def test_query_success_truncates_long_conclusion(self, sender):
        class Orch:
            async def run_session(self, q):
                return {"summary": {
                    "conclusion": "c" * 4000,
                    "confidence_score": 0.9,
                    "confidence_tier": "CORROBORATED",
                }}

        listener = TelegramListener(orchestrator=Orch())
        asyncio.run(listener._cmd_query("q"))
        text = [m for m in sender.messages if "CORROBORATED" in m["text"]]
        assert len(text) == 1
        assert len(text[0]["text"]) <= 3600
        assert text[0]["text"].endswith("...")

    def test_query_timeout_message(self, sender):
        class Orch:
            async def run_session(self, q):
                await asyncio.sleep(5)

        listener = TelegramListener(orchestrator=Orch())
        asyncio.run(listener._cmd_query("q", timeout=0.05))
        assert any("timed out" in m["text"] for m in sender.messages)

    def test_query_failure_message(self, sender):
        class Orch:
            async def run_session(self, q):
                raise ValueError("nope")

        listener = TelegramListener(orchestrator=Orch())
        asyncio.run(listener._cmd_query("q"))
        assert any("Analysis failed" in m["text"] for m in sender.messages)


class TestSmartQuery:
    def test_smart_query_composes_status_answer(self, sender, monkeypatch):
        from tools.tg import listener as listener_mod

        status_payload = {
            "research_loop": {"cycles_completed": 4, "backtests_run": 9},
            "hypotheses": {"total": 12, "draft": 3, "backtesting": 2,
                           "paper_trading": 5, "live": 0},
            "claude_code": {"total_successful": 77},
            "line_monitor": {"credits": {"remaining": 432}},
        }

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                class R:
                    def json(self_inner):
                        return status_payload

                return R()

        monkeypatch.setattr(listener_mod.httpx, "AsyncClient", FakeClient)
        lm = FakeLineMonitor({"nba": {"total_edges": 6}})
        listener = TelegramListener(line_monitor=lm)

        async def main():
            await listener._cmd_smart_query("how are we doing?")

        asyncio.run(main())
        text = sender.messages[-1]["text"]
        assert 'You asked: "how are we doi' in text
        assert "4 research cycles" in text
        assert "77 Claude calls" in text
        assert "Hypotheses: 12 total" in text
        assert "Backtests: 9 completed" in text
        assert "432 credits left" in text
        assert "nba: 6 edges detected" in text

    def test_smart_query_survives_total_failure(self, sender, monkeypatch):
        from tools.tg import listener as listener_mod

        class BoomClient:
            def __init__(self, *a, **k):
                raise RuntimeError("no network")

        monkeypatch.setattr(listener_mod.httpx, "AsyncClient", BoomClient)
        listener = TelegramListener()

        async def main():
            await listener._cmd_smart_query("q")

        asyncio.run(main())
        # The inner fetch failure is caught and degrades to an empty-status
        # answer rather than an error reply.
        assert sender.messages
        text = sender.messages[-1]["text"]
        assert "You asked" in text
        assert "System: Running" in text


class TestPollLoop:
    @staticmethod
    def _fake_client_cls(updates, calls, listener_holder):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"result": updates}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                calls.append(params)
                # The production loop only sleeps on error paths, so a
                # successful poll would otherwise busy-spin. Stop it here
                # after one iteration.
                listener_holder["listener"]._running = False
                return FakeResponse()

        return FakeClient

    def _run_one_iteration(self, listener, updates):
        calls = []
        holder = {"listener": listener}
        monkey_cls = self._fake_client_cls(updates, calls, holder)

        async def main():
            listener._running = True
            from tools.tg import listener as listener_mod
            orig_client = listener_mod.httpx.AsyncClient
            listener_mod.httpx.AsyncClient = monkey_cls
            try:
                await asyncio.wait_for(listener._poll_loop(), timeout=3)
            finally:
                listener_mod.httpx.AsyncClient = orig_client
                listener._running = False

        asyncio.run(main())
        assert listener._running is False
        assert len(calls) >= 1

    def test_poll_loop_processes_updates_for_own_chat(self, monkeypatch, sender):
        from tools.tg import listener as listener_mod
        monkeypatch.setattr(listener_mod, "CHAT_ID", "999")

        updates = [{
            "update_id": 41,
            "message": {"chat": {"id": 999}, "text": "/help"},
        }]
        calls = []
        holder = {"listener": None}
        monkeypatch.setattr(
            listener_mod.httpx, "AsyncClient",
            self._fake_client_cls(updates, calls, holder),
        )

        listener = TelegramListener()
        holder["listener"] = listener
        self._run_one_iteration(listener, updates)
        assert listener._last_update_id == 41
        assert any("Callisto Commands" in m["text"] for m in sender.messages)

    def test_poll_loop_ignores_other_chats(self, monkeypatch, sender):
        from tools.tg import listener as listener_mod
        monkeypatch.setattr(listener_mod, "CHAT_ID", "999")

        updates = [{
            "update_id": 55,
            "message": {"chat": {"id": 111}, "text": "/help"},
        }]
        calls = []
        holder = {"listener": None}
        monkeypatch.setattr(
            listener_mod.httpx, "AsyncClient",
            self._fake_client_cls(updates, calls, holder),
        )

        listener = TelegramListener()
        holder["listener"] = listener
        self._run_one_iteration(listener, updates)
        assert listener._last_update_id == 55  # offset still advances
        assert sender.messages == []          # but no reply sent


# ---------------------------------------------------------------------------
# Package hygiene
# ---------------------------------------------------------------------------

class TestPackageHygiene:
    def test_tools_tg_init_exports_all_public_names(self):
        import tools.tg as pkg
        for name in pkg.__all__:
            assert getattr(pkg, name, None) is not None

    def test_submodules_importable_individually(self):
        import importlib
        for mod in ("tools.tg.config", "tools.tg.client",
                    "tools.tg.alerts", "tools.tg.listener"):
            assert importlib.import_module(mod) is not None

    def test_no_live_execution_surface_added(self):
        """The split must not add any live-betting execution surface."""
        import inspect
        from tools.tg import listener as listener_mod
        src = inspect.getsource(listener_mod)
        assert "execute" not in src.lower()
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src

    def test_facade_line_count_small(self):
        """The facade must stay thin — this is a split, not a move-in-place."""
        import pathlib
        facade = pathlib.Path(telegram_facade.__file__)
        lines = facade.read_text().splitlines()
        assert len(lines) < 80
