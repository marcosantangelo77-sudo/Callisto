"""Pure computation of the Team Cohesion Index from roster + team info.

No I/O — fully deterministic and unit-testable.
"""

from collections import Counter

from tools.tciscrape.constants import STATE_REGIONS


def compute_tci(roster: dict, team_info: dict) -> dict:
    """
    Compute Team Cohesion Index from roster and team data.

    Returns individual metrics and composite TCI score (0-100).

    Based on meta-analysis (195 studies, n=12,023): task cohesion and social
    cohesion are distinct constructs. In women's teams, social cohesion shows
    a NEGATIVE performance correlation while task cohesion is strongly positive.
    The formula separates these dimensions accordingly.

    Task Cohesion (positive contributors):
      - Roster experience/continuity (30%)
      - Coaching tenure stability (30%)
      - Class balance / role clarity (10%)

    Social Cohesion (negative/neutral — NOT added to score):
      - Geographic concentration (tracked but not scored positively)
      - Domestic concentration (tracked but not scored)

    Program Stability (moderate positive):
      - Coaching tenure consistency (captured in task cohesion)
      - Low transfer churn proxy (20%)
      - Institutional continuity (10%)
    """
    players = roster.get("players", [])
    if not players:
        return {"tci_score": 0, "error": "no players"}

    # --- Geographic concentration (tracked, NOT positively scored) ---
    states = [p["home_state"] for p in players if p.get("home_state")]
    regions = [STATE_REGIONS.get(s, "Unknown") for s in states]
    domestic_count = sum(
        1 for p in players
        if p.get("home_country", "USA") in ("USA", "US", "United States")
    )
    international_count = len(players) - domestic_count

    if regions:
        region_counts = Counter(regions)
        top_region, top_count = region_counts.most_common(1)[0]
        geo_concentration = top_count / len(regions)
    else:
        top_region = "Unknown"
        geo_concentration = 0

    if states:
        state_counts = Counter(states)
        top_state, top_state_count = state_counts.most_common(1)[0]
        state_concentration = top_state_count / len(states)
    else:
        top_state = "Unknown"
        state_concentration = 0

    # --- Class distribution (experience = task cohesion proxy) ---
    class_years = [p.get("class_year", "").lower() for p in players]
    seniors_grad = sum(
        1 for c in class_years
        if any(y in c for y in ["senior", "sr", "graduate", "grad", "5th"])
    )
    juniors = sum(
        1 for c in class_years
        if any(y in c for y in ["junior", "jr"])
    )
    sophomores = sum(
        1 for c in class_years
        if any(y in c for y in ["sophomore", "so"])
    )
    freshmen = sum(
        1 for c in class_years
        if any(y in c for y in ["freshman", "fr"])
    )
    upperclassmen = seniors_grad + juniors
    underclassmen = sophomores + freshmen
    total_classified = upperclassmen + underclassmen
    experience_ratio = upperclassmen / total_classified if total_classified > 0 else 0.5

    # Class balance: teams with a spread across all years have better role clarity
    # Perfect balance = 0.25 each; measure via inverse of standard deviation
    if total_classified > 0:
        class_fracs = [
            seniors_grad / total_classified,
            juniors / total_classified,
            sophomores / total_classified,
            freshmen / total_classified,
        ]
        class_mean = 0.25
        class_variance = sum((f - class_mean) ** 2 for f in class_fracs) / 4
        # 0 variance = perfect balance (score 1.0), high variance = unbalanced (score 0)
        class_balance = max(0, 1.0 - (class_variance ** 0.5) * 4)
    else:
        class_balance = 0.5

    # --- Coaching tenure (TASK cohesion — system continuity) ---
    coach = team_info.get("head_coach", {})
    coaching_tenure = coach.get("tenure_years", 0)
    coaching_stability = min(coaching_tenure / 10.0, 1.0)

    # --- Transfer churn proxy (LOW freshmen ratio = roster stability) ---
    # High freshman/transfer count signals roster disruption
    if total_classified > 0:
        continuity_proxy = 1.0 - (freshmen / total_classified)
    else:
        continuity_proxy = 0.5

    # --- Institutional stability (weaker signal, reduced weight) ---
    affiliation = team_info.get("religious_affiliation", "secular")
    institutional_factor = 0.1 if affiliation != "secular" else 0.0

    # --- Composite TCI Score (0-100) ---
    # Weights based on academic evidence for women's team performance:
    # Task cohesion proxies dominate; social cohesion excluded from positive scoring
    tci_score = (
        experience_ratio * 30           # Task: roster experience (30%)
        + coaching_stability * 30       # Task: coaching system tenure (30%)
        + continuity_proxy * 20         # Stability: low roster churn (20%)
        + class_balance * 10            # Task: role clarity / class spread (10%)
        + institutional_factor * 100    # Stability: institutional continuity (10%)
    )

    # --- Social Cohesion Index (tracked separately, NOT added to TCI) ---
    # Academic evidence: social cohesion is negatively correlated with
    # women's team performance. High values here may indicate RISK, not edge.
    social_cohesion = (
        geo_concentration * 50
        + (1 - international_count / max(len(players), 1)) * 50
    )

    return {
        "tci_score": round(tci_score, 1),
        "task_cohesion": round(experience_ratio * 30 + coaching_stability * 30 + class_balance * 10, 1),
        "social_cohesion": round(social_cohesion, 1),
        "stability_score": round(continuity_proxy * 20 + institutional_factor * 100, 1),
        "geographic_concentration": round(geo_concentration, 3),
        "top_region": top_region,
        "state_concentration": round(state_concentration, 3),
        "top_state": top_state,
        "experience_ratio": round(experience_ratio, 3),
        "class_balance": round(class_balance, 3),
        "continuity_proxy": round(continuity_proxy, 3),
        "upperclassmen": upperclassmen,
        "underclassmen": underclassmen,
        "seniors_grad": seniors_grad,
        "juniors": juniors,
        "sophomores": sophomores,
        "freshmen": freshmen,
        "coaching_tenure_years": coaching_tenure,
        "coaching_stability": round(coaching_stability, 3),
        "religious_affiliation": affiliation,
        "institutional_factor": institutional_factor,
        "international_players": international_count,
        "domestic_players": domestic_count,
        "roster_size": len(players),
    }
