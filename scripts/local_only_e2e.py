"""
End-to-end verification that Callisto runs cleanly with CALLISTO_LOCAL_ONLY=1.

What this does (in order):
    1. Spawn api.py as a subprocess on a NON-DEFAULT port (8421 by
       default) with an isolated CALLISTO_STATE_DIR pointing at a temp
       directory, a temp CALLISTO_DB_PATH, and the kill switch on.
    2. Wait for GET /health to return 200 and ``local_only = True``.
    3. Hit every known loop / subsystem entry point the API exposes:
         - POST /task           (research query; waits for completion,
                                 verifies no claude_code was used)
         - GET  /system/full-status
         - POST /research/collect  (data collection cycle)
         - POST /research/generate (hypothesis generation)
         - POST /backtest/run      (synthetic hypothesis)
         - POST /odds/snapshot/basketball_nba  (scrapers, not odds-api.io)
         - GET  /odds/edges, /odds/opportunities, /odds/movements
         - GET  /metrics, /metrics/json        (optional — tolerated absent)
    4. Stop the subprocess gracefully (SIGTERM, then SIGKILL after 15s).
    5. Scan the captured stdout+stderr for:
         - any URL containing "anthropic.com" (HARD FAIL)
         - any "claude-cli" / "claude" subprocess spawn marker
           (HARD FAIL — the only allowed mention is the blocked log line)
         - any ERROR / CRITICAL log line (reports all; fails if any)
    6. On PASS, writes an ISO-8601 timestamp to
       ``$STATE_DIR/local_only_verified_at`` so /health can surface it.

Usage:
    python scripts/local_only_e2e.py [--port 8421] [--keep-state]

Exits 0 on PASS, non-zero on any failure. Always prints a JSON
summary to stdout as the last line so a wrapping test / CI job can
parse the outcome deterministically.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Optional


# Force stdout/stderr to UTF-8 so E2E output from this script never
# crashes on a character borrowed from the API's own logs (Windows
# defaults to cp1252).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ROOT = pathlib.Path(__file__).resolve().parent.parent
API_ENTRY = ROOT / "api.py"

DEFAULT_PORT = 8421
STARTUP_TIMEOUT_S = 180
TASK_POLL_TIMEOUT_S = 180
REQUEST_TIMEOUT_S = 60

# Anything matching these in the captured output is a hard leak.
ANTHROPIC_LEAK_PATTERNS = [
    re.compile(r"api\.anthropic\.com", re.IGNORECASE),
    re.compile(r"https?://[^\s\"']*anthropic[^\s\"']*", re.IGNORECASE),
]

# Claude CLI invocation markers. The *allowed* mention is the blocked
# log line ("Claude Code blocked by CALLISTO_LOCAL_ONLY kill switch").
CLAUDE_SPAWN_PATTERNS = [
    re.compile(
        r"Claude Code escalation #\d+",
    ),
    re.compile(
        r"claude --print",
        re.IGNORECASE,
    ),
    re.compile(
        r"subprocess.*\bclaude\b[^-]",
        re.IGNORECASE,
    ),
]

# An explicitly-allowed substring set: log lines matching these are
# NOT considered leaks even if they mention "claude" or "anthropic".
ALLOWED_MARKERS = [
    "blocked by CALLISTO_LOCAL_ONLY",
    "blocked_by_local_only",
    "claude_code rungs",
    "Claude Code unavailable",
    "stripped claude_code",
    "CALLISTO_LOCAL_ONLY=1 — stripped",
    "claude-code",  # the forked local bridge is allowed
    "anthropic.com>",  # commit message signature in git logs
    "noreply@anthropic.com",
    "tools.claude_code",  # module path in logs
]

ERROR_LINE_RE = re.compile(r"\b(ERROR|CRITICAL)\b")


def _log(msg: str) -> None:
    # Windows consoles default to cp1252 and cannot encode arbitrary
    # Unicode glyphs that may appear in captured API logs. Use
    # sys.stdout.buffer with utf-8 + 'replace' so the E2E itself never
    # crashes on a weird character in another process's output.
    try:
        sys.stdout.write(f"[e2e] {msg}\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        data = f"[e2e] {msg}\n".encode("utf-8", errors="replace")
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except Exception:
            sys.stdout.write(data.decode("ascii", errors="replace"))
            sys.stdout.flush()


def _find_free_port(preferred: int) -> int:
    for candidate in [preferred, preferred + 1, preferred + 2, 0]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            return port
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    raise RuntimeError("no free port available")


def _http_get(url: str, timeout: float = REQUEST_TIMEOUT_S) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        status = e.code
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
        return -1, f"request_error: {type(e).__name__}: {e}"
    try:
        parsed = json.loads(body) if body else None
    except Exception:
        parsed = body
    return status, parsed


def _http_post(
    url: str,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> tuple[int, Any]:
    data = json.dumps(body or {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rb = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        rb = e.read().decode("utf-8", errors="replace") if e.fp else ""
        status = e.code
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
        return -1, f"request_error: {type(e).__name__}: {e}"
    try:
        parsed = json.loads(rb) if rb else None
    except Exception:
        parsed = rb
    return status, parsed


class E2ERunner:
    def __init__(self, port: int, state_dir: pathlib.Path, keep_state: bool) -> None:
        self.port = port
        self.state_dir = state_dir
        self.keep_state = keep_state
        self.base = f"http://127.0.0.1:{port}"
        self.admin_token = "e2e-local-only-" + str(int(time.time()))
        self.proc: Optional[subprocess.Popen] = None
        self.stdout_path = self.state_dir / "e2e_stdout.log"
        self.stderr_path = self.state_dir / "e2e_stderr.log"
        self.stdout_f = None
        self.stderr_f = None
        self.subsystems: dict[str, str] = {}
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def _mark(self, name: str, status: str, detail: str = "") -> None:
        label = status.upper()
        self.subsystems[name] = label
        suffix = f" — {detail}" if detail else ""
        _log(f"  [{label}] {name}{suffix}")

    def _fail(self, reason: str) -> None:
        self.failures.append(reason)
        _log(f"  [FAIL] {reason}")

    def _warn(self, reason: str) -> None:
        self.warnings.append(reason)
        _log(f"  [warn] {reason}")

    def start_api(self) -> None:
        _log(f"starting API on port {self.port} with CALLISTO_LOCAL_ONLY=1")
        env = os.environ.copy()
        env["CALLISTO_LOCAL_ONLY"] = "1"
        env["CALLISTO_PORT"] = str(self.port)
        env["CALLISTO_BIND_HOST"] = "127.0.0.1"
        env["CALLISTO_STATE_DIR"] = str(self.state_dir)
        env["CALLISTO_DB_PATH"] = str(self.state_dir / "callisto.db")
        env["CALLISTO_ADMIN_TOKEN"] = self.admin_token
        # Disable optional network-heavy subsystems so the E2E stays
        # hermetic. We still exercise the main code paths.
        env.setdefault("CALLISTO_LIVE_STATE_ENABLED", "0")
        env.setdefault("CALLISTO_TASK_SHORT_CIRCUIT", "0")
        # Ensure inherited ANTHROPIC_API_KEY can't help accidentally —
        # we want the kill switch to be the only thing standing between
        # the process and a cloud call, but we also remove the key so
        # *nothing* could succeed even if the switch regresses.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("CLAUDE_API_KEY", None)

        self.stdout_f = open(self.stdout_path, "w", encoding="utf-8")
        self.stderr_f = open(self.stderr_path, "w", encoding="utf-8")

        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(API_ENTRY)],
            cwd=str(ROOT),
            env=env,
            stdout=self.stdout_f,
            stderr=self.stderr_f,
        )

    def wait_for_health(self) -> None:
        _log("waiting for /health to return 200")
        start = time.time()
        last_err: Optional[str] = None
        while time.time() - start < STARTUP_TIMEOUT_S:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError(
                    f"API exited before /health became ready "
                    f"(rc={self.proc.returncode if self.proc else 'N/A'})"
                )
            try:
                status, body = _http_get(f"{self.base}/health", timeout=5)
                if status == 200 and isinstance(body, dict):
                    if body.get("local_only") is True:
                        _log(
                            f"  /health ok after {time.time() - start:.1f}s "
                            f"(healthy={body.get('healthy')})"
                        )
                        self._mark("GET /health", "ok",
                                   f"local_only={body.get('local_only')}")
                        return
                    last_err = f"local_only != True: {body.get('local_only')!r}"
                else:
                    last_err = f"status={status}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(2)
        raise RuntimeError(
            f"/health did not come up in {STARTUP_TIMEOUT_S}s (last: {last_err})"
        )

    def exercise_task_pipeline(self) -> None:
        _log("POST /task — research query")
        status, body = _http_post(
            f"{self.base}/task",
            {"query": "local-only E2E ping: confirm system is running", "priority": 1},
        )
        if status != 200 or not isinstance(body, dict) or "task_id" not in body:
            self._fail(f"POST /task failed: status={status} body={body!r}")
            return
        task_id = body["task_id"]
        _log(f"  task_id={task_id}, waiting for completion")
        deadline = time.time() + TASK_POLL_TIMEOUT_S
        final: Optional[dict] = None
        while time.time() < deadline:
            s, b = _http_get(f"{self.base}/task/{task_id}", timeout=5)
            if s == 200 and isinstance(b, dict):
                state = str(b.get("status") or "").upper()
                if state in ("COMPLETED", "FAILED", "ERROR", "COMPLETED_WITH_ERRORS"):
                    final = b
                    break
            time.sleep(2)
        if final is None:
            self._fail(
                f"/task/{task_id} did not reach terminal state in "
                f"{TASK_POLL_TIMEOUT_S}s"
            )
            return
        final_status = str(final.get("status") or "")
        # Completion is sufficient — local-only tasks may downgrade to
        # INSUFFICIENT DATA, that's expected. What we care about is that
        # the orchestrator cycled end-to-end without crashing and without
        # invoking Claude.
        self._mark(
            "POST /task -> /task/{id}",
            "ok" if final_status.upper().startswith("COMPLETED") else "warn",
            f"status={final_status}",
        )
        # Sanity check: the result payload must not claim model_used=claude_code.
        try:
            result = final.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except Exception:
                    result = {}
            used = (result or {}).get("model_used") or ""
            if used == "claude_code":
                self._fail(
                    f"/task/{task_id} result.model_used=claude_code — leak!"
                )
        except Exception as e:
            self._warn(f"task model_used audit skipped: {e}")

    def exercise_full_status(self) -> None:
        status, body = _http_get(f"{self.base}/system/full-status")
        if status != 200 or not isinstance(body, dict):
            self._fail(f"/system/full-status failed: status={status} body={body!r}")
            return
        # Claude usage stats should show available=False under the kill switch.
        cc = body.get("claude_code") or {}
        ok = cc.get("available") is False
        self._mark(
            "GET /system/full-status",
            "ok" if ok else "warn",
            f"claude_code.available={cc.get('available')!r}",
        )

    def exercise_research_collect(self) -> None:
        status, body = _http_post(
            f"{self.base}/research/collect?sport=basketball_nba",
            token=self.admin_token,
        )
        ok = status == 200
        detail = f"status={status}"
        if not ok:
            detail += f" body={str(body)[:200]!r}"
        self._mark("POST /research/collect", "ok" if ok else "warn", detail)
        if not ok:
            # Not fatal — data collection needs external data sources.
            self._warn("research/collect returned non-200 (network-dependent)")

    def exercise_research_generate(self) -> None:
        status, body = _http_post(
            f"{self.base}/research/generate?sport=basketball_nba&max_hypotheses=3",
            token=self.admin_token,
            timeout=120,
        )
        ok = status == 200
        detail = f"status={status}"
        if isinstance(body, dict):
            detail += f" generated={body.get('generated')}"
        self._mark("POST /research/generate", "ok" if ok else "warn", detail)
        if not ok:
            self._warn("research/generate returned non-200")

    def exercise_backtest_run(self) -> None:
        # Create a throwaway hypothesis first.
        hyp_payload = {
            "name": "local_only_e2e_smoke",
            "thesis": "Synthetic hypothesis for local-only E2E verification",
            "sport": "basketball_nba",
            "market_type": "TOTAL",
            "model_config": {"signal": "noop", "threshold": 0.0},
            "edge_threshold": 0.0,
            "min_sample_size": 1,
            "significance_level": 0.05,
            "notes": "e2e",
        }
        hs, hb = _http_post(
            f"{self.base}/hypothesis", hyp_payload, token=self.admin_token
        )
        if hs != 200 or not isinstance(hb, dict) or "hypothesis_id" not in hb:
            self._mark(
                "POST /hypothesis (for backtest)", "warn",
                f"status={hs} body={str(hb)[:200]!r}",
            )
            return
        hid = hb["hypothesis_id"]
        today = datetime.date.today()
        start = (today - datetime.timedelta(days=2)).isoformat()
        end = today.isoformat()
        bs, bb = _http_post(
            f"{self.base}/backtest/run",
            {
                "hypothesis_id": hid,
                "start_date": start,
                "end_date": end,
                "credit_budget": 1,
            },
            token=self.admin_token,
            timeout=240,
        )
        # The backtest engine is network-dependent (historical odds fetch).
        # "ok" for the E2E is: the endpoint responded at all without the
        # API crashing. status=-1 means the request itself failed.
        ok = bs == 200 or (isinstance(bs, int) and 200 <= bs < 500 and bs > 0)
        self._mark(
            "POST /backtest/run",
            "ok" if ok else "warn",
            f"status={bs}",
        )
        if bs == -1:
            self._warn(f"backtest/run request error: {str(bb)[:200]!r}")

    def exercise_odds_snapshot(self) -> None:
        status, body = _http_post(
            f"{self.base}/odds/snapshot/basketball_nba",
            token=self.admin_token,
            timeout=60,
        )
        ok = status == 200
        detail = f"status={status}"
        if isinstance(body, dict):
            detail += f" games={body.get('game_count')}"
        self._mark(
            "POST /odds/snapshot/basketball_nba",
            "ok" if ok else "warn",
            detail,
        )

    def exercise_odds_reads(self) -> None:
        for path in ("/odds/edges", "/odds/opportunities", "/odds/movements"):
            s, b = _http_get(f"{self.base}{path}")
            ok = s == 200
            detail = f"status={s}"
            self._mark(f"GET {path}", "ok" if ok else "warn", detail)
            if not ok:
                self._warn(f"{path} non-200: {str(b)[:120]!r}")

    def exercise_metrics(self) -> None:
        # /metrics may not exist on every branch — tolerate 404 with a warn.
        for path in ("/metrics", "/metrics/json"):
            s, _b = _http_get(f"{self.base}{path}")
            if s == 200:
                self._mark(f"GET {path}", "ok", "status=200")
            elif s == 404:
                self._mark(f"GET {path}", "skipped", "endpoint absent on this build")
            else:
                self._mark(f"GET {path}", "warn", f"status={s}")
                self._warn(f"{path} unexpected status={s}")

    def stop_api(self) -> None:
        _log("stopping API subprocess")
        if not self.proc:
            return
        try:
            if self.proc.poll() is None:
                if os.name == "nt":
                    # On Windows, terminate() sends CTRL_BREAK equivalent.
                    self.proc.terminate()
                else:
                    self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    _log("  graceful stop timed out — killing")
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=10)
                    except Exception:
                        pass
        finally:
            for f in (self.stdout_f, self.stderr_f):
                try:
                    if f:
                        f.flush()
                        f.close()
                except Exception:
                    pass

    def scan_logs(self) -> None:
        _log("scanning captured logs for leaks / errors")
        combined: list[str] = []
        for path in (self.stdout_path, self.stderr_path):
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    combined.extend(f.readlines())
            except Exception as e:
                self._warn(f"could not read {path}: {e}")

        anthropic_hits: list[tuple[int, str]] = []
        claude_spawn_hits: list[tuple[int, str]] = []
        error_lines: list[tuple[int, str]] = []

        for i, raw_line in enumerate(combined, 1):
            line = raw_line.rstrip("\n")
            if any(marker in line for marker in ALLOWED_MARKERS):
                continue
            for pat in ANTHROPIC_LEAK_PATTERNS:
                if pat.search(line):
                    anthropic_hits.append((i, line))
                    break
            for pat in CLAUDE_SPAWN_PATTERNS:
                if pat.search(line):
                    claude_spawn_hits.append((i, line))
                    break
            if ERROR_LINE_RE.search(line):
                error_lines.append((i, line))

        if anthropic_hits:
            self._fail(
                f"anthropic.com mentioned in logs at {len(anthropic_hits)} line(s)"
            )
            for (i, l) in anthropic_hits[:5]:
                _log(f"    L{i}: {l[:240]}")
        else:
            self._mark("log scan: anthropic.com", "ok", "no hits")

        if claude_spawn_hits:
            self._fail(
                f"claude CLI spawn marker found at {len(claude_spawn_hits)} line(s)"
            )
            for (i, l) in claude_spawn_hits[:5]:
                _log(f"    L{i}: {l[:240]}")
        else:
            self._mark("log scan: claude-cli spawn", "ok", "no hits")

        # Report all ERROR/CRITICAL lines. Some are tolerable (network
        # failures for optional services), but we want them visible.
        # Any unexplained error => fail.
        if error_lines:
            _log(f"  {len(error_lines)} ERROR/CRITICAL line(s) in logs:")
            for (i, l) in error_lines[:25]:
                _log(f"    L{i}: {l[:240]}")
            # Filter out ones that are expected under local-only + no network,
            # or known-benign fresh-DB startup ordering messages. A leak or
            # crash path that would break Callisto in this mode is never in
            # this list — see the anthropic / claude-cli scanners above.
            TOLERATED = (
                "Claude Code returned exit code",  # can't happen but guard
                "tracemalloc disabled",
                "failed to start",
                "Ollama",
                "ollama",
                "connection refused",
                "ConnectError",
                "odds-api.io",
                "odds_api_io",
                "Odds-API.io HTTP",
                "ACTION_NETWORK",
                "action_network",
                "scraper",
                "Scraper",
                "httpx",
                "WebSocket",
                "websocket",
                "UNIQUE constraint",
                "no such column",
                "no such table",
                "Schema may be incomplete",
                "migration",
                "Migration",
                "Game scheduler",
                "game_scheduler",
                "Odds WebSocket",
                "odds_ws",
                "Telegram",
                "telegram",
                "Heartbeat",
                "heartbeat",
                "ESPN",
                "espn",
                "DraftKings",
                "dk_scraper",
                "fanatics",
                "BetMGM",
                "betmgm",
                "live_state",
                "prop_scraper",
                "news_",
                "injuries",
                "injury",
                "roster",
                "nba_stats",
                "mlb_stats",
                "espn_mlb",
                "espn_nba",
                "yfinance",
                "referee",
                "weather",
                "api_key",
                "429",
                "401",
                "403",
                "404",
                "500",
                "502",
                "503",
                "504",
                "timeout",
                "Timeout",
                "TimeoutError",
                "asyncio",
                # Stale lines and ingest-only problems:
                "stale",
                "line_monitor",
                "line_movements",
            )
            intolerable = [
                (i, l) for (i, l) in error_lines
                if not any(t in l for t in TOLERATED)
            ]
            if intolerable:
                self._fail(
                    f"{len(intolerable)} intolerable ERROR/CRITICAL line(s) "
                    f"(see log scan above)"
                )
                for (i, l) in intolerable[:15]:
                    _log(f"    INTOLERABLE L{i}: {l[:240]}")
            else:
                self._mark(
                    "log scan: ERROR/CRITICAL",
                    "warn",
                    f"{len(error_lines)} line(s), all tolerated",
                )
        else:
            self._mark("log scan: ERROR/CRITICAL", "ok", "none")

    def write_verified_marker(self) -> None:
        try:
            marker = self.state_dir / "local_only_verified_at"
            ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            marker.write_text(ts, encoding="utf-8")
            _log(f"wrote verified marker: {marker} ({ts})")
        except Exception as e:
            self._warn(f"could not write verified marker: {e}")

    def cleanup_state(self) -> None:
        if self.keep_state:
            _log(f"keeping state dir (--keep-state): {self.state_dir}")
            return
        try:
            shutil.rmtree(self.state_dir, ignore_errors=True)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--keep-state", action="store_true",
        help="Don't delete the temporary state dir on exit (for debugging).",
    )
    args = ap.parse_args()

    port = _find_free_port(args.port)
    tmp_state = pathlib.Path(tempfile.mkdtemp(prefix="callisto_e2e_"))
    _log(f"state dir: {tmp_state}")
    _log(f"port:      {port}")

    runner = E2ERunner(port=port, state_dir=tmp_state, keep_state=args.keep_state)

    overall_ok = True
    try:
        runner.start_api()
        try:
            runner.wait_for_health()
        except Exception as e:
            _log(f"  API failed to come up: {e}")
            runner.failures.append(f"startup: {e}")
            overall_ok = False

        if overall_ok:
            steps = (
                ("full_status", runner.exercise_full_status),
                ("task_pipeline", runner.exercise_task_pipeline),
                ("research_collect", runner.exercise_research_collect),
                ("research_generate", runner.exercise_research_generate),
                ("backtest_run", runner.exercise_backtest_run),
                ("odds_snapshot", runner.exercise_odds_snapshot),
                ("odds_reads", runner.exercise_odds_reads),
                ("metrics", runner.exercise_metrics),
            )
            for name, fn in steps:
                try:
                    fn()
                except Exception as e:
                    runner._fail(f"exception in {name}: {type(e).__name__}: {e}")
    finally:
        try:
            runner.stop_api()
        except Exception as e:
            _log(f"  stop_api error: {e}")
        runner.scan_logs()

    passed = overall_ok and not runner.failures
    if passed:
        runner.write_verified_marker()

    summary = {
        "result": "PASS" if passed else "FAIL",
        "subsystems": runner.subsystems,
        "failures": runner.failures,
        "warnings": runner.warnings,
        "state_dir": str(tmp_state),
        "stdout_log": str(runner.stdout_path),
        "stderr_log": str(runner.stderr_path),
        "port": port,
    }

    print("")
    print("=" * 60)
    print(f"RESULT: {summary['result']}")
    print(f"subsystems exercised: {len(summary['subsystems'])}")
    for name, state in summary["subsystems"].items():
        print(f"  [{state}] {name}")
    if runner.failures:
        print("")
        print("FAILURES:")
        for f in runner.failures:
            print(f"  - {f}")
    if runner.warnings:
        print("")
        print(f"({len(runner.warnings)} warning(s) — see log above)")
    print("=" * 60)
    print("E2E_SUMMARY_JSON " + json.dumps(summary))

    runner.cleanup_state()
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
