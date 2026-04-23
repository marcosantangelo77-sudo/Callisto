"""
Standalone launcher for the Callisto ops dashboard.

Usage:

    python scripts/run_dashboard.py
    CALLISTO_DASHBOARD_PORT=8421 python scripts/run_dashboard.py

Env vars:

    CALLISTO_DASHBOARD_PORT   (default 8421)
    CALLISTO_DASHBOARD_HOST   (default 127.0.0.1 — loopback only)
    CALLISTO_MAIN_API         (default http://localhost:8420)
    CALLISTO_DASHBOARD_TOKEN  (optional; loopback-allowed like admin)
    CALLISTO_DB_PATH          (default <repo>/data/callisto.db)

Read-only: this server never issues writes to the main Callisto API.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `tools.dashboard` resolves when
# this script is invoked from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> None:
    import uvicorn
    from tools.dashboard import build_dashboard_subapp, DEFAULT_MAIN_API, DEFAULT_DB_PATH

    host = os.environ.get("CALLISTO_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("CALLISTO_DASHBOARD_PORT", "8421"))

    app = build_dashboard_subapp(
        main_api_url=os.environ.get("CALLISTO_MAIN_API", DEFAULT_MAIN_API),
        db_path=os.environ.get("CALLISTO_DB_PATH", DEFAULT_DB_PATH),
    )

    print(f"[callisto-dashboard] http://{host}:{port}/ -> main API "
          f"{os.environ.get('CALLISTO_MAIN_API', DEFAULT_MAIN_API)}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
