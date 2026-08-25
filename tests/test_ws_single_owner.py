"""Regression tests: LineMonitor is the sole application-lifespan WS owner.

The odds provider allows one WebSocket connection per API key. The API
lifespan previously started the module-global start_odds_stream() on top of
LineMonitor's own OddsWebSocket, so the second connection evicted the first
(reconnect thrash) and bypassed CALLISTO_WS_ENABLED=0.

Network-free: source-structure assertions plus a faked socket class; no live
endpoint is contacted.
"""
import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

import api  # noqa: F401  (import must succeed without odds-stream wiring)
from tools.line_monitor import WS_SPORTS, LineMonitor


API_SRC = Path(api.__file__).read_text()


@pytest.fixture
def fake_ws_cls():
    """Stub `websockets` so tools/odds_ws imports, and return a fake socket."""
    if "websockets" not in sys.modules:
        try:
            import websockets  # noqa: F401
        except ModuleNotFoundError:
            stub = types.ModuleType("websockets")
            stub.connect = None
            sys.modules["websockets"] = stub

    import tools.odds_ws as odds_ws_mod

    class _FakeWS:
        instances = []

        def __init__(self, on_update=None, sports=None):
            self.on_update = on_update
            raw = sports if isinstance(sports, str) else ",".join(sports or [])
            self.sports = [s.strip() for s in raw.split(",") if s.strip()]
            self.started = False
            self.stopped = False
            _FakeWS.instances.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    original = odds_ws_mod.OddsWebSocket
    odds_ws_mod.OddsWebSocket = _FakeWS
    _FakeWS.instances = []
    yield _FakeWS
    odds_ws_mod.OddsWebSocket = original


def test_api_lifespan_has_no_global_odds_stream():
    """api.py must not import or call the global stream helpers."""
    assert "from tools.odds_ws import" not in API_SRC
    assert "await start_odds_stream" not in API_SRC
    assert "await stop_odds_stream" not in API_SRC


def test_api_lifespan_documents_single_owner():
    """Ownership comment sits where LineMonitor is started in lifespan."""
    tree = ast.parse(API_SRC)
    lifespan = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    src = ast.get_source_segment(API_SRC, lifespan)
    assert "Sole owner" in src


def test_line_monitor_is_single_ingestion_wired_owner(fake_ws_cls):
    """LineMonitor's socket is stored, started once, wired to ingestion."""
    async def run():
        monitor = LineMonitor()
        await monitor._start_ws()
        return monitor

    monitor = asyncio.run(run())


    assert len(fake_ws_cls.instances) == 1, "exactly one socket may be created"
    sock = fake_ws_cls.instances[0]
    assert sock is monitor._ws_client
    assert sock.started and not sock.stopped
    assert sock.on_update == monitor._handle_ws_update
    assert sock.sports == [s.strip() for s in WS_SPORTS.split(",")]

    # Shutdown has exactly this one owner too.
    asyncio.run(monitor.stop())
    assert sock.stopped
    assert monitor._ws_client is None


def test_odds_ws_public_helpers_kept():
    """Backward compat: module-level stream helpers still exist."""
    from tools.odds_ws import OddsWebSocket, start_odds_stream, stop_odds_stream  # noqa: F401
