"""
Women's Sports Identity & Cultural Cohesion Hypotheses

Extends the TCI (Team Cohesion Index) work into identity/cultural/demographic
dimensions that the market doesn't price. The TCI backtest on 52 NCAAW games
showed decomposed signals beat composite: experience ratio (59.6%) and stability
score (57.7%) are real edges. This script pushes deeper into WHY those signals
work — the cultural, regional, demographic, and institutional factors that
create or destroy team chemistry in women's sports.

THESIS: Women's sports markets are thin (less liquid, less sharp money, less
analytical attention). Identity/cultural factors that create measurable team
chemistry effects persist as pricing inefficiencies far longer than in men's
markets. The composite "identity mesh" may be flat (like composite TCI was),
but decomposed sub-signals should carry.

Generates 35+ hypotheses across:
  - Regional/geographic identity
  - Demographic composition & heterogeneity
  - Religious/institutional identity
  - Identity mesh / interaction effects
  - WNBA extensions
  - Market structure edges
  - Cross-sport women's hypotheses
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# HYPOTHESIS DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

WOMENS_IDENTITY_HYPOTHESES = [

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 1: REGIONAL / GEOGRAPHIC IDENTITY
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_geographic_clustering_early_season_ats",
        "thesis": (
            "NCAAW teams with high geographic clustering (60%+ of roster from "
            "the same US region) cover the spread in the first 6 weeks of the "
            "season. The mechanism: players from the same region share cultural "
            "norms, communication styles, and basketball idioms that accelerate "
            "early-season chemistry formation. This edge should decay as all "
            "teams gel over the season. The prior geographic_concentration_ats "
            "hypothesis tested season-long and was flat — this isolates the "
            "TEMPORAL window where regional homogeneity matters most. "
            "DATA SOURCE: ESPN roster API hometown/state fields (already collected "
            "by tci_scraper.py STATE_REGIONS mapping). Filter to Nov-Dec games."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "geographic_clustering",
            "temporal_window": "early_season",
            "weeks": 6,
            "min_concentration": 0.60,
            "data_source": "espn_roster_api",
            "fields_needed": ["home_state", "home_country"],
            "context_factors": [
                "geographic_concentration",
                "top_region",
                "weeks_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Extension of rejected wcbb_geographic_concentration_ats. "
            "That tested full-season; this isolates early-season temporal window "
            "where regional homogeneity should matter most for chemistry formation."
        ),
    },
    {
        "name": "ncaaw_in_state_recruiting_ats",
        "thesis": (
            "NCAAW teams where 50%+ of the roster is from the same state as the "
            "school cover the spread, particularly in home games. In-state players "
            "have existing local fan/family support networks that boost performance, "
            "and they chose the school for cultural fit — not just athletics. This "
            "creates stronger program identity than nationally-recruited rosters. "
            "DATA SOURCE: ESPN roster API home_state field vs school state. Already "
            "available via tci_scraper.py. Calculate in_state_pct per team."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "in_state_recruiting_pct",
            "min_in_state_pct": 0.50,
            "data_source": "espn_roster_api",
            "fields_needed": ["home_state", "school_state"],
            "context_factors": [
                "in_state_percentage",
                "home_away",
                "state_concentration",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Testable now with existing TCI scraper data. State-level recruiting "
            "concentration as identity signal. Strongest for mid-major programs "
            "where in-state recruiting IS the identity (Iowa, UConn, etc.)."
        ),
    },
    {
        "name": "ncaaw_sec_acc_regional_identity_road_ats",
        "thesis": (
            "SEC and ACC women's basketball teams with strong Southern regional "
            "identity (70%+ roster from Southeast region) outperform ATS in "
            "tournament/neutral-site games played away from their region. The "
            "mechanism: strong regional identity creates an us-vs-them mentality "
            "that bonds the team when displaced from home culture. This is the "
            "'cultural siege' effect — teams with strong shared identity perform "
            "better under adversity than teams with diffuse identity. "
            "DATA SOURCE: ESPN roster home_state mapped to Southeast region. "
            "Conference affiliation from team info API. Tournament game locations."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "regional_identity_away_performance",
            "conferences": ["SEC", "ACC"],
            "min_regional_concentration": 0.70,
            "region": "Southeast",
            "game_filter": "away_or_neutral",
            "data_source": "espn_roster_api + game_location",
            "context_factors": [
                "geographic_concentration",
                "conference",
                "game_location_region",
                "team_home_region",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Novel hypothesis. Tests whether strong regional cultural identity "
            "creates a cohesion advantage specifically in away/neutral contexts. "
            "SEC women's basketball has the most pronounced regional identity."
        ),
    },
    {
        "name": "ncaaw_urban_rural_culture_clash_ats",
        "thesis": (
            "When a team dominated by players from major metro areas (population "
            "500k+) faces a team dominated by players from small-town/rural areas, "
            "the cultural mismatch creates different game dynamics. Urban-roster "
            "teams play faster, more individualistic basketball; rural-roster teams "
            "play more structured, team-oriented basketball. In close games (spread "
            "<7), rural-roster teams should cover more often due to better execution "
            "under pressure and less hero-ball tendencies. "
            "DATA SOURCE: ESPN roster hometown field cross-referenced with US Census "
            "metro area population data. Requires external population lookup for "
            "each player's hometown. Not currently in system — needs data collection."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "urban_rural_composition",
            "spread_filter": {"max_abs_spread": 7},
            "data_source": "espn_roster_api + us_census_population",
            "data_available": False,
            "fields_needed": [
                "hometown",
                "hometown_population",
                "metro_classification",
            ],
            "context_factors": [
                "urban_player_pct",
                "rural_player_pct",
                "matchup_urban_rural_differential",
                "spread",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Requires external data collection (Census population by city). "
            "Novel hypothesis based on play-style cultural differences. "
            "Classify hometowns as urban (500k+ metro), suburban (50k-500k), "
            "or rural (<50k). Test urban-dominant vs rural-dominant matchups."
        ),
    },
    {
        "name": "ncaaw_military_academy_ats_consistency",
        "thesis": (
            "Women's teams at military-connected schools (Army, Navy, Air Force) "
            "show lower variance in ATS performance — they cover close to 50% but "
            "with significantly less volatility than non-military programs. The "
            "institutional discipline structure creates consistent but not "
            "exceptional performance. The edge is in TOTALS: military academy "
            "games should hit unders at a higher rate due to disciplined defensive "
            "play and controlled pace. "
            "DATA SOURCE: Hardcoded list of military academies. Historical ATS "
            "and totals results from odds providers. Readily available."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "totals",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "military_institutional_discipline",
            "schools": ["Army", "Navy", "Air Force"],
            "side_filter": "Under",
            "data_source": "odds_api + hardcoded_school_list",
            "data_available": True,
            "context_factors": [
                "is_military_academy",
                "total_line",
                "opponent_pace",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Small sample (3 schools). May need to combine across seasons. "
            "Military discipline hypothesis is testable with existing odds data. "
            "Extension: test if this holds for men's military academy teams too."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 2: DEMOGRAPHIC COMPOSITION & HETEROGENEITY
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_roster_homogeneity_early_vs_late_ats",
        "thesis": (
            "NCAAW teams with high demographic homogeneity (measured by the "
            "Herfindahl-Hirschman Index of roster diversity across race/ethnicity "
            "categories) gel faster early-season but plateau or regress late-season. "
            "Diverse teams start slower but peak in March. The mechanism: homogeneous "
            "groups form social bonds faster (similarity-attraction theory) but have "
            "narrower strategic/creative toolkits. Diverse groups take longer to "
            "integrate but produce more adaptive, resilient team dynamics under "
            "tournament pressure. Bet homogeneous teams early-season, diverse teams "
            "in March. "
            "DATA SOURCE: Player headshot analysis for demographic classification "
            "(ethically fraught — prefer self-reported data from school media guides "
            "or NCAA demographic reports). NCAA publishes aggregate team-level "
            "demographic data by school which avoids individual classification."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "demographic_homogeneity_temporal",
            "temporal_split": {
                "early": "weeks_1_to_8",
                "late": "weeks_16_to_postseason",
            },
            "data_source": "ncaa_demographic_reports + school_media_guides",
            "data_available": False,
            "fields_needed": [
                "team_demographic_hhi",
                "diversity_index",
                "weeks_into_season",
            ],
            "context_factors": [
                "demographic_hhi",
                "season_phase",
                "opponent_demographic_hhi",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 50,
        "significance_level": 0.10,
        "notes": (
            "Requires NCAA aggregate demographic data (publicly available in annual "
            "reports by school). Use team-level aggregate, NOT individual player "
            "classification. Ethically framed as composition heterogeneity research, "
            "consistent with organizational behavior literature."
        ),
    },
    {
        "name": "ncaaw_international_player_adjustment_period",
        "thesis": (
            "NCAAW teams with 3+ international players (non-USA home_country) "
            "underperform ATS in the first 8 games of the season but outperform "
            "ATS from January onward. International players bring diverse skills "
            "but face cultural adjustment, language barriers, and different "
            "basketball systems that slow early-season integration. By January, "
            "integration is complete and the diverse skill set becomes an advantage. "
            "Bet AGAINST high-international teams in November; bet ON them in "
            "February/March. "
            "DATA SOURCE: ESPN roster API home_country field. Already collected by "
            "tci_scraper.py. Filter for non-USA countries."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "international_player_adjustment",
            "min_international_players": 3,
            "temporal_split": {
                "fade_period": "first_8_games",
                "back_period": "january_onward",
            },
            "data_source": "espn_roster_api",
            "data_available": True,
            "fields_needed": ["home_country"],
            "context_factors": [
                "international_count",
                "international_pct",
                "games_played",
                "month",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Testable NOW with existing tci_scraper.py data. home_country field "
            "identifies international players. Hypothesis predicts early-season "
            "adjustment period followed by skill diversity advantage. Two-phase "
            "test: fade early, back late."
        ),
    },
    {
        "name": "ncaaw_diversity_vs_homogeneity_tournament_ats",
        "thesis": (
            "In the NCAA Women's Tournament specifically, teams with higher roster "
            "diversity (measured by international player %, multi-region geographic "
            "distribution, and class year spread) outperform ATS. The tournament "
            "demands adaptability across 3-6 games against varied opponents — diverse "
            "rosters have a broader strategic toolkit. Homogeneous teams that dominate "
            "regular season conference play (against familiar opponents) face a "
            "diversity disadvantage when confronting unfamiliar styles. "
            "DATA SOURCE: ESPN roster API (hometown, home_country, class_year) — "
            "all available now. Construct diversity index from multiple dimensions."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "roster_diversity_tournament",
            "game_filter": "ncaa_tournament_only",
            "diversity_components": [
                "geographic_spread",
                "international_pct",
                "class_year_balance",
            ],
            "data_source": "espn_roster_api",
            "data_available": True,
            "context_factors": [
                "diversity_index",
                "geographic_spread",
                "international_pct",
                "class_balance",
                "tournament_round",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Testable with existing data. Directly extends TCI backtest "
            "(52 tournament games). Composite diversity index from: geographic "
            "entropy, international %, class year balance. Test against DK "
            "closing lines from import_ncaaw_closing_lines.py."
        ),
    },
    {
        "name": "ncaaw_demographic_mismatch_totals",
        "thesis": (
            "When two NCAAW teams with very different demographic compositions "
            "(high differential in diversity index) meet, games go OVER the total "
            "more often. The mechanism: teams with different cultural backgrounds "
            "play different styles, creating pace mismatches and defensive confusion "
            "that inflate scoring. Homogeneous-vs-homogeneous and diverse-vs-diverse "
            "matchups are more predictable (defense adjusts to similar style). "
            "DATA SOURCE: Composite diversity index (geographic + international + "
            "class balance) — available from ESPN roster API."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "totals",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "demographic_mismatch_totals",
            "side_filter": "Over",
            "min_diversity_differential": 0.20,
            "data_source": "espn_roster_api",
            "data_available": True,
            "context_factors": [
                "team_a_diversity_index",
                "team_b_diversity_index",
                "diversity_differential",
                "total_line",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Novel hypothesis. Style-clash theory applied through demographic "
            "composition lens. Test with existing roster data and historical "
            "totals results. Could be the first totals-side identity hypothesis."
        ),
    },
    {
        "name": "ncaaw_gender_dynamics_vs_ncaab_differential",
        "thesis": (
            "The same team composition factors (experience ratio, stability score, "
            "geographic concentration) have LARGER predictive effects in NCAAW than "
            "NCAAB. Academic meta-analyses (Carron et al. 2002, Eys et al. 2015) "
            "show women's teams are more affected by interpersonal dynamics than "
            "men's teams. If true, the same TCI signals should produce stronger "
            "edges in women's lines. This is a META-hypothesis: compare signal "
            "strength of experience_ratio and stability_score between NCAAW and "
            "NCAAB tournament ATS outcomes. "
            "DATA SOURCE: ESPN roster API for both men's and women's rosters. "
            "DraftKings closing lines for both tournaments. Requires collecting "
            "NCAAB roster data (not currently in system)."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "meta_comparison",
            "signal": "gender_differential_signal_strength",
            "compare_sports": ["basketball_ncaaw", "basketball_ncaab"],
            "signals_to_compare": ["experience_ratio", "stability_score"],
            "data_source": "espn_roster_api + odds_api",
            "data_available": False,
            "fields_needed": [
                "ncaab_rosters",
                "ncaab_closing_lines",
            ],
            "context_factors": [
                "sport_gender",
                "signal_name",
                "effect_size",
                "sample_size",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 50,
        "significance_level": 0.10,
        "notes": (
            "META-hypothesis. If women's teams respond more to interpersonal "
            "dynamics (per academic literature), identity signals should be "
            "systematically stronger in NCAAW than NCAAB. Requires NCAAB data "
            "collection. High value if confirmed — justifies entire women's "
            "sports identity research program."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 3: RELIGIOUS / INSTITUTIONAL IDENTITY
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_religious_affiliation_tournament_composure",
        "thesis": (
            "Women's basketball teams at religious-affiliated schools (Catholic, "
            "LDS, Baptist — see RELIGIOUS_PROGRAMS in tci_scraper.py) perform "
            "better ATS in high-pressure tournament games compared to secular "
            "schools, controlling for seed and experience. Shared institutional "
            "values create a composure advantage — players have a framework for "
            "handling pressure that extends beyond basketball. The effect is "
            "strongest for underdogs (+3 or more). "
            "DATA SOURCE: RELIGIOUS_PROGRAMS dict in tci_scraper.py already maps "
            "schools to affiliation. NCAA tournament results from odds API. "
            "Ready to test now."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "religious_affiliation_composure",
            "game_filter": "ncaa_tournament_only",
            "underdog_filter": {"min_spread": 3},
            "data_source": "tci_scraper.RELIGIOUS_PROGRAMS + odds_api",
            "data_available": True,
            "context_factors": [
                "religious_affiliation",
                "is_religious_school",
                "spread",
                "tournament_round",
                "seed",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Testable now. RELIGIOUS_PROGRAMS already in tci_scraper.py. "
            "Filter to tournament underdogs. Institutional values → composure "
            "under pressure. Extends institutional_factor from TCI formula "
            "(currently only 10% weight — this tests it in isolation)."
        ),
    },
    {
        "name": "ncaaw_notre_dame_byu_effect_ats",
        "thesis": (
            "Schools with the STRONGEST religious/institutional identity (Notre Dame, "
            "BYU, Gonzaga, Baylor) have women's basketball programs that outperform "
            "ATS in the second half of the season and postseason. These schools have "
            "the most cohesive institutional cultures in college sports, with shared "
            "values that extend beyond athletics. The 'identity premium' should be "
            "largest in high-pressure late-season situations. "
            "DATA SOURCE: Hardcoded tier-1 religious identity schools. Historical "
            "ATS from odds providers. Simple to test."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "elite_religious_identity",
            "tier_1_schools": ["Notre Dame", "BYU", "Gonzaga", "Baylor"],
            "temporal_window": "second_half_and_postseason",
            "data_source": "hardcoded_schools + odds_api",
            "data_available": True,
            "context_factors": [
                "school_name",
                "religious_tier",
                "season_phase",
                "is_postseason",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 20,
        "significance_level": 0.10,
        "notes": (
            "Very small N per school per season. May need 3+ seasons of data. "
            "But hypothesis is concrete and easily testable. Notre Dame and "
            "BYU women's programs have historically outperformed seeds."
        ),
    },
    {
        "name": "ncaaw_jesuit_school_collective_ats",
        "thesis": (
            "Women's basketball teams at Jesuit universities (Georgetown, Gonzaga, "
            "Marquette, Creighton, Xavier, Loyola, Holy Cross) have a collective "
            "institutional ethos ('men and women for others') that translates to "
            "more team-oriented play. This creates an ATS advantage when matched "
            "against programs with more individualistic cultures (schools known for "
            "producing WNBA individual stars). "
            "DATA SOURCE: Hardcoded Jesuit school list (well-defined). Assist "
            "rate and ball-movement statistics from ESPN box scores could validate "
            "the mechanism (but ATS test doesn't require it)."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "jesuit_collective_identity",
            "jesuit_schools": [
                "Georgetown", "Gonzaga", "Marquette", "Creighton",
                "Xavier", "Loyola", "Holy Cross", "Seton Hall",
                "St. John's", "DePaul",
            ],
            "data_source": "hardcoded_schools + odds_api",
            "data_available": True,
            "context_factors": [
                "is_jesuit_school",
                "opponent_type",
                "assist_rate",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "10 Jesuit schools = decent sample across seasons. Team-oriented "
            "institutional culture as cohesion proxy. Easy to test with ATS "
            "data. Mechanism is that Jesuit education emphasizes communal "
            "values which translate to team-first basketball."
        ),
    },
    {
        "name": "ncaaw_hbcu_cultural_identity_ats",
        "thesis": (
            "Women's basketball teams at HBCUs (Historically Black Colleges and "
            "Universities) carry a strong cultural identity and community bond "
            "that creates cohesion advantages in specific contexts — particularly "
            "when playing as underdogs against Power 5 schools. The HBCU team "
            "identity is a galvanizing force under adversity. Test HBCU programs "
            "ATS as underdogs (+8 or more) in early-season games against P5. "
            "DATA SOURCE: HBCU list is well-defined (107 schools). Historical "
            "ATS from odds providers. Note: HBCU games often have very thin "
            "or no betting lines available — sample may be small."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "hbcu_cultural_identity",
            "underdog_filter": {"min_spread": 8},
            "opponent_filter": "power_5",
            "data_source": "hbcu_school_list + odds_api",
            "data_available": False,
            "fields_needed": [
                "hbcu_classification",
                "opponent_conference",
                "spread",
            ],
            "context_factors": [
                "is_hbcu",
                "spread",
                "opponent_conference_tier",
                "game_location",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 20,
        "significance_level": 0.10,
        "notes": (
            "Data availability is the main concern — many HBCU games don't have "
            "betting lines. Need to verify DraftKings/odds API coverage of HBCU "
            "games. If lines exist, the cultural identity thesis is strong and "
            "completely unpriced by the market."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 4: IDENTITY MESH / INTERACTION EFFECTS
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_three_factor_identity_model_ats",
        "thesis": (
            "A three-factor interaction model combining regional identity "
            "(geographic concentration), experience level (experience ratio), and "
            "coaching stability (tenure years) predicts NCAAW ATS outcomes better "
            "than any single factor. The interaction term captures the SYNERGY: "
            "experienced players from the same region under a long-tenured coach "
            "create a multiplicative cohesion effect. When all three factors favor "
            "one team, the edge should be 5%+ above baseline. "
            "DATA SOURCE: All three factors already computed by tci_scraper.py. "
            "This is a pure model architecture change — no new data needed."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "three_factor_interaction",
            "factors": [
                "geographic_concentration",
                "experience_ratio",
                "coaching_tenure_years",
            ],
            "interaction_type": "multiplicative",
            "data_source": "tci_scraper.py (all fields available)",
            "data_available": True,
            "context_factors": [
                "geographic_concentration",
                "experience_ratio",
                "coaching_tenure_years",
                "three_factor_score",
                "three_factor_differential",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Key hypothesis: tests whether interaction effects exist beyond "
            "the decomposed single-factor signals. The original composite TCI "
            "was flat — but it used ADDITIVE weighting. A MULTIPLICATIVE "
            "interaction model may capture synergies the additive model missed."
        ),
    },
    {
        "name": "ncaaw_culture_fit_incoming_class_ats",
        "thesis": (
            "NCAAW teams where the incoming class (freshmen + transfers) closely "
            "matches the existing roster's geographic/demographic profile cover "
            "the spread at a higher rate in the first half of the season. 'Culture "
            "fit' — measured by similarity between incoming players' home states/ "
            "regions and the existing roster's geographic distribution — accelerates "
            "integration. High culture-fit teams skip the adjustment period. "
            "DATA SOURCE: ESPN roster API with class_year filtering. Compare "
            "freshmen home_states to upperclassmen home_states. Available now."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "culture_fit_incoming_class",
            "temporal_window": "first_half_season",
            "data_source": "espn_roster_api",
            "data_available": True,
            "fields_needed": [
                "class_year",
                "home_state",
                "home_country",
            ],
            "context_factors": [
                "culture_fit_score",
                "incoming_class_size",
                "geographic_similarity_index",
                "weeks_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Testable now. Compute geographic distribution of upperclassmen "
            "vs freshmen/transfers. High overlap = high culture fit. "
            "Cosine similarity between regional distributions of the two groups."
        ),
    },
    {
        "name": "ncaaw_transfer_portal_identity_disruption_ats",
        "thesis": (
            "NCAAW teams that added 3+ transfer players in the offseason "
            "underperform ATS in the first 10 games of the season. The transfer "
            "portal disrupts team identity — incoming transfers carry the culture "
            "and habits of their previous program, creating identity friction. "
            "This is an extension of the TCI continuity_proxy signal, but focused "
            "specifically on the IDENTITY disruption (not just the skill adjustment). "
            "Teams with transfers from the SAME conference should show less disruption "
            "than teams with transfers from different conferences/regions. "
            "DATA SOURCE: ESPN roster API class_year='transfer' or years_exp "
            "anomalies. May need transfer portal databases (247Sports, On3) "
            "for accurate transfer counts. Partially available."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "transfer_portal_identity_disruption",
            "min_transfers": 3,
            "temporal_window": "first_10_games",
            "data_source": "espn_roster_api + transfer_portal_databases",
            "data_available": False,
            "fields_needed": [
                "transfer_status",
                "previous_school",
                "previous_conference",
                "current_conference",
            ],
            "context_factors": [
                "transfer_count",
                "transfer_conference_match_pct",
                "games_played",
                "continuity_proxy",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Transfer portal data requires additional collection (247Sports/On3). "
            "ESPN roster sometimes flags transfers via class year anomalies. "
            "The freshman count in TCI is a rough proxy — this refines it."
        ),
    },
    {
        "name": "ncaaw_identity_mesh_composite_vs_decomposed",
        "thesis": (
            "A composite 'identity mesh score' combining geographic concentration, "
            "demographic homogeneity, institutional values alignment, and incoming "
            "class culture fit performs WORSE than decomposed individual signals — "
            "mirroring the TCI finding where composite was flat but components "
            "carried signal. This META-hypothesis predicts that identity factors "
            "are independent and should NOT be combined into a single score. If "
            "confirmed, each identity dimension gets its own hypothesis and betting "
            "signal rather than a blended model. "
            "DATA SOURCE: All available identity factors from this hypothesis set."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "meta_comparison",
            "signal": "composite_vs_decomposed_identity",
            "composite_factors": [
                "geographic_concentration",
                "demographic_hhi",
                "institutional_factor",
                "culture_fit_score",
            ],
            "data_source": "internal_hypothesis_results",
            "data_available": False,
            "context_factors": [
                "composite_identity_score",
                "decomposed_signals",
                "comparison_metric",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 50,
        "significance_level": 0.10,
        "notes": (
            "META-hypothesis. Can only be tested after individual identity "
            "signals have been backtested. This determines the modeling "
            "architecture: single blended score vs independent signal portfolio."
        ),
    },
    {
        "name": "ncaaw_coaching_identity_match_ats",
        "thesis": (
            "NCAAW teams where the head coach's background (region, alma mater "
            "conference) matches the team's geographic/cultural identity perform "
            "better ATS. A coach who 'fits' the program's culture communicates "
            "more effectively and recruits players who match the system. When "
            "coaching identity is mismatched (e.g., a Northeast coach at a "
            "Deep South school), the cultural friction reduces cohesion. "
            "DATA SOURCE: Coach biographical data from ESPN or school websites "
            "(alma mater, hometown). Not currently in system — needs collection."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "coaching_identity_match",
            "data_source": "espn_coach_bios + school_websites",
            "data_available": False,
            "fields_needed": [
                "coach_alma_mater",
                "coach_hometown",
                "coach_home_region",
                "school_region",
                "coach_identity_match_score",
            ],
            "context_factors": [
                "coach_identity_match",
                "coaching_tenure_years",
                "team_geographic_concentration",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Requires coach biographical data collection. Interesting interaction "
            "with coaching tenure — a mismatched coach who stays 5+ years may "
            "have imposed their culture successfully (tenure overrides mismatch)."
        ),
    },
    {
        "name": "ncaaw_returning_starter_identity_core_ats",
        "thesis": (
            "NCAAW teams that return 3+ starters from the previous season (the "
            "'identity core') outperform ATS, especially in the first half of the "
            "season. Returning starters define the team's identity and on-court "
            "chemistry. When the identity core is intact, new players assimilate "
            "into an EXISTING culture rather than building from scratch. This "
            "extends experience_ratio by focusing on STARTERS specifically — "
            "bench depth doesn't contribute as much to identity formation. "
            "DATA SOURCE: Previous season starting lineup data from ESPN box "
            "scores. Cross-reference with current roster. Requires historical "
            "box score collection but is feasible."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "returning_starter_core",
            "min_returning_starters": 3,
            "temporal_window": "first_half_season",
            "data_source": "espn_box_scores_prior_season + current_roster",
            "data_available": False,
            "fields_needed": [
                "prior_season_starters",
                "current_roster",
                "returning_starter_count",
            ],
            "context_factors": [
                "returning_starters",
                "returning_starter_minutes_pct",
                "experience_ratio",
                "weeks_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Requires prior-season starting lineup data. More refined than "
            "experience_ratio (which counts all upperclassmen equally). "
            "Starters ARE the team identity — returning them should be the "
            "strongest continuity signal."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 5: WNBA EXTENSIONS
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "wnba_roster_diversity_advantage_ats",
        "thesis": (
            "WNBA teams have the most diverse rosters in women's basketball — "
            "international players, different college systems, age ranges from 22 "
            "to 40. Teams that embrace and integrate this diversity (measured by "
            "roster diversity index: international %, age spread, college program "
            "variety) outperform ATS from mid-season onward. The WNBA's short "
            "season (40 games) means diverse teams that figure it out by game 15 "
            "have 25 games of edge before books catch up. "
            "DATA SOURCE: WNBA roster data from ESPN API. International player "
            "data, age, college of origin. Available."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "wnba_diversity_advantage",
            "temporal_window": "game_15_onward",
            "data_source": "espn_wnba_roster_api",
            "data_available": True,
            "fields_needed": [
                "home_country",
                "age",
                "college",
                "years_pro",
            ],
            "context_factors": [
                "roster_diversity_index",
                "international_pct",
                "age_spread",
                "college_variety",
                "games_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "WNBA extension of college diversity thesis. Professional teams "
            "have more diversity and longer to gel. The short season means the "
            "edge window is compressed but potentially larger per game."
        ),
    },
    {
        "name": "wnba_rookie_integration_disruption_props",
        "thesis": (
            "WNBA teams that add 2+ first-round rookies show measurable disruption "
            "in the first 3 weeks: veteran player assist numbers drop (integration "
            "period), team pace changes, and defensive rating worsens. Bet UNDER "
            "on veteran player assists and OVER on team total in the first 8 games "
            "when 2+ rookies are in the rotation. "
            "DATA SOURCE: WNBA draft results (public). Rookie rotation minutes "
            "from ESPN box scores. Veteran player assist lines from DraftKings "
            "props."
        ),
        "sport": "basketball_wnba",
        "market_type": "player_assists",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "rookie_integration_disruption",
            "min_rookies_in_rotation": 2,
            "temporal_window": "first_8_games",
            "side_filter": "Under",
            "data_source": "wnba_draft_results + espn_box_scores + dk_props",
            "data_available": False,
            "fields_needed": [
                "draft_position",
                "rookie_minutes",
                "veteran_assist_lines",
            ],
            "context_factors": [
                "rookie_count_in_rotation",
                "rookie_minutes_pct",
                "veteran_player",
                "games_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "WNBA prop market is new and inefficient. Rookie integration "
            "disruption is predictable from draft results (known before season). "
            "Veteran assist unders when new rookies disrupt passing patterns."
        ),
    },
    {
        "name": "wnba_olympic_year_loyalty_disruption_ats",
        "thesis": (
            "In Olympic years (2024, 2028...), WNBA teams whose star players "
            "participate in national team duty show a performance dip in the first "
            "2 weeks after the Olympic break. Players return with national team "
            "habits, different offensive systems, and loyalty dividends that "
            "temporarily disrupt club chemistry. Teams losing 2+ players to "
            "Olympics underperform ATS immediately post-break. "
            "DATA SOURCE: Olympic/national team rosters (public). WNBA schedule "
            "and Olympic break dates. Historical ATS from odds providers."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "olympic_year_loyalty_disruption",
            "olympic_years": [2024, 2028, 2032],
            "temporal_window": "first_2_weeks_post_olympic_break",
            "min_players_away": 2,
            "data_source": "olympic_rosters + wnba_schedule + odds_api",
            "data_available": False,
            "fields_needed": [
                "olympic_roster_membership",
                "post_break_game_number",
                "players_returning_from_olympics",
            ],
            "context_factors": [
                "players_at_olympics",
                "games_since_break",
                "opponent_players_at_olympics",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 15,
        "significance_level": 0.10,
        "notes": (
            "Very small sample (Olympic years only). But the disruption is "
            "predictable and public — yet books don't explicitly adjust for "
            "it. 2024 Olympics provided first WNBA dataset for this hypothesis."
        ),
    },
    {
        "name": "wnba_expansion_team_identity_formation_ats",
        "thesis": (
            "WNBA expansion teams (Golden State Valkyries 2025, future expansion) "
            "follow a predictable identity formation arc: heavy underperformance "
            "ATS in months 1-2, followed by identity crystallization and improved "
            "ATS from month 3 onward. New teams have zero established identity — "
            "no culture, no chemistry, no system continuity. Books set early-season "
            "lines based on talent assessment, but talent without identity "
            "underperforms in team sports. Bet heavy UNDERS and FADES on expansion "
            "teams early, then look for the inflection point. "
            "DATA SOURCE: Expansion team schedule and ATS from odds providers. "
            "Valkyries 2025 season is the first test dataset."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "expansion_team_identity_formation",
            "expansion_teams": {"Golden State Valkyries": 2025},
            "temporal_phases": {
                "fade_period": "months_1_2",
                "inflection": "month_3",
                "identity_formed": "months_4_onward",
            },
            "data_source": "wnba_schedule + odds_api",
            "data_available": True,
            "context_factors": [
                "is_expansion_team",
                "months_into_franchise",
                "games_played",
                "home_away",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 15,
        "significance_level": 0.10,
        "notes": (
            "Valkyries 2025 season provides first WNBA expansion data in years. "
            "Small N but predictable pattern from other sports expansions. "
            "Identity formation timeline is the key variable."
        ),
    },
    {
        "name": "wnba_offseason_continuity_early_season_ats",
        "thesis": (
            "WNBA teams that retain 80%+ of their minutes-weighted roster from the "
            "prior season outperform ATS in the first 4 weeks of the season. The "
            "WNBA's compressed preseason (3 weeks) heavily penalizes roster "
            "turnover — teams that return their core intact have a massive early "
            "advantage that books underweight because they anchor to preseason "
            "power rankings rather than continuity metrics. "
            "DATA SOURCE: Prior-season minutes distribution from ESPN box scores. "
            "Current roster from ESPN API. Calculate minutes-weighted continuity."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "wnba_offseason_continuity",
            "min_continuity_pct": 0.80,
            "temporal_window": "first_4_weeks",
            "data_source": "espn_wnba_box_scores + roster_api",
            "data_available": False,
            "fields_needed": [
                "prior_season_minutes",
                "current_roster",
                "minutes_weighted_continuity",
            ],
            "context_factors": [
                "minutes_continuity_pct",
                "roster_turnover_count",
                "weeks_into_season",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Extension of wnba_experience_ratio_early_season but using "
            "minutes-weighted continuity instead of raw experience ratio. "
            "More precise signal — a returning bench player matters less "
            "than a returning 30-mpg starter."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 6: MARKET STRUCTURE EDGES
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_spread_width_identity_interaction_ats",
        "thesis": (
            "NCAAW spreads are wider (more variance) than NCAAB spreads for "
            "equivalent matchup quality. Identity factors that would be priced "
            "into NCAAB lines persist in NCAAW. Specifically: when the experience "
            "ratio differential is >= 10 AND the spread is wider than 5 points, "
            "the experienced team covers at a higher rate than the overall "
            "experience_ratio ATS rate. The wider spread gives more room for "
            "the identity edge to manifest without being fully priced in. "
            "DATA SOURCE: Historical NCAAW spreads from DraftKings/odds API. "
            "TCI data already available. Combine spread width with identity signal."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "spread_width_identity_interaction",
            "min_spread_width": 5,
            "min_experience_differential": 10,
            "data_source": "odds_api + tci_scraper.py",
            "data_available": True,
            "context_factors": [
                "spread",
                "experience_differential",
                "spread_width_category",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Market structure hypothesis: wider spreads = more room for "
            "unpriced edges. Combines proven experience_ratio signal with "
            "spread width filter. Should improve hit rate at cost of N."
        ),
    },
    {
        "name": "ncaaw_thin_market_clv_persistence",
        "thesis": (
            "Closing Line Value (CLV) persistence is higher in NCAAW than NBA — "
            "meaning if you identify an edge early (when lines open), it doesn't "
            "get arbitraged away by sharp money before closing. In NBA, CLV decays "
            "as the line moves toward efficiency. In NCAAW, the line often DOESN'T "
            "move because there isn't enough sharp action. This means identity-based "
            "edges can be captured at opening lines and still be +CLV at close. "
            "DATA SOURCE: Opening vs closing line comparisons from odds API. "
            "Measure CLV retention rate for NCAAW vs NBA."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "market_structure",
            "signal": "thin_market_clv_persistence",
            "compare_sport": "basketball_nba",
            "data_source": "odds_api_opening_and_closing_lines",
            "data_available": False,
            "fields_needed": [
                "opening_line",
                "closing_line",
                "line_movement",
                "sport",
            ],
            "context_factors": [
                "clv_retained_pct",
                "line_movement_magnitude",
                "sport",
                "market_liquidity_proxy",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 100,
        "significance_level": 0.05,
        "notes": (
            "Market structure meta-hypothesis. If CLV persists longer in NCAAW, "
            "it validates the entire women's sports betting research program — "
            "any edge found will be more exploitable than the same edge in "
            "men's markets."
        ),
    },
    {
        "name": "wnba_prop_market_inefficiency_baseline",
        "thesis": (
            "WNBA player prop markets have systematically wider juice and less "
            "accurate lines than NBA props, creating a baseline inefficiency that "
            "makes ANY correctly-identified signal more valuable. Measure the "
            "average hold % on WNBA props vs NBA props, and measure the hit rate "
            "of naive consensus-devig models on WNBA vs NBA. If WNBA props are "
            "less efficient, it justifies dedicating modeling resources to WNBA "
            "prop edges — including identity-based ones. "
            "DATA SOURCE: DraftKings prop odds for WNBA and NBA. Calculate vig "
            "and consensus-devig fair values for both."
        ),
        "sport": "basketball_wnba",
        "market_type": "player_points",
        "model_config": {
            "type": "market_structure",
            "signal": "prop_market_inefficiency_baseline",
            "compare_sport": "basketball_nba",
            "data_source": "draftkings_props_api",
            "data_available": True,
            "fields_needed": [
                "prop_odds_over",
                "prop_odds_under",
                "implied_hold_pct",
                "sport",
            ],
            "context_factors": [
                "hold_pct",
                "sport",
                "prop_type",
                "market_age",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 200,
        "significance_level": 0.05,
        "notes": (
            "Foundational hypothesis. If WNBA props are significantly less "
            "efficient than NBA props, it justifies the entire WNBA prop "
            "research program. Measure hold %, line accuracy, and consensus "
            "devig hit rate for both sports."
        ),
    },
    {
        "name": "ncaaw_line_stale_value_ats",
        "thesis": (
            "NCAAW lines that don't move from open to close (stale lines) are "
            "more likely to be mispriced than NBA stale lines. In the NBA, a stale "
            "line means the market agreed it was correct. In NCAAW, a stale line "
            "may simply mean nobody bet into it. When identity signals (experience "
            "ratio, stability) favor the underdog and the line hasn't moved, the "
            "underdog covers at an even higher rate than the overall identity signal "
            "would predict. "
            "DATA SOURCE: Opening vs closing line comparison from odds API. Filter "
            "for zero or near-zero movement. Combine with identity signals."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "market_structure",
            "signal": "stale_line_identity_interaction",
            "max_line_movement": 0.5,
            "identity_signal": "experience_ratio",
            "data_source": "odds_api + tci_scraper.py",
            "data_available": False,
            "fields_needed": [
                "opening_line",
                "closing_line",
                "line_movement",
                "experience_differential",
            ],
            "context_factors": [
                "line_movement",
                "is_stale_line",
                "experience_differential",
                "spread",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 20,
        "significance_level": 0.10,
        "notes": (
            "Combines market structure (stale lines) with identity signal. "
            "The idea: a correct identity signal + a stale line = maximum edge "
            "because the market never had a chance to correct it."
        ),
    },
    {
        "name": "ncaaw_market_efficiency_season_trend",
        "thesis": (
            "NCAAW market efficiency improves over the course of the season as "
            "more data becomes available and books adjust. Identity-based edges "
            "should be strongest in November/December (limited data, thin markets) "
            "and weakest in March (tournament = most attention). If true, focus "
            "identity betting on early season and shift to other edges in March. "
            "DATA SOURCE: Historical ATS accuracy of closing lines by month. "
            "Measure how often favorites cover by month to detect efficiency trends."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "market_structure",
            "signal": "seasonal_efficiency_trend",
            "data_source": "odds_api_historical",
            "data_available": False,
            "fields_needed": [
                "game_month",
                "closing_spread",
                "actual_margin",
                "cover_result",
            ],
            "context_factors": [
                "month",
                "season_phase",
                "market_efficiency_proxy",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 100,
        "significance_level": 0.05,
        "notes": (
            "Meta-hypothesis about when to bet. If efficiency follows a "
            "seasonal trend, it determines the optimal temporal window for "
            "deploying identity signals. Early season = max edge, March = min."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 7: CROSS-SPORT WOMEN'S HYPOTHESES
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "nwsl_roster_continuity_early_season_ats",
        "thesis": (
            "NWSL (National Women's Soccer League) teams that retain 70%+ of their "
            "minutes-weighted roster from the prior season outperform ATS in the "
            "first 6 weeks. Women's soccer has the most extensive academic literature "
            "on team cohesion — Carron et al. have shown that task cohesion explains "
            "30%+ of performance variance in women's soccer teams. If the NCAAW "
            "experience_ratio signal transfers to NWSL, it validates the hypothesis "
            "as a universal women's team sports phenomenon, not basketball-specific. "
            "DATA SOURCE: NWSL roster data from ESPN/league website. Historical "
            "ATS from odds providers. NWSL betting markets are newer and thinner "
            "than WNBA — good edge potential."
        ),
        "sport": "soccer_nwsl",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "nwsl_roster_continuity",
            "min_continuity_pct": 0.70,
            "temporal_window": "first_6_weeks",
            "data_source": "espn_nwsl_roster + odds_api",
            "data_available": False,
            "fields_needed": [
                "prior_season_minutes",
                "current_roster",
                "minutes_weighted_continuity",
            ],
            "context_factors": [
                "minutes_continuity_pct",
                "weeks_into_season",
                "home_away",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Cross-sport validation. If experience/continuity signal works in "
            "NWSL too, it confirms the thesis is about women's team dynamics, "
            "not basketball-specific. NWSL markets are even thinner than WNBA."
        ),
    },
    {
        "name": "nwsl_international_integration_totals",
        "thesis": (
            "NWSL games involving teams with 5+ international players go OVER the "
            "total in the first month of the season due to defensive integration "
            "issues — international players come from different tactical systems "
            "and need time to learn the defensive shape. By month 2, the over-rate "
            "normalizes. This mirrors the NCAAW international_player_adjustment "
            "hypothesis but applied to soccer where defensive cohesion is even "
            "more critical (one miscommunication = goal). "
            "DATA SOURCE: NWSL roster nationality data. Game totals from odds API."
        ),
        "sport": "soccer_nwsl",
        "market_type": "totals",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "nwsl_international_defensive_integration",
            "min_international_players": 5,
            "side_filter": "Over",
            "temporal_window": "first_month",
            "data_source": "nwsl_roster_nationality + odds_api",
            "data_available": False,
            "fields_needed": [
                "player_nationality",
                "international_count",
                "game_month",
            ],
            "context_factors": [
                "international_count",
                "international_pct",
                "games_into_season",
                "total_line",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "NWSL totals market is very thin — large potential edge if signal "
            "is real. Defensive integration in soccer takes longer than basketball "
            "because the tactical complexity is higher."
        ),
    },
    {
        "name": "ncaaw_volleyball_cohesion_transfer",
        "thesis": (
            "If team cohesion identity factors predict ATS outcomes in NCAAW "
            "basketball, they should also predict outcomes in NCAA women's "
            "volleyball — where team chemistry is arguably more important (rotation "
            "systems, setter-hitter chemistry, serve-receive patterns all depend "
            "on cohesion). Test experience ratio and stability score from TCI on "
            "volleyball match outcomes. If the signal transfers, it opens an "
            "entirely new sport for identity-based betting. "
            "DATA SOURCE: ESPN volleyball roster data (same API structure as "
            "basketball). NCAA volleyball betting lines from odds providers."
        ),
        "sport": "volleyball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "volleyball_cohesion_transfer",
            "signals_to_test": ["experience_ratio", "stability_score"],
            "data_source": "espn_volleyball_roster + odds_api",
            "data_available": False,
            "fields_needed": [
                "volleyball_roster",
                "class_year",
                "coaching_tenure",
                "volleyball_betting_lines",
            ],
            "context_factors": [
                "experience_ratio",
                "stability_score",
                "sport",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Cross-sport validation hypothesis. NCAA volleyball is growing fast "
            "as a betting market. ESPN has volleyball roster data. If cohesion "
            "signals work here, the thesis is universal for women's team sports."
        ),
    },
    {
        "name": "womens_universal_cohesion_meta_analysis",
        "thesis": (
            "Across all women's team sports (NCAAW basketball, WNBA, NWSL, NCAA "
            "volleyball), the experience_ratio signal produces a positive ATS "
            "return. The MAGNITUDE of the effect varies by sport (stronger in "
            "thinner markets, stronger where cohesion matters more tactically) "
            "but the DIRECTION is consistent. This meta-hypothesis aggregates "
            "results across sports to determine if there is a universal women's "
            "team cohesion betting edge. "
            "DATA SOURCE: Results from individual sport hypotheses in this set."
        ),
        "sport": "general",
        "market_type": "spreads",
        "model_config": {
            "type": "meta_analysis",
            "signal": "universal_womens_cohesion_edge",
            "sports_to_aggregate": [
                "basketball_ncaaw",
                "basketball_wnba",
                "soccer_nwsl",
                "volleyball_ncaaw",
            ],
            "core_signal": "experience_ratio",
            "data_source": "internal_hypothesis_results",
            "data_available": False,
            "context_factors": [
                "sport",
                "effect_size",
                "sample_size",
                "market_thickness",
            ],
        },
        "edge_threshold": 0.01,
        "min_sample_size": 100,
        "significance_level": 0.05,
        "notes": (
            "Ultimate meta-hypothesis. Can only be tested after sport-specific "
            "hypotheses produce results. If confirmed, it establishes 'women's "
            "sports team cohesion' as a systematic, multi-sport betting edge."
        ),
    },

    # ══════════════════════════════════════════════════════════════════════
    # SECTION 8: ADDITIONAL IDENTITY DEEP-DIVES
    # ══════════════════════════════════════════════════════════════════════

    {
        "name": "ncaaw_coach_gender_identity_ats",
        "thesis": (
            "NCAAW teams coached by women outperform ATS compared to teams coached "
            "by men, particularly in tournament play. The mechanism: female coaches "
            "may build stronger interpersonal connections with female players, "
            "creating deeper trust and more effective communication under pressure. "
            "Academic literature on gender congruence in coaching supports this — "
            "athlete-coach gender match improves coach-athlete relationship quality. "
            "DATA SOURCE: Coach gender is determinable from ESPN coach data (first "
            "name/biographical info). Already partially available in tci_scraper.py "
            "COACHING_TENURE_FALLBACK."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "coach_gender_match",
            "game_filter": "ncaa_tournament_only",
            "data_source": "espn_coach_data + tci_scraper.py",
            "data_available": True,
            "fields_needed": [
                "coach_gender",
                "coaching_tenure_years",
            ],
            "context_factors": [
                "coach_gender",
                "is_tournament",
                "coaching_tenure_years",
                "seed",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Gender congruence in coaching is well-studied in sports psychology. "
            "Coach gender is easily determinable. ~60% of NCAAW coaches are women "
            "as of 2025-26. Test tournament ATS split by coach gender."
        ),
    },
    {
        "name": "ncaaw_conference_cultural_identity_ats",
        "thesis": (
            "Teams that have been in the SAME conference for 10+ years have "
            "a conference identity that provides an ATS advantage in conference play "
            "but may hurt in out-of-conference/tournament play against unfamiliar "
            "styles. Conference realignment (SEC/Big 12 expansion 2024-25) "
            "disrupts this identity — teams that just switched conferences should "
            "underperform ATS in conference play for 1-2 years while building "
            "new conference identity. "
            "DATA SOURCE: Conference membership history (public, Wikipedia/ESPN). "
            "Recent realignment data is well-documented."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "conference_identity_tenure",
            "min_conference_tenure_years": 10,
            "realignment_disruption_years": 2,
            "data_source": "conference_membership_history + odds_api",
            "data_available": False,
            "fields_needed": [
                "conference",
                "years_in_conference",
                "recently_realigned",
                "prior_conference",
            ],
            "context_factors": [
                "years_in_conference",
                "is_conference_game",
                "recently_realigned",
                "opponent_years_in_conference",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Timely hypothesis given 2024-25 conference realignment. Texas, "
            "Oklahoma (to SEC), Colorado, Arizona (to Big 12) women's teams "
            "all face identity disruption. Testable starting this season."
        ),
    },
    {
        "name": "ncaaw_team_age_homogeneity_ats",
        "thesis": (
            "NCAAW teams with high age homogeneity (most players within 1 year of "
            "each other, either all young or all experienced) perform differently "
            "than age-diverse teams. All-young teams are volatile (high variance "
            "ATS), all-experienced teams are consistent (low variance, slight "
            "positive ATS), and age-diverse teams are moderate. The edge: bet "
            "experienced-homogeneous teams as slight underdogs and fade "
            "young-homogeneous teams in close games. "
            "DATA SOURCE: ESPN roster API class_year field. Already available "
            "via tci_scraper.py. Compute class year standard deviation."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "age_homogeneity",
            "data_source": "espn_roster_api",
            "data_available": True,
            "fields_needed": [
                "class_year",
                "years_exp",
            ],
            "context_factors": [
                "class_year_stddev",
                "age_homogeneity_type",
                "experience_ratio",
                "spread",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Related to experience_ratio but captures a different dimension: "
            "HOMOGENEITY of age rather than just proportion of upperclassmen. "
            "A team of all sophomores and a team of all seniors are both "
            "age-homogeneous but play very differently."
        ),
    },
    {
        "name": "ncaaw_5th_year_grad_transfer_wisdom_ats",
        "thesis": (
            "NCAAW teams with 2+ fifth-year seniors or grad transfers outperform "
            "ATS in close games (spread < 5). These players have seen everything — "
            "multiple coaching systems, multiple conferences, high-pressure games. "
            "Their composure in close games is a measurable edge. Unlike raw "
            "experience_ratio, this focuses on the MOST experienced players "
            "specifically. "
            "DATA SOURCE: ESPN roster API class_year field (Graduate, 5th Year). "
            "Available now."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "fifth_year_grad_wisdom",
            "min_fifth_year_players": 2,
            "spread_filter": {"max_abs_spread": 5},
            "data_source": "espn_roster_api",
            "data_available": True,
            "fields_needed": [
                "class_year",
            ],
            "context_factors": [
                "fifth_year_count",
                "grad_transfer_count",
                "spread",
                "game_location",
            ],
        },
        "edge_threshold": 0.03,
        "min_sample_size": 25,
        "significance_level": 0.10,
        "notes": (
            "Refinement of experience_ratio. Isolates the MOST experienced "
            "players rather than general upperclassmen. Close game filter "
            "targets situations where composure matters most."
        ),
    },
    {
        "name": "ncaaw_hometown_hero_home_game_ats",
        "thesis": (
            "NCAAW teams with multiple players (3+) whose hometown is within 100 "
            "miles of the school outperform ATS in home games specifically. These "
            "players have family and community support networks at home games, "
            "creating an amplified home court advantage beyond the standard HCA. "
            "The effect should be strongest in conference play (more return "
            "visitors game to game). "
            "DATA SOURCE: ESPN roster API hometown field. School location. Calculate "
            "distance between player hometown and school. Requires geocoding."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "hometown_hero_hca",
            "min_local_players": 3,
            "max_distance_miles": 100,
            "game_filter": "home_only",
            "data_source": "espn_roster_api + geocoding_api",
            "data_available": False,
            "fields_needed": [
                "hometown",
                "home_state",
                "school_location",
                "distance_to_school",
            ],
            "context_factors": [
                "local_player_count",
                "avg_distance_to_school",
                "is_home_game",
                "is_conference_game",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 40,
        "significance_level": 0.10,
        "notes": (
            "Requires geocoding of player hometowns and school locations. "
            "Google Maps API or similar. The hometown support hypothesis is "
            "intuitive and untested in the market."
        ),
    },
    {
        "name": "wnba_team_identity_strength_midseason_ats",
        "thesis": (
            "WNBA teams with strong pre-existing identity (measured by brand "
            "continuity metrics: years since last rebrand, coach tenure, core "
            "player tenure, city/community connection) outperform ATS in "
            "mid-season stretches when motivation typically dips. Strong identity "
            "provides intrinsic motivation that sustains effort during the 'dog "
            "days' of the WNBA season (June-July between the start and playoff "
            "push). Fade low-identity teams in mid-season, back high-identity teams. "
            "DATA SOURCE: WNBA team history (public). Coach tenure, franchise "
            "history, core player tenure from ESPN."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "wnba_identity_strength_midseason",
            "temporal_window": "june_july",
            "identity_components": [
                "franchise_age",
                "years_since_rebrand",
                "coach_tenure",
                "core_player_tenure",
            ],
            "data_source": "wnba_franchise_history + espn_roster",
            "data_available": False,
            "fields_needed": [
                "franchise_history",
                "coach_tenure",
                "core_player_years",
                "rebrand_history",
            ],
            "context_factors": [
                "identity_strength_score",
                "game_month",
                "is_midseason",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "WNBA franchises range from brand-new (Valkyries) to 30 years old "
            "(original 8). The identity strength variance is massive. Minnesota "
            "Lynx (strong identity) vs Dallas Wings (recently relocated/rebranded)."
        ),
    },
    {
        "name": "ncaaw_shared_adversity_ats",
        "thesis": (
            "NCAAW teams that experienced a significant shared adversity event "
            "(coaching change in the last 2 years, NCAA investigation, major "
            "player injury/departure, natural disaster affecting campus) show "
            "a cohesion boost in the FOLLOWING season. Shared adversity is one "
            "of the strongest predictors of team cohesion in organizational "
            "psychology — teams that survive hardship together bond more tightly. "
            "The effect should be strongest in Year 2 after the adversity event "
            "(Year 1 = disruption, Year 2 = galvanization). "
            "DATA SOURCE: News API or manual compilation of adversity events. "
            "Coaching changes are available from tci_scraper.py tenure data."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "identity_cohesion",
            "signal": "shared_adversity_cohesion_boost",
            "adversity_lookback_years": 2,
            "adversity_types": [
                "coaching_change",
                "ncaa_investigation",
                "star_player_departure",
                "program_scandal",
            ],
            "data_source": "news_api + tci_scraper.py + manual",
            "data_available": False,
            "fields_needed": [
                "adversity_event_type",
                "adversity_event_date",
                "years_since_adversity",
            ],
            "context_factors": [
                "adversity_type",
                "years_since_adversity",
                "coaching_tenure_years",
                "experience_ratio",
            ],
        },
        "edge_threshold": 0.02,
        "min_sample_size": 30,
        "significance_level": 0.10,
        "notes": (
            "Coaching changes are the most common and trackable adversity event. "
            "Teams with a new coach in Year 1 (disruption → fade) followed by "
            "Year 2 (galvanization → back) is a testable pattern. The coaching "
            "tenure = 2 years in tci_scraper.py is a proxy for this."
        ),
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# MAIN: INSERT HYPOTHESES INTO DATABASE
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    from tools.hypothesis import HypothesisManager

    mgr = HypothesisManager()
    await mgr.initialize()

    existing = await mgr.list_hypotheses()
    existing_names = {h["name"]: h["hypothesis_id"] for h in existing}

    print("=" * 75)
    print("WOMEN'S SPORTS IDENTITY & CULTURAL COHESION HYPOTHESES")
    print(f"Generating {len(WOMENS_IDENTITY_HYPOTHESES)} hypotheses")
    print("=" * 75)

    # Categorize hypotheses for reporting
    categories = {
        "Regional/Geographic Identity": [],
        "Demographic Composition & Heterogeneity": [],
        "Religious/Institutional Identity": [],
        "Identity Mesh / Interaction Effects": [],
        "WNBA Extensions": [],
        "Market Structure Edges": [],
        "Cross-Sport Women's": [],
        "Additional Identity Deep-Dives": [],
    }

    created = 0
    skipped = 0
    errors = 0

    for h in WOMENS_IDENTITY_HYPOTHESES:
        name = h["name"]

        # Categorize for reporting
        if "geographic" in name or "in_state" in name or "sec_acc" in name or "urban_rural" in name or "military" in name:
            cat = "Regional/Geographic Identity"
        elif "homogeneity" in name or "international" in name or "diversity" in name or "demographic" in name or "gender_dynamics" in name:
            cat = "Demographic Composition & Heterogeneity"
        elif "religious" in name or "notre_dame" in name or "jesuit" in name or "hbcu" in name:
            cat = "Religious/Institutional Identity"
        elif "three_factor" in name or "culture_fit" in name or "transfer_portal" in name or "identity_mesh" in name or "coaching_identity" in name or "returning_starter" in name:
            cat = "Identity Mesh / Interaction Effects"
        elif "wnba" in name:
            cat = "WNBA Extensions"
        elif "market" in name or "clv" in name or "prop_market" in name or "stale" in name or "efficiency" in name or "spread_width" in name:
            cat = "Market Structure Edges"
        elif "nwsl" in name or "volleyball" in name or "universal" in name:
            cat = "Cross-Sport Women's"
        else:
            cat = "Additional Identity Deep-Dives"

        if name in existing_names:
            print(f"  SKIP (exists): {name}")
            categories[cat].append(f"  [EXISTS] {name}")
            skipped += 1
            continue

        try:
            import json as _json
            model_config = h["model_config"]

            hid = await mgr.create_hypothesis(
                name=name,
                thesis=h["thesis"],
                sport=h["sport"],
                market_type=h["market_type"],
                model_config=model_config,
                edge_threshold=h.get("edge_threshold", 0.02),
                min_sample_size=h.get("min_sample_size", 30),
                significance_level=h.get("significance_level", 0.10),
                notes=h.get("notes", "Women's sports identity hypothesis — Marco's core thesis"),
            )
            created += 1
            data_available = model_config.get("data_available", False)
            data_tag = "DATA:READY" if data_available else "DATA:NEEDED"
            print(f"  [DRAFT] {name} -> {hid} ({data_tag})")
            categories[cat].append(f"  [DRAFT] {name} ({data_tag})")
        except Exception as e:
            errors += 1
            print(f"  [ERROR] {name}: {e}")
            categories[cat].append(f"  [ERROR] {name}: {e}")

    # Summary
    print("\n" + "=" * 75)
    print("SUMMARY")
    print("=" * 75)
    print(f"  Created: {created}")
    print(f"  Skipped (already exist): {skipped}")
    print(f"  Errors: {errors}")
    print(f"  Total hypotheses attempted: {len(WOMENS_IDENTITY_HYPOTHESES)}")

    print("\n" + "=" * 75)
    print("HYPOTHESES BY CATEGORY")
    print("=" * 75)
    for cat, entries in categories.items():
        if entries:
            print(f"\n  [{len(entries)}] {cat}:")
            for e in entries:
                print(f"    {e}")

    # Data availability summary
    print("\n" + "=" * 75)
    print("DATA AVAILABILITY")
    print("=" * 75)
    ready = [h["name"] for h in WOMENS_IDENTITY_HYPOTHESES
             if h["model_config"].get("data_available", False)]
    needed = [h["name"] for h in WOMENS_IDENTITY_HYPOTHESES
              if not h["model_config"].get("data_available", False)]
    print(f"\n  DATA READY ({len(ready)} hypotheses — can backtest NOW):")
    for n in ready:
        print(f"    - {n}")
    print(f"\n  DATA NEEDED ({len(needed)} hypotheses — require collection):")
    for n in needed:
        print(f"    - {n}")

    await mgr.close()

    print("\n" + "=" * 75)
    print("NEXT STEPS")
    print("=" * 75)
    print("  1. Backtest DATA READY hypotheses against DK closing lines")
    print("  2. Collect missing data sources for DATA NEEDED hypotheses")
    print("  3. Run the three_factor_interaction model (multiplicative vs additive)")
    print("  4. Compare signal strength: NCAAW vs NCAAB (gender_dynamics meta-test)")
    print("  5. Start WNBA data collection for 2025 season (Valkyries expansion)")
    print("  6. Build NWSL roster scraper for cross-sport validation")


if __name__ == "__main__":
    asyncio.run(main())
