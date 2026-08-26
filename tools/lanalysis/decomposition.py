"""Movement decomposition — HP-filter-inspired trend/noise separation."""

import numpy as np

from tools.lanalysis._util import _parse_timestamp


def decompose_movement(
    line_history: list[dict],
    sport: str = "americanfootball_nfl",
) -> dict:
    """
    Decompose a line history into trend (sharp) and noise (public) components.

    Uses a simplified Hodrick-Prescott-inspired filter. The HP filter separates
    a time series into a smooth trend component (lambda-penalized for curvature)
    and a cyclical/noise component. We use numpy to solve the linear system.

    For sports betting:
    - Trend = sustained directional movement from sharp money
    - Noise = random fluctuation from small/public bets

    The sharp component is the trend derivative: persistent moves in one direction.
    The public component is residual oscillation that doesn't survive smoothing.

    Args:
        line_history: List of dicts, each with:
            - timestamp: ISO string or Unix epoch
            - line: float (spread, total, or price)
            - book: str (bookmaker name, optional)
        sport: Sport key for lambda calibration.

    Returns:
        Dict with trend, noise, sharp_component, public_component, and metadata.
    """
    if len(line_history) < 3:
        return {
            "error": "Need at least 3 data points for decomposition",
            "trend": [],
            "noise": [],
            "sharp_component": 0.0,
            "public_component": 0.0,
        }

    # Extract time-ordered line values
    entries = sorted(line_history, key=lambda e: _parse_timestamp(e.get("timestamp", 0)))
    lines = np.array([float(e["line"]) for e in entries], dtype=np.float64)
    n = len(lines)

    # HP filter smoothing parameter.
    # Higher lambda = smoother trend (more aggressively separates noise).
    # NFL lines move slowly → higher lambda. NBA lines are more volatile → lower.
    lambda_map = {
        "americanfootball_nfl": 1600.0,
        "americanfootball_ncaaf": 1600.0,
        "basketball_nba": 400.0,
        "basketball_ncaab": 400.0,
        "baseball_mlb": 800.0,
        "icehockey_nhl": 800.0,
    }
    lam = lambda_map.get(sport, 800.0)

    # Solve the HP filter: minimise sum((y - tau)^2) + lambda * sum((tau_{t+1} - 2*tau_t + tau_{t-1})^2)
    # This is equivalent to: (I + lambda * K'K) * tau = y
    # where K is the second-difference matrix.
    identity = np.eye(n)

    # Build second-difference matrix K (n-2 x n)
    K = np.zeros((n - 2, n))
    for i in range(n - 2):
        K[i, i] = 1.0
        K[i, i + 1] = -2.0
        K[i, i + 2] = 1.0

    # Solve for trend
    A = identity + lam * (K.T @ K)
    trend = np.linalg.solve(A, lines)
    noise = lines - trend

    # Sharp component: net directional trend movement (first to last)
    sharp_component = float(trend[-1] - trend[0])

    # Public component: RMS of the noise — measures magnitude of random oscillation
    public_component = float(np.sqrt(np.mean(noise ** 2)))

    # Trend direction and strength
    if abs(sharp_component) < 0.25:
        trend_direction = "flat"
    elif sharp_component > 0:
        trend_direction = "rising"
    else:
        trend_direction = "falling"

    # Compute incremental trend velocities for steam detection
    trend_velocity = np.diff(trend)
    max_velocity = float(np.max(np.abs(trend_velocity))) if len(trend_velocity) > 0 else 0.0

    # Signal-to-noise ratio: how much of total variance is trend vs noise
    total_var = float(np.var(lines)) if np.var(lines) > 0 else 1e-9
    trend_var = float(np.var(trend))
    noise_var = float(np.var(noise))
    snr = trend_var / noise_var if noise_var > 1e-9 else float("inf")

    return {
        "trend": trend.tolist(),
        "noise": noise.tolist(),
        "raw_values": lines.tolist(),
        "sharp_component": round(sharp_component, 4),
        "public_component": round(public_component, 4),
        "trend_direction": trend_direction,
        "max_velocity": round(max_velocity, 4),
        "signal_to_noise": round(snr, 4),
        "trend_variance_pct": round((trend_var / total_var) * 100, 2) if total_var > 1e-9 else 0.0,
        "noise_variance_pct": round((noise_var / total_var) * 100, 2) if total_var > 1e-9 else 0.0,
        "data_points": n,
        "interpretation": (
            f"Sharp money moved the line {abs(sharp_component):.2f} points "
            f"({'toward favorite' if sharp_component < 0 else 'toward underdog'}) "
            f"with public noise RMS of {public_component:.2f}. "
            f"SNR={snr:.1f} — {'clean sharp signal' if snr > 3 else 'noisy, mixed action' if snr > 1 else 'mostly noise'}."
        ),
    }
