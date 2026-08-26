"""
Overnight setup: complete all historical imports, then start Callisto.

Run this, walk away. It will:
1. Import all sports sequentially (no DB lock contention)
2. Start the Callisto API
3. The watchdog takes over from there

Usage: python scripts/overnight_setup.py
"""
import subprocess
import sys
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
from tools.state_paths import restart_signal_path  # noqa: E402

SPORTS = ['nba', 'nhl', 'nfl', 'mls', 'ncaam', 'ncaaw', 'nwsl', 'ufl', 'ahl', 'mlb', 'gleague', 'usl']

print("=" * 60)
print("CALLISTO OVERNIGHT SETUP")
print("=" * 60)

# Phase 1: Import all sports
print("\n[Phase 1] Historical odds import — all sports sequential")
for sport in SPORTS:
    print(f"\n--- {sport.upper()} ---", flush=True)
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, 'scripts/import_historical_odds.py', '--sport', sport],
            timeout=1200,  # 20 min max per sport
            capture_output=False,
        )
        elapsed = time.time() - start
        status = "OK" if result.returncode == 0 else f"FAILED (code {result.returncode})"
        print(f"  {sport.upper()}: {status} ({elapsed:.0f}s)", flush=True)
    except subprocess.TimeoutExpired:
        print(f"  {sport.upper()}: TIMEOUT (>20 min)", flush=True)
    except Exception as e:
        print(f"  {sport.upper()}: ERROR ({e})", flush=True)

# Phase 2: Signal restart (off-OneDrive state dir)
print("\n[Phase 2] Signaling Callisto restart...")
restart_file = restart_signal_path()
with open(restart_file, "w", encoding="utf-8") as f:
    f.write(f"overnight_setup complete — all imports done, restart API")
print(f"  Restart signal written to {restart_file}. Watchdog will start the API.")

# Phase 3: Start API directly (in case watchdog isn't running)
print("\n[Phase 3] Starting Callisto API...")
try:
    # Start API as a detached process
    subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'api:app', '--host', os.environ.get("CALLISTO_BIND_HOST", "127.0.0.1"), '--port', '8420'],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        stdout=open(PROJECT_ROOT / 'logs' / 'api_overnight.log', 'w'),
        stderr=subprocess.STDOUT,
    )
    print("  API started as background process")
except Exception as e:
    print(f"  API start failed: {e}")
    print("  The watchdog should pick this up automatically")

print("\n" + "=" * 60)
print("OVERNIGHT SETUP COMPLETE")
print("Callisto is running. Watchdog monitors. Go to sleep.")
print("=" * 60)
