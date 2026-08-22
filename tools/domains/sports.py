"""
Sports domain plugin — betting becomes "first among equals", not the default
payload (DOMAIN_GENERALITY §2b). Registers the 21 odds/scanner tools, the
sports freshness rules (formerly the hardcoded _SPORTS_FRESHNESS_PATTERN),
and the sports tool dispatcher that used to live as an if/elif chain in
orchestrator._execute_tool.

The orchestrator no longer imports odds modules at module load for its own
dispatch — this plugin does. Sports behavior is unchanged; a financial or
literature query simply no longer receives these tools in its prompt.
"""

import re

from tools.domain_registry import DomainPlugin

# ── Freshness rules (moved verbatim from orchestrator) ────────────────────
_FRESHNESS_RULES = [
    # Rosters/players/teams change; default to past month to avoid stale data.
    (
        re.compile(
            r"\b(roster|player|team|lineup|starter|injury|trade|return|"
            r"celtics|lakers|warriors|nets|knicks|bulls|heat|bucks|"
            r"nba|nfl|mlb|nhl|ncaa|wnba|pga|"
            r"prop|props|betting|edge|splits|stats)\b",
            re.IGNORECASE,
        ),
        "pm",
    ),
]


def build_sports_plugin(tool_schemas: list, execute) -> DomainPlugin:
    """Factory keeps this module free of orchestrator imports at module load;
    the orchestrator passes its existing ODDS_TOOLS schemas + dispatcher."""
    return DomainPlugin(
        name="sports",
        domains=set(),          # sports is not an AGP Domain value — match on keywords
        keywords=re.compile(
            r"\b(odds?|bet|betting|wager|parlay|spread|total|moneyline|h2h|"
            r"props?|bookmaker|bankroll|kelly|edge scan|clv|devig|"
            r"nba|nfl|mlb|nhl|ncaa|wnba|pga|sport)\b",
            re.IGNORECASE,
        ),
        tool_schemas=tool_schemas,
        freshness=_FRESHNESS_RULES,
        execute=execute,
    )
