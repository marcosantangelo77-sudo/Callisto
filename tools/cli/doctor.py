"""`callisto doctor` — can this box run a live question right now?"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _db_path() -> str:
    """Resolve through the entry script so tests that patch
    ``callisto._db_path`` stay effective."""
    import callisto
    return callisto._db_path()


def _default_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH",
                     str(REPO / "memory" / "callisto.db"))


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    provs: dict = {}
    print("== providers ==")
    try:
        from inference import load_providers_config
        cfg = load_providers_config(args.providers)
        default = cfg.get("default_tier") if isinstance(cfg, dict) else None
        provs = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
        for name, ep in provs.items():
            mark = "*" if name == default else " "
            print(f"  {mark}{name:<12} backend={ep.get('backend')}"
                  f" model={ep.get('model','-')}"
                  f" concurrency={ep.get('max_concurrency','?')}")
        if not provs:
            print("  NO PROVIDERS CONFIGURED"); ok = False
    except Exception as exc:
        print(f"  config unreadable: {exc}"); ok = False

    print("== hermes cli ==")
    try:
        from tools.pipeline.hermes_cli import hermes_available
        avail = hermes_available()
    except Exception as exc:
        avail = False
        print(f"  check failed: {exc}")
    print(f"  available: {avail}")
    needs_hermes = any(p.get("backend") == "hermes_cli"
                       for p in provs.values())
    if needs_hermes and not avail:
        print("  a configured provider uses backend=hermes_cli but the CLI")
        print("  is not reachable — those tiers will fail at ask time")
        ok = False

    print("== database ==")
    db = _db_path()
    print(f"  path: {db}")
    print(f"  present: {Path(db).exists()}")

    print("== source registry ==")
    try:
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        names = sorted(reg.names())
        print(f"  {len(names)} adapters registered: {', '.join(names)}")
        if not names:
            ok = False
    except Exception as exc:
        print(f"  registry unavailable: {exc}"); ok = False

    print("== seal ==")
    seal_raw = os.getenv("CALLISTO_SEAL_KEY", "").strip()
    if not seal_raw:
        print("  FAIL: CALLISTO_SEAL_KEY is not set — seals are unkeyed")
        print("  SHA-256 checksums and therefore forgeable; set a hex key")
        print("  to enable HMAC-sealed sessions")
        ok = False
    else:
        try:
            bytes.fromhex(seal_raw)
        except ValueError:
            print("  FAIL: CALLISTO_SEAL_KEY is set but is not valid hex —")
            print("  seals fall back to unkeyed (forgeable); fix the key value")
            ok = False
        else:
            print("  OK: seal key is set (hex-valid); seals are HMAC-SHA256")

    print("== bind ==")
    bind_host = os.getenv("CALLISTO_BIND_HOST", "").strip() or "127.0.0.1"
    print(f"  host: {bind_host}")
    if bind_host in ("0.0.0.0", "::"):
        print("  FAIL: binding to an unspecified address exposes the API")
        print("  beyond loopback; set CALLISTO_BIND_HOST=127.0.0.1")
        ok = False
    else:
        print("  OK: loopback default — API not exposed off-host")

    print("== money switches ==")
    import tools.order_manager
    import tools.bet_executor
    try:
        om_src = Path(tools.order_manager.__file__).read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  OrderManager source unreadable: {exc}")
        ok = False
    else:
        if re.search(
            r"self\._enabled\s*=\s*True", om_src.split("def enable", 1)[0]
        ):
            print("  FAIL: OrderManager.__init__ defaults _enabled = True;")
            print("  orders are live by default — flip the default to False")
            ok = False
        else:
            print("  OK: OrderManager.__init__ defaults _enabled = False")
    try:
        be_src = Path(tools.bet_executor.__file__).read_text(encoding="utf-8")
    except Exception as ext:
        print(f"  BetExecutor source unreadable: {ext}")
        ok = False
    else:
        # Match the __init__ body only (8-space class indent). A 4-space
        # `self._enabled` regex false-failed here even when the default is
        # False — doctor must not cry wolf on a safe executor.
        init_m = re.search(
            r"class BetExecutor\b.*?def __init__\(self\):(.*?)(\n    (?:async )?def )",
            be_src,
            re.S,
        )
        init_body = init_m.group(1) if init_m else ""
        if not re.search(r"self\._enabled\s*=\s*False", init_body):
            print("  FAIL: BetExecutor.__init__ does not assign _enabled = False")
            ok = False
        else:
            print("  OK: BetExecutor.__init__ assigns _enabled = False")
    print(f"  CALLISTO_LOCAL_ONLY: {'on' if os.getenv('CALLISTO_LOCAL_ONLY', '').strip() else 'off'}")
    allow_live = os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE", "").strip()
    print(f"  CALLISTO_ALLOW_LIVE_EXECUTE: {'on' if allow_live else 'off'}")

    print("\ndoctor:", "OK" if ok else "PROBLEMS FOUND (see above)")
    return 0 if ok else 1


_cmd_doctor = cmd_doctor  # backwards-compatible alias
