"""Stage B fail-closed invariant registry (wave 6).

Source/AST-level pins only — these tests read the files as text so they
never import tools.autonomous or start servers.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


# 1. CLI seal-key gate before ask does anything expensive
def test_callisto_has_check_seal_key():
    src = _read("tools", "cli", "ask.py")
    assert "def check_seal_key" in src
    assert "check_seal_key" in _read("callisto.py")


def test_cmd_ask_gates_on_check_seal_key_before_router():
    src = _read("tools", "cli", "ask.py")
    start = src.index("async def cmd_ask(")
    nxt = src.find("\ndef ", start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    gate = body.index("check_seal_key()")
    router = body.index("_load_router(")
    assert gate < router, "cmd_ask must check seal key before loading router/research"


# 2. paper trading: only paper_trading is a paper signal status
def test_paper_signal_statuses_frozenset_literal():
    src = _read("tools", "signals", "paper.py")
    line = [l for l in src.splitlines()
            if "_PAPER_TRADE_SIGNAL_STATUSES =" in l][0]
    assert 'frozenset({"paper_trading"})' in line
    assert '"live"' not in line


# 3. api.py: admin-gated GETs vs public health endpoints
def test_tasks_and_wiki_stats_require_admin_or_loopback():
    src = _read("api.py")
    for route in ('@app.get("/tasks"', '@app.get("/wiki/stats"'):
        idx = src.index(route)
        chunk = src[idx:src.find("\n@", idx)]
        assert "require_admin_or_loopback" in chunk, route


def test_health_endpoints_are_not_admin_gated():
    src = _read("api.py")
    for route in ('@app.get("/health")', '@app.get("/health/livez")',
                  '@app.get("/health/readyz")'):
        idx = src.index(route)
        chunk = src[idx:src.find("\n@", idx)]
        assert "require_admin_or_loopback" not in chunk, route


# 4. preregistration verify_seal fails closed on exception
def test_verify_seal_returns_false_on_exception():
    src = _read("agp", "preregistration.py")
    start = src.index("def verify_seal(")
    nxt = src.find("\n    def ", start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    assert "except Exception:" in body
    assert "return False" in body
    except_idx = body.index("except Exception:")
    ret_idx = body.rindex("return False", except_idx)
    tail = body[ret_idx:body.index("\n", ret_idx)]
    assert tail.strip() == "return False"


# 5. autonomous get_status exposes last-cycle fields
def test_get_status_reports_cycle_health():
    src = _read("tools", "autonomous.py") + _read("tools", "auto", "status.py")
    start = src.rindex("def get_status(")
    # Slice 5 moved the dict keys into tools.auto.status.build_research_loop_status;
    # scan the concatenated sources rather than only the facade wrapper.
    assert '"last_cycle_ok"' in src
    assert '"last_cycle_phase_failures"' in src
    nxt = src.find("\n    def ", start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    assert "build_research_loop_status" in body or '"last_cycle_ok"' in body


# 6. latency finding exists and mentions p50
def test_hermes_latency_finding_mentions_p50():
    src = _read("findings", "hermes_latency_2026-08-26.md")
    assert "p50" in src
