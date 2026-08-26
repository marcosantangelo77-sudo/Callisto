"""
Team-name normalization and fuzzy matching across data sources.

Extracted verbatim from tools/backtest_io.py.
"""


# Canonical team alias map — maps any known variation to a single key.
# Covers MLB, NBA, NFL, NHL. Keys are lowercase.
_TEAM_ALIASES: dict[str, str] = {}

def _build_alias_map() -> dict[str, str]:
    """Build a comprehensive alias -> canonical name mapping."""
    # Each entry: canonical name -> list of known aliases
    teams = {
        # ── MLB ──
        "arizona diamondbacks": ["az diamondbacks", "ari diamondbacks", "d-backs", "dbacks"],
        "atlanta braves": ["atl braves"],
        "baltimore orioles": ["bal orioles", "balt orioles"],
        "boston red sox": ["bos red sox", "redsox"],
        "chicago cubs": ["chi cubs", "chc cubs"],
        "chicago white sox": ["chi white sox", "chw white sox", "chi sox", "whitesox"],
        "cincinnati reds": ["cin reds", "cincy reds"],
        "cleveland guardians": ["cle guardians", "cleveland indians", "cle indians"],
        "colorado rockies": ["col rockies", "colo rockies"],
        "detroit tigers": ["det tigers"],
        "houston astros": ["hou astros"],
        "kansas city royals": ["kc royals"],
        "los angeles angels": ["la angels", "anaheim angels", "laa angels", "angels"],
        "los angeles dodgers": ["la dodgers", "lad dodgers"],
        "miami marlins": ["mia marlins", "fla marlins", "florida marlins"],
        "milwaukee brewers": ["mil brewers"],
        "minnesota twins": ["min twins"],
        "new york mets": ["ny mets", "nym mets"],
        "new york yankees": ["ny yankees", "nyy yankees"],
        "athletics": ["oakland athletics", "oakland a's", "oak athletics", "a's", "as"],
        "philadelphia phillies": ["phi phillies", "philly phillies", "phl phillies"],
        "pittsburgh pirates": ["pit pirates", "pitt pirates"],
        "san diego padres": ["sd padres"],
        "san francisco giants": ["sf giants"],
        "seattle mariners": ["sea mariners"],
        "st. louis cardinals": ["stl cardinals", "st louis cardinals", "saint louis cardinals"],
        "tampa bay rays": ["tb rays"],
        "texas rangers": ["tex rangers"],
        "toronto blue jays": ["tor blue jays", "blue jays"],
        "washington nationals": ["was nationals", "wsh nationals", "nats"],
        # ── NBA ──
        "atlanta hawks": ["atl hawks"],
        "boston celtics": ["bos celtics"],
        "brooklyn nets": ["bkn nets", "bk nets"],
        "charlotte hornets": ["cha hornets", "char hornets"],
        "chicago bulls": ["chi bulls"],
        "cleveland cavaliers": ["cle cavaliers", "cle cavs", "cavs"],
        "dallas mavericks": ["dal mavericks", "dal mavs", "mavs"],
        "denver nuggets": ["den nuggets"],
        "detroit pistons": ["det pistons"],
        "golden state warriors": ["gs warriors", "gsw warriors"],
        "houston rockets": ["hou rockets"],
        "indiana pacers": ["ind pacers"],
        "los angeles clippers": ["la clippers", "lac clippers"],
        "los angeles lakers": ["la lakers", "lal lakers"],
        "memphis grizzlies": ["mem grizzlies"],
        "miami heat": ["mia heat"],
        "milwaukee bucks": ["mil bucks"],
        "minnesota timberwolves": ["min timberwolves", "min wolves", "t-wolves"],
        "new orleans pelicans": ["no pelicans", "nop pelicans", "nola pelicans"],
        "new york knicks": ["ny knicks", "nyk knicks"],
        "oklahoma city thunder": ["okc thunder"],
        "orlando magic": ["orl magic"],
        "philadelphia 76ers": ["phi 76ers", "philly 76ers", "philadelphia sixers", "phi sixers", "sixers"],
        "phoenix suns": ["phx suns"],
        "portland trail blazers": ["por trail blazers", "portland blazers", "por blazers", "blazers"],
        "sacramento kings": ["sac kings"],
        "san antonio spurs": ["sa spurs"],
        "toronto raptors": ["tor raptors"],
        "utah jazz": ["uta jazz"],
        "washington wizards": ["was wizards", "wsh wizards"],
        # ── NFL ──
        "arizona cardinals": ["az cardinals", "ari cardinals"],
        "atlanta falcons": ["atl falcons"],
        "baltimore ravens": ["bal ravens", "balt ravens"],
        "buffalo bills": ["buf bills"],
        "carolina panthers": ["car panthers"],
        "chicago bears": ["chi bears"],
        "cincinnati bengals": ["cin bengals", "cincy bengals"],
        "cleveland browns": ["cle browns"],
        "dallas cowboys": ["dal cowboys"],
        "denver broncos": ["den broncos"],
        "detroit lions": ["det lions"],
        "green bay packers": ["gb packers"],
        "houston texans": ["hou texans"],
        "indianapolis colts": ["ind colts", "indy colts"],
        "jacksonville jaguars": ["jax jaguars", "jac jaguars"],
        "kansas city chiefs": ["kc chiefs"],
        "las vegas raiders": ["lv raiders", "oakland raiders", "oak raiders"],
        "los angeles chargers": ["la chargers", "lac chargers", "san diego chargers", "sd chargers"],
        "los angeles rams": ["la rams", "lar rams", "st. louis rams", "stl rams"],
        "miami dolphins": ["mia dolphins"],
        "minnesota vikings": ["min vikings"],
        "new england patriots": ["ne patriots", "nep patriots", "pats"],
        "new orleans saints": ["no saints", "nola saints"],
        "new york giants": ["ny giants", "nyg giants"],
        "new york jets": ["ny jets", "nyj jets"],
        "philadelphia eagles": ["phi eagles", "philly eagles"],
        "pittsburgh steelers": ["pit steelers", "pitt steelers"],
        "san francisco 49ers": ["sf 49ers", "niners"],
        "seattle seahawks": ["sea seahawks"],
        "tampa bay buccaneers": ["tb buccaneers", "tb bucs", "bucs"],
        "tennessee titans": ["ten titans"],
        "washington commanders": ["was commanders", "wsh commanders", "washington football team"],
        # ── NHL ──
        "anaheim ducks": ["ana ducks"],
        "boston bruins": ["bos bruins"],
        "buffalo sabres": ["buf sabres"],
        "calgary flames": ["cgy flames", "cal flames"],
        "carolina hurricanes": ["car hurricanes", "canes"],
        "chicago blackhawks": ["chi blackhawks"],
        "colorado avalanche": ["col avalanche", "avs"],
        "columbus blue jackets": ["cbj blue jackets", "blue jackets"],
        "dallas stars": ["dal stars"],
        "detroit red wings": ["det red wings"],
        "edmonton oilers": ["edm oilers"],
        "florida panthers": ["fla panthers"],
        "los angeles kings": ["la kings", "lak kings"],
        "minnesota wild": ["min wild"],
        "montreal canadiens": ["mtl canadiens", "canadiens", "habs"],
        "nashville predators": ["nsh predators", "nas predators", "preds"],
        "new jersey devils": ["nj devils", "njd devils"],
        "new york islanders": ["ny islanders", "nyi islanders"],
        "new york rangers": ["ny rangers", "nyr rangers"],
        "ottawa senators": ["ott senators", "sens"],
        "philadelphia flyers": ["phi flyers", "philly flyers"],
        "pittsburgh penguins": ["pit penguins", "pitt penguins", "pens"],
        "san jose sharks": ["sj sharks"],
        "seattle kraken": ["sea kraken"],
        "st. louis blues": ["stl blues", "st louis blues", "saint louis blues"],
        "tampa bay lightning": ["tb lightning", "tbl lightning", "bolts"],
        "toronto maple leafs": ["tor maple leafs", "leafs"],
        "utah mammoth": ["uta mammoth", "utah hockey club", "utah hc"],
        "vancouver canucks": ["van canucks"],
        "vegas golden knights": ["vgk golden knights", "vegas knights", "golden knights"],
        "washington capitals": ["was capitals", "wsh capitals", "caps"],
        "winnipeg jets": ["wpg jets"],
    }

    alias_map: dict[str, str] = {}
    for canonical, aliases in teams.items():
        alias_map[canonical] = canonical
        for alias in aliases:
            alias_map[alias] = canonical
    return alias_map

def _normalize_team(name: str) -> str:
    """Normalize team name for fuzzy matching across data sources.

    Uses a canonical alias map for exact lookups, then falls back to
    city-abbreviation replacement for unknown names.

    Handles differences between Odds API names (e.g. "Los Angeles Dodgers")
    and ESPN names (e.g. "LA Dodgers", "Athletics", etc.).
    """
    if not name:
        return ""
    n = name.strip().lower()
    # Remove trailing periods from abbreviations (e.g. "St." -> "st")
    n = " ".join(n.split())

    # Build alias map once (lazy singleton).
    # Build into a local first, then atomically assign — this guarantees
    # readers never see a partially-built dict (race-safe with GIL).
    alias_map = _TEAM_ALIASES
    if not alias_map:
        alias_map = _build_alias_map()
        _TEAM_ALIASES.clear()
        _TEAM_ALIASES.update(alias_map)

    # Direct alias lookup
    if n in alias_map:
        return alias_map[n]

    # Fallback: city abbreviation replacement for unknown names
    city_replacements = {
        "los angeles": "la",
        "new york": "ny",
        "san francisco": "sf",
        "san antonio": "sa",
        "san diego": "sd",
        "golden state": "gs",
        "oklahoma city": "okc",
        "portland trail blazers": "portland blazers",
        "brooklyn": "bkn",
        "saint louis": "st. louis",
        "st louis": "st. louis",
    }
    for full, abbrev in city_replacements.items():
        if n.startswith(full):
            n = abbrev + n[len(full):]
            break
    n = " ".join(n.split())
    return n

def _team_matches(name_a: str, name_b: str) -> bool:
    """Check if two team names refer to the same team.

    Uses canonical alias resolution first, then falls back to
    mascot matching and substring containment.
    """
    if not name_a or not name_b:
        return False
    if name_a == name_b:
        return True

    a = _normalize_team(name_a)
    b = _normalize_team(name_b)

    if a == b:
        return True

    # Last word (mascot) match — "LA Dodgers" vs "Los Angeles Dodgers"
    # Only match if mascot has 4+ chars to avoid false positives
    a_last = a.rsplit(None, 1)[-1] if a else ""
    b_last = b.rsplit(None, 1)[-1] if b else ""
    if a_last == b_last and len(a_last) > 3:
        return True

    # Substring: "Athletics" matches "Oakland Athletics" or "Athletics"
    if len(a) > 3 and len(b) > 3 and (a in b or b in a):
        return True

    return False
