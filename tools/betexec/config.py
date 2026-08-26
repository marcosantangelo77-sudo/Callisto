"""Safety limits and tunables for the bet executor.

Extracted from ``tools/bet_executor`` (split, 2026-08). All values are
parsed from the environment once at import time, exactly as before the
split — callers (including the ``tools.bet_executor`` facade) import the
resolved constants.

NOTE: the ``REGIME_SIZING_ENABLED`` / ``REGIME_SAFETY_ENABLED`` gates are
re-read by the facade at call time so runtime monkeypatching keeps working;
the constants here are the import-time defaults.
"""

import os
from pathlib import Path

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
SCREENSHOT_DIR = Path("memory/bet_screenshots")
SESSION_DIR = Path("memory/dk_session")

# --- Safety limits (configurable via env) ---
MAX_BET_PCT = float(os.getenv("EXECUTOR_MAX_BET_PCT", "0.05"))       # 5% of bankroll per bet
# SECURITY (audit H-1): hard ceiling on the SUM of all currently-pending stakes.
# Per-bet caps don't prevent ruin when N concurrent bets clear simultaneously.
# 25% bankroll exposed at any moment is the documented ceiling; raise via env.
MAX_OPEN_EXPOSURE_PCT = float(os.getenv("EXECUTOR_MAX_OPEN_EXPOSURE_PCT", "0.25"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("EXECUTOR_DAILY_LOSS_PCT", "0.20"))  # 20% of bankroll
MIN_EDGE_TO_EXECUTE = float(os.getenv("EXECUTOR_MIN_EDGE", "0.02"))  # 2% minimum EV
KELLY_FRACTION = float(os.getenv("EXECUTOR_KELLY_FRACTION", "0.25")) # Quarter Kelly
MIN_BET_AMOUNT = float(os.getenv("EXECUTOR_MIN_BET", "1.00"))       # $1 minimum

# --- Portfolio-level caps (feat/portfolio-kelly-live-loop, audit 2026-04-22) ---
# Prevents N LIVE hyps from all loading up on one MLB game. Per-game cap
# scales ALL stakes on the same event_id if their sum would exceed bankroll * cap.
MAX_GAME_EXPOSURE_PCT = float(os.getenv("CALLISTO_MAX_GAME_EXPOSURE_PCT", "0.08"))
# Per-sport cap: prevent all-MLB days from pushing too much on baseball.
MAX_SPORT_EXPOSURE_PCT = float(os.getenv("CALLISTO_MAX_SPORT_EXPOSURE_PCT", "0.15"))

# --- Drawdown kill switch (feat/portfolio-kelly-live-loop, audit 2026-04-22) ---
# If bankroll drops more than MAX_DRAWDOWN_PCT below the 30-day peak, flip
# _enabled=False on the executor AND set all LIVE hyps to 'drawdown_paused'.
# Recovery is MANUAL — auto-resume is intentionally not implemented.
MAX_DRAWDOWN_PCT = float(os.getenv("CALLISTO_MAX_DRAWDOWN_PCT", "0.15"))
DRAWDOWN_PEAK_WINDOW_DAYS = int(os.getenv("CALLISTO_DRAWDOWN_WINDOW_DAYS", "30"))

# --- Variance-dampener boundaries tied to paper-trade sample size ---
# < 25 signals: fresh evidence, force half-Kelly (0.125 base fraction)
# >= 100 signals: full quarter-Kelly allowed (0.25 base fraction)
# Smooth linear interp in between.
VAR_DAMPENER_LOW_N = int(os.getenv("CALLISTO_VAR_DAMPENER_LOW_N", "25"))
VAR_DAMPENER_HIGH_N = int(os.getenv("CALLISTO_VAR_DAMPENER_HIGH_N", "100"))

# --- Regime-aware sizing (feat/regime-aware-sizing, 2026-04-22) ---
REGIME_SIZING_ENABLED = os.getenv("CALLISTO_REGIME_SIZING", "1") == "1"
REGIME_SAFETY_ENABLED = os.getenv("CALLISTO_REGIME_SAFETY", "1") == "1"
REGIME_MIN_MULT = 0.1   # never zero-size a live bet; use safety gate for that
REGIME_MAX_MULT = 1.5   # cap upside even in the best regime

HALF_KELLY_FRACTION = 0.125  # half-Kelly relative to quarter-Kelly floor
FULL_QUARTER_KELLY_FRACTION = 0.25  # full quarter-Kelly
