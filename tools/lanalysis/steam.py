"""Steam move detection — coordinated sharp action across books."""

import numpy as np

from tools.lanalysis._util import _parse_timestamp


def detect_steam(
    line_snapshots: list[dict],
    threshold_cents: int = 15,
    time_window_minutes: int = 10,
) -> list[dict]:
    """
    Detect steam moves — rapid coordinated line movement across multiple books.

    A steam move is when sharp syndicates hit multiple sportsbooks simultaneously,
    causing lines to move rapidly across the market. These are among the most
    reliable sharp signals because they represent coordinated, informed action.

    Detection logic:
    1. Group snapshots by time window
    2. For each window, measure movement magnitude per book
    3. If multiple books move >= threshold in the same direction within the window,
       flag as steam

    Velocity matters more than magnitude — a 15-cent move in 5 minutes is sharper
    than a 30-cent move over 6 hours.

    Args:
        line_snapshots: List of dicts, each with:
            - timestamp: ISO string or Unix epoch
            - line: float (the spread/total/price)
            - book: str (bookmaker name)
        threshold_cents: Minimum movement in cents (for prices) or 0.1*pts (for
            spreads) to qualify. Default 15 cents.
        time_window_minutes: Time window to check for coordinated moves.

    Returns:
        List of detected steam moves with metadata.
    """
    if len(line_snapshots) < 4:
        return []

    # Parse and sort by timestamp
    parsed = []
    for snap in line_snapshots:
        ts = _parse_timestamp(snap.get("timestamp", 0))
        parsed.append({
            "timestamp": ts,
            "line": float(snap["line"]),
            "book": snap.get("book", "unknown"),
        })
    parsed.sort(key=lambda x: x["timestamp"])

    window_seconds = time_window_minutes * 60
    threshold = threshold_cents / 100.0  # Convert cents to points

    # For each snapshot, look forward within the window and track per-book movement
    steam_moves = []
    processed_windows: set[tuple] = set()  # Avoid duplicate detections

    for i, anchor in enumerate(parsed):
        # Collect all snapshots within the time window after this anchor
        window_end = anchor["timestamp"] + window_seconds
        window_snaps = [s for s in parsed[i:] if s["timestamp"] <= window_end]

        if len(window_snaps) < 3:
            continue

        # Track first and last line per book within window
        book_first: dict[str, float] = {}
        book_last: dict[str, float] = {}
        book_first_ts: dict[str, float] = {}
        book_last_ts: dict[str, float] = {}

        for s in window_snaps:
            bk = s["book"]
            if bk not in book_first:
                book_first[bk] = s["line"]
                book_first_ts[bk] = s["timestamp"]
            book_last[bk] = s["line"]
            book_last_ts[bk] = s["timestamp"]

        # Calculate movement per book
        movements: dict[str, float] = {}
        velocities: dict[str, float] = {}
        for bk in book_first:
            mv = book_last[bk] - book_first[bk]
            elapsed = max(book_last_ts[bk] - book_first_ts[bk], 1.0)
            movements[bk] = mv
            velocities[bk] = mv / (elapsed / 60.0)  # Points per minute

        # Count books moving in the same direction above threshold
        up_movers = {bk: mv for bk, mv in movements.items() if mv >= threshold}
        down_movers = {bk: mv for bk, mv in movements.items() if mv <= -threshold}

        # Steam = 2+ books moving in same direction above threshold
        for direction_label, movers in [("up", up_movers), ("down", down_movers)]:
            if len(movers) < 2:
                continue

            # Create a signature to deduplicate
            sig = (
                direction_label,
                frozenset(movers.keys()),
                round(anchor["timestamp"] / (window_seconds / 2)),  # Bucket
            )
            if sig in processed_windows:
                continue
            processed_windows.add(sig)

            avg_movement = float(np.mean(list(movers.values())))
            max_movement = max(movers.values(), key=abs)
            avg_velocity = float(np.mean([velocities[bk] for bk in movers]))

            # Confidence based on coordination breadth, speed, and magnitude
            books_fraction = len(movers) / max(len(book_first), 1)
            magnitude_score = min(abs(avg_movement) / 0.5, 1.0)
            velocity_score = min(abs(avg_velocity) / 0.1, 1.0)  # 0.1 pts/min = high
            confidence = float(np.clip(
                0.35 * books_fraction + 0.35 * magnitude_score + 0.30 * velocity_score,
                0.0, 1.0,
            ))

            steam_moves.append({
                "direction": direction_label,
                "books_moved": len(movers),
                "total_books_tracked": len(book_first),
                "book_movements": {bk: round(mv, 4) for bk, mv in movers.items()},
                "book_velocities": {bk: round(velocities[bk], 4) for bk in movers},
                "avg_movement": round(avg_movement, 4),
                "max_movement": round(max_movement, 4),
                "avg_velocity_per_min": round(avg_velocity, 4),
                "window_start": anchor["timestamp"],
                "window_minutes": time_window_minutes,
                "confidence": round(confidence, 4),
                "interpretation": (
                    f"STEAM MOVE {direction_label.upper()}: {len(movers)}/{len(book_first)} "
                    f"books moved {direction_label} by avg {abs(avg_movement):.2f} pts in "
                    f"{time_window_minutes} min (velocity: {abs(avg_velocity):.3f} pts/min). "
                    f"Confidence: {confidence:.0%}."
                ),
            })

    # Sort by confidence descending
    steam_moves.sort(key=lambda x: x["confidence"], reverse=True)
    return steam_moves
