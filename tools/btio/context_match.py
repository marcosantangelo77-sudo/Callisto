"""Game-vs-context matching and needs-context checks.

Extracted verbatim from tools/backtest_io.py. FILTERABLE_CONTEXT_FACTORS
remains canonically defined in tools.backtest_io and is imported at call
time to avoid import cycles.
"""

import re


def _game_matches_context_filter(
    game_context: dict,
    hypothesis_name: str,
    thesis: str,
    config: dict,
) -> bool:
    """Check if a game matches the hypothesis's contextual requirements.

    Uses hypothesis name, thesis text, and config.context_factors to determine
    what context conditions are needed, then checks them against the pre-computed
    game context.

    Returns True if the game should be processed, False to skip.
    """
    name_lower = hypothesis_name.lower().replace("-", " ").replace("_", " ")
    thesis_lower = (thesis or "").lower()
    text = f"{name_lower} {thesis_lower}"
    context_factors = config.get("context_factors", [])
    cf_set = {f.lower().replace(" ", "_") for f in context_factors}

    if not game_context:
        return False  # Context filtering expected but no data — fail closed

    # ── STRUCTURED GAME FILTERS (from model_config — highest priority) ──
    # These are machine-readable specs generated alongside the hypothesis,
    # not reverse-engineered from natural language.  When present they are
    # authoritative; the regex fallbacks below only fire for legacy
    # hypotheses that lack structured filters.
    gf = config.get("game_filters") or {}
    if gf:
        gf_side = gf.get("side")  # "home", "away", or None

        # ── Game-level filters (not team-specific) ──
        if "min_rest_mismatch" in gf:
            hr = game_context.get("home_days_rest", 1)
            ar = game_context.get("away_days_rest", 1)
            if abs(hr - ar) < gf["min_rest_mismatch"]:
                return False

        if gf.get("require_revenge"):
            if not game_context.get("is_revenge"):
                return False

        # ── Team-specific filters: conjunctive per-team ──
        # When gf_side is set, only that side is checked.
        # When gf_side is None, ALL team-specific conditions must be
        # satisfied by the SAME team.  Previous OR-per-condition logic
        # allowed home to pass one filter and away another, producing
        # identical event sets across hypotheses with different theses.
        candidates = {gf_side} if gf_side else {"home", "away"}

        if gf.get("require_b2b"):
            candidates = {s for s in candidates if game_context.get(f"{s}_b2b")}
            if not candidates:
                return False

        if "max_rest_days" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_days_rest", 99) <= gf["max_rest_days"]}
            if not candidates:
                return False

        if "min_games_in_4" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_games_in_4", 1) >= gf["min_games_in_4"]}
            if not candidates:
                return False

        if "require_road_streak" in gf:
            threshold = gf["require_road_streak"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_road_streak", 0) >= threshold}
            if not candidates:
                return False

        if gf.get("require_sandwich"):
            candidates = {s for s in candidates if game_context.get(f"{s}_sandwich")}
            if not candidates:
                return False

        if "min_win_pct" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_win_pct", 0.5) >= gf["min_win_pct"]}
            if not candidates:
                return False

        if "max_win_pct" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_win_pct", 0.5) <= gf["max_win_pct"]}
            if not candidates:
                return False

        if "win_pct_range" in gf:
            lo, hi = gf["win_pct_range"]
            candidates = {s for s in candidates
                          if lo <= game_context.get(f"{s}_win_pct", 0.5) <= hi}
            if not candidates:
                return False

        if "max_prev_margin" in gf:
            threshold = gf["max_prev_margin"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_prev_margin", 0) <= threshold}
            if not candidates:
                return False

        if "min_prev_margin" in gf:
            threshold = gf["min_prev_margin"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_prev_margin", 0) >= threshold}
            if not candidates:
                return False

        # Structured filters are authoritative — skip regex fallbacks
        return True

    # ── REGEX FALLBACKS (for hypotheses without structured filters) ──
    # Regex matching infers context filters from hypothesis keywords
    # (sandwich, revenge, blowout, etc.). However, two hypotheses sharing
    # the same keyword (e.g., both containing "revenge") will match the
    # SAME game_context field and produce identical event sets.
    #
    # Guard: require explicit context_factors to use regex fallbacks.
    # Hypotheses without context_factors get 0 events and are rejected
    # for insufficient data — better than corrupted identical event sets.
    if not cf_set:
        return False
    from tools import backtest_io

    _any_filter_matched = False

    # ── Back-to-back filter ──
    if ("back_to_back" in cf_set or "is_b2b_second_night" in cf_set
            or "back_to_back_second_night" in cf_set
            or re.search(r"\bb2b\b|\bback.to.back\b", text)):
        _any_filter_matched = True
        if not game_context.get("home_b2b") and not game_context.get("away_b2b"):
            return False

    # ── Days rest filter ──
    if ("days_rest" in cf_set or "days_since_last_game" in cf_set
            or re.search(r"\bshort.rest\b|\brest.mismatch\b|\bdays?.rest\b", text)):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 99)
        away_rest = game_context.get("away_days_rest", 99)
        if home_rest > 2 and away_rest > 2:
            return False

    # ── Extra rest filter ──
    if "extra_rest_days" in cf_set or re.search(r"\bextra.rest\b", text):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 1)
        away_rest = game_context.get("away_days_rest", 1)
        if home_rest < 3 and away_rest < 3:
            return False

    # ── Road trip filter ──
    if ("consecutive_road_games" in cf_set or "road_trip_game_number" in cf_set
            or re.search(r"\broad.trip\b|\b\d\+?\s*(?:road|away)\b|\bconsecutive.(?:road|away)\b", text)):
        _any_filter_matched = True
        threshold = 3
        m = re.search(r"(\d)\+?\s*(?:road|away)", text)
        if m:
            threshold = int(m.group(1))
        away_streak = game_context.get("away_road_streak", 0)
        home_road_before = game_context.get("home_road_streak", 0)
        if away_streak < threshold and home_road_before < threshold:
            return False

    # ── Schedule density (3in4, 4in5) filter ──
    if ("schedule_density" in cf_set or "games_in_last_4_days" in cf_set
            or re.search(r"\b3.?in.?4\b|\b4.?in.?5\b|\bschedule.compress\b|\bschedule.density\b", text)):
        _any_filter_matched = True
        home_g4 = game_context.get("home_games_in_4", 1)
        away_g4 = game_context.get("away_games_in_4", 1)
        if home_g4 < 3 and away_g4 < 3:
            return False

    # ── Sandwich game filter ──
    if ("schedule_context" in cf_set
            or re.search(r"\bsandwich\b|\btrap.game\b|\bletdown\b", text)):
        _any_filter_matched = True
        if not game_context.get("home_sandwich") and not game_context.get("away_sandwich"):
            return False

    # ── Revenge game filter ──
    if ("revenge_game_flag" in cf_set or "is_revenge_game" in cf_set
            or re.search(r"\brevenge\b|\bformer.team\b", text)):
        _any_filter_matched = True
        if not game_context.get("is_revenge"):
            return False

    # ── Playoff standing / clinched / eliminated / bubble filter ──
    if ("playoff_standing" in cf_set
            or re.search(r"\bclinch|\beliminated\b|\btanking\b|\bplayoff.(?:race|bubble)\b|\bdesperate\b|\bbubble\b|\bmust.win\b", text)):
        _any_filter_matched = True
        home_wp = game_context.get("home_win_pct", 0.5)
        away_wp = game_context.get("away_win_pct", 0.5)

        if re.search(r"\bclinch", text):
            # 65%+ win pct = likely clinched (top ~6 teams per conference)
            # Previous 60% was too loose — captured mid-tier teams
            if home_wp < 0.65 and away_wp < 0.65:
                return False
        elif re.search(r"\beliminated\b|\btanking\b", text):
            # 35%- win pct = likely eliminated/tanking
            # Previous 40% was too loose — captured mediocre teams
            if home_wp > 0.35 and away_wp > 0.35:
                return False
        elif re.search(r"\bbubble\b|\bdesperate\b|\bmust.win\b|\bplayoff.race\b", text):
            # Bubble/desperate = at least one team in tight playoff fight
            # Narrowed from 40-60% to 43-57% to exclude comfortable mid-table
            if not (0.43 <= home_wp <= 0.57 or 0.43 <= away_wp <= 0.57):
                return False

    # ── Both teams short rest filter ──
    if "both_teams_short_rest" in cf_set:
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 99)
        away_rest = game_context.get("away_days_rest", 99)
        if home_rest > 1 or away_rest > 1:
            return False

    # ── Rest mismatch filter ──
    if ("rest_mismatch" in cf_set
            or re.search(r"\brest.(?:mismatch|differential|advantage|edge)\b|\bfresh.vs.tired\b", text)):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 1)
        away_rest = game_context.get("away_days_rest", 1)
        # Extract mismatch threshold from text (e.g., "2+ day rest mismatch")
        mm = re.search(r"(\d)\+?\s*(?:day)?\s*rest", text)
        threshold = int(mm.group(1)) if mm else 2
        if abs(home_rest - away_rest) < threshold:
            return False

    # ── Bad loss / blowout / bounce filter (using prev_margin) ──
    if (re.search(r"\bbad.loss\b|\bblowout(?!.win)\b|\bblown.(?:out|lead)\b|\bbounce\b"
                   r"|\bhangover\b|\bafter.(?:bad|ugly|blowout)", text)):
        _any_filter_matched = True
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team lost their previous game badly (margin < -10)
        if hpm > -10 and apm > -10:
            return False

    # ── Winning streak / dominant win filter (using prev_margin) ──
    if re.search(r"\bwinning.streak\b|\bblowout.win\b|\bdomin\w+.win\b|\bmomentum\b", text):
        _any_filter_matched = True
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team won their previous game convincingly (margin > 10)
        if hpm < 10 and apm < 10:
            return False

    # ── Losing team / struggling team filter ──
    if re.search(r"\blosing.streak\b|\bstruggling\b|\bslumping\b|\bskid\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team is losing AND lost their previous game
        if not ((hwp < 0.45 and hpm < 0) or (awp < 0.45 and apm < 0)):
            return False

    # ── Generic streak filter (bare "streak" without winning/losing qualifier) ──
    if not _any_filter_matched and re.search(r"\bstreak\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        # At least one team has a notably non-average record (on a streak)
        if not (hwp >= 0.58 or hwp <= 0.42 or awp >= 0.58 or awp <= 0.42):
            return False

    # ── Home stand filter ──
    if not _any_filter_matched and re.search(r"\bhome.?stand\b", text):
        _any_filter_matched = True
        # Home stand = home team not on road trip + playing frequently
        home_road = game_context.get("home_road_streak", 0)
        home_g4 = game_context.get("home_games_in_4", 1)
        # Home team must have 0 consecutive road games and 2+ games in 4 days
        if home_road > 0 or home_g4 < 2:
            return False

    # ── Favorite/underdog/dominant/narrative filters ──
    # These patterns exist in _needs_context_filter but previously had no
    # corresponding game-level filter, causing fail-closed (0 events).
    # Use win_pct as proxy: favorites ~55%+, underdogs ~45%-, dominant ~60%+.
    if not _any_filter_matched and re.search(r"\bfavorite\b", text):
        _any_filter_matched = True
        # At least one team must be a clear favorite (high win%)
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp < 0.55 and awp < 0.55:
            return False

    if not _any_filter_matched and re.search(r"\bunderdog\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp > 0.45 and awp > 0.45:
            return False

    if not _any_filter_matched and re.search(r"\bdominant\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp < 0.60 and awp < 0.60:
            return False

    if not _any_filter_matched and re.search(r"\bnarrative\b", text):
        _any_filter_matched = True
        # Narrative games = high-profile matchups with extreme records
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if not (hwp >= 0.58 or hwp <= 0.42 or awp >= 0.58 or awp <= 0.42):
            return False

    # ── Non-game-filterable conditions (fail-closed) ──
    # These patterns describe market-level or venue-level conditions that
    # cannot be evaluated from schedule context. Previously these set
    # _any_filter_matched=True and passed ALL games through, causing the
    # "164 identical events" bug (DW#230). Now they return False so
    # hypotheses with these conditions get 0 events and are rejected for
    # insufficient data. The gate in autonomous.py _phase_evaluate() also
    # prevents new hypotheses with these conditions from entering backtesting
    # without structured game_filters.
    _unfilterable_patterns = [
        r"\baltitud|\belev\w+|\bdenver\b|\bmile.high\b|\bcoors\b",        # venue
        r"\bpacific\b|\beastern\b|\bcentral\b|\btime.?zone\b|\bearly.tip\b|\blate.tip\b",  # timezone
        r"\bclosing.line\b|\bline.?value\b|\bclv\b|\bline.?move\b",       # market dynamics
        r"\bsharp\b|\bsteam\b|\breversal\b|\brlm\b",                      # market signals
        r"\bsecond.half\b|\bfirst.half\b|\bhalf.time\b",                  # market segment
    ]
    for pat in _unfilterable_patterns:
        if not _any_filter_matched and re.search(pat, text):
            # Can't filter at game level — fail closed to prevent identical event sets
            return False

    if not _any_filter_matched:
        # No regex pattern matched the hypothesis text — we can't verify the
        # hypothesis condition for this game.  Fail closed to prevent all
        # games leaking through unfiltered (the "149 identical events" bug).
        return False

    return True


def _needs_context_filter(hypothesis_name: str, thesis: str, config: dict) -> bool:
    """Quick check: does this hypothesis need game-level context filtering?

    Returns True if the hypothesis references any schedule-derivable context
    factor in its name, thesis, or context_factors config, OR has structured
    game_filters that require schedule context to evaluate.
    """
    from tools import backtest_io

    # Structured game_filters are authoritative — always require context
    if config.get("game_filters"):
        return True

    context_factors = config.get("context_factors", [])
    cf_set = {f.lower().replace(" ", "_") for f in context_factors}
    if cf_set & backtest_io.FILTERABLE_CONTEXT_FACTORS:
        return True

    text = f"{hypothesis_name} {thesis or ''}".lower().replace("_", " ").replace("-", " ")
    schedule_patterns = [
        r"\bb2b\b", r"\bback.to.back\b", r"\bdays?.rest\b", r"\bshort.rest\b",
        r"\broad.trip\b", r"\bconsecutive.(?:road|away)\b",
        r"\b3.?in.?4\b", r"\b4.?in.?5\b", r"\bschedule.(?:compress|density)\b",
        r"\bsandwich\b", r"\btrap.game\b",
        r"\brevenge\b", r"\bformer.team\b",
        r"\bclinch", r"\beliminated\b", r"\btanking\b", r"\bplayoff.(?:race|bubble)\b",
        r"\bdesperate\b", r"\bmust.win\b",
        r"\bextra.rest\b", r"\brest.mismatch\b",
        r"\bhomestand\b", r"\bhome.stand\b", r"\bwinning.streak\b", r"\blosing.streak\b",
        r"\bwin.pct\b", r"\bwin.rate\b",
        # Venue/environment context
        r"\baltitud", r"\belev\w+", r"\bdenver\b", r"\bmile.high\b",
        # Time/timezone context
        r"\bpacific\b", r"\beastern\b", r"\bcentral\b", r"\btime.?zone\b", r"\bearly.tip\b", r"\blate.tip\b",
        # Line movement / closing line context
        r"\bclosing.line\b", r"\bline.?value\b", r"\bclv\b", r"\bline.?move\b",
        # Sharp money / steam context
        r"\bsharp\b", r"\bsteam\b", r"\breversal\b", r"\brlm\b",
        # Half-specific context
        r"\bsecond.half\b", r"\bfirst.half\b", r"\bhalf.time\b",
    ]
    return any(re.search(p, text) for p in schedule_patterns)
