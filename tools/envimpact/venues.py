"""
Venue databases for Callisto environmental models.

NFL stadiums, NBA arenas, and MLB parks with the physical attributes that
drive the adjustment math: altitude, domes, surfaces, wind exposure, and
park factors (FanGraphs multi-year averages).
"""

# =============================================================================
# VENUE DATABASES
# =============================================================================

# NFL venues: altitude_ft, dome (True/False), surface, typical_wind_exposure (0-10)
NFL_VENUES = {
    # AFC East
    "BUF": {"name": "Highmark Stadium", "altitude_ft": 600, "dome": False, "surface": "turf", "wind_exposure": 9, "city": "Buffalo"},
    "MIA": {"name": "Hard Rock Stadium", "altitude_ft": 7, "dome": False, "surface": "grass", "wind_exposure": 6, "city": "Miami"},
    "NE":  {"name": "Gillette Stadium", "altitude_ft": 256, "dome": False, "surface": "turf", "wind_exposure": 8, "city": "Foxborough"},
    "NYJ": {"name": "MetLife Stadium", "altitude_ft": 7, "dome": False, "surface": "turf", "wind_exposure": 7, "city": "East Rutherford"},
    "NYG": {"name": "MetLife Stadium", "altitude_ft": 7, "dome": False, "surface": "turf", "wind_exposure": 7, "city": "East Rutherford"},
    # AFC North
    "BAL": {"name": "M&T Bank Stadium", "altitude_ft": 30, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Baltimore"},
    "CIN": {"name": "Paycor Stadium", "altitude_ft": 490, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Cincinnati"},
    "CLE": {"name": "Cleveland Browns Stadium", "altitude_ft": 583, "dome": False, "surface": "grass", "wind_exposure": 9, "city": "Cleveland"},
    "PIT": {"name": "Acrisure Stadium", "altitude_ft": 730, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Pittsburgh"},
    # AFC South
    "HOU": {"name": "NRG Stadium", "altitude_ft": 43, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Houston"},
    "IND": {"name": "Lucas Oil Stadium", "altitude_ft": 715, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Indianapolis"},
    "JAX": {"name": "EverBank Stadium", "altitude_ft": 16, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Jacksonville"},
    "TEN": {"name": "Nissan Stadium", "altitude_ft": 400, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Nashville"},
    # AFC West
    "DEN": {"name": "Empower Field at Mile High", "altitude_ft": 5280, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Denver"},
    "KC":  {"name": "GEHA Field at Arrowhead", "altitude_ft": 800, "dome": False, "surface": "grass", "wind_exposure": 7, "city": "Kansas City"},
    "LV":  {"name": "Allegiant Stadium", "altitude_ft": 2030, "dome": True, "surface": "grass", "wind_exposure": 0, "city": "Las Vegas"},
    "LAC": {"name": "SoFi Stadium", "altitude_ft": 108, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Inglewood"},
    # NFC East
    "DAL": {"name": "AT&T Stadium", "altitude_ft": 600, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Arlington"},
    "PHI": {"name": "Lincoln Financial Field", "altitude_ft": 39, "dome": False, "surface": "grass", "wind_exposure": 7, "city": "Philadelphia"},
    "WAS": {"name": "Commanders Field", "altitude_ft": 200, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Landover"},
    # NFC North
    "CHI": {"name": "Soldier Field", "altitude_ft": 595, "dome": False, "surface": "grass", "wind_exposure": 9, "city": "Chicago"},
    "DET": {"name": "Ford Field", "altitude_ft": 600, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Detroit"},
    "GB":  {"name": "Lambeau Field", "altitude_ft": 640, "dome": False, "surface": "grass", "wind_exposure": 8, "city": "Green Bay"},
    "MIN": {"name": "U.S. Bank Stadium", "altitude_ft": 830, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Minneapolis"},
    # NFC South
    "ATL": {"name": "Mercedes-Benz Stadium", "altitude_ft": 1050, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Atlanta"},
    "CAR": {"name": "Bank of America Stadium", "altitude_ft": 748, "dome": False, "surface": "turf", "wind_exposure": 4, "city": "Charlotte"},
    "NO":  {"name": "Caesars Superdome", "altitude_ft": 3, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "New Orleans"},
    "TB":  {"name": "Raymond James Stadium", "altitude_ft": 36, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Tampa"},
    # NFC West
    "ARI": {"name": "State Farm Stadium", "altitude_ft": 1100, "dome": True, "surface": "grass", "wind_exposure": 0, "city": "Glendale"},
    "LAR": {"name": "SoFi Stadium", "altitude_ft": 108, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Inglewood"},
    "SF":  {"name": "Levi's Stadium", "altitude_ft": 43, "dome": False, "surface": "grass", "wind_exposure": 6, "city": "Santa Clara"},
    "SEA": {"name": "Lumen Field", "altitude_ft": 20, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Seattle"},
}

# NBA arenas: altitude is the primary environmental factor
NBA_VENUES = {
    "DEN": {"name": "Ball Arena", "altitude_ft": 5280, "city": "Denver"},
    "UTA": {"name": "Delta Center", "altitude_ft": 4226, "city": "Salt Lake City"},
    "PHX": {"name": "Footprint Center", "altitude_ft": 1086, "city": "Phoenix"},
    "ATL": {"name": "State Farm Arena", "altitude_ft": 1050, "city": "Atlanta"},
    "OKC": {"name": "Paycom Center", "altitude_ft": 1201, "city": "Oklahoma City"},
    "MIL": {"name": "Fiserv Forum", "altitude_ft": 617, "city": "Milwaukee"},
    "MIN": {"name": "Target Center", "altitude_ft": 830, "city": "Minneapolis"},
    "IND": {"name": "Gainbridge Fieldhouse", "altitude_ft": 715, "city": "Indianapolis"},
    "CLE": {"name": "Rocket Mortgage Fieldhouse", "altitude_ft": 653, "city": "Cleveland"},
    "CHI": {"name": "United Center", "altitude_ft": 595, "city": "Chicago"},
    "DET": {"name": "Little Caesars Arena", "altitude_ft": 600, "city": "Detroit"},
    "DAL": {"name": "American Airlines Center", "altitude_ft": 430, "city": "Dallas"},
    "MEM": {"name": "FedExForum", "altitude_ft": 337, "city": "Memphis"},
    "CHA": {"name": "Spectrum Center", "altitude_ft": 748, "city": "Charlotte"},
    "POR": {"name": "Moda Center", "altitude_ft": 50, "city": "Portland"},
    "SAC": {"name": "Golden 1 Center", "altitude_ft": 30, "city": "Sacramento"},
    "GSW": {"name": "Chase Center", "altitude_ft": 10, "city": "San Francisco"},
    "LAL": {"name": "Crypto.com Arena", "altitude_ft": 340, "city": "Los Angeles"},
    "LAC": {"name": "Intuit Dome", "altitude_ft": 108, "city": "Inglewood"},
    "HOU": {"name": "Toyota Center", "altitude_ft": 43, "city": "Houston"},
    "SAS": {"name": "Frost Bank Center", "altitude_ft": 650, "city": "San Antonio"},
    "NOP": {"name": "Smoothie King Center", "altitude_ft": 3, "city": "New Orleans"},
    "MIA": {"name": "Kaseya Center", "altitude_ft": 7, "city": "Miami"},
    "ORL": {"name": "Kia Center", "altitude_ft": 82, "city": "Orlando"},
    "WAS": {"name": "Capital One Arena", "altitude_ft": 25, "city": "Washington"},
    "PHI": {"name": "Wells Fargo Center", "altitude_ft": 39, "city": "Philadelphia"},
    "NYK": {"name": "Madison Square Garden", "altitude_ft": 33, "city": "New York"},
    "BKN": {"name": "Barclays Center", "altitude_ft": 33, "city": "Brooklyn"},
    "BOS": {"name": "TD Garden", "altitude_ft": 20, "city": "Boston"},
    "TOR": {"name": "Scotiabank Arena", "altitude_ft": 249, "city": "Toronto"},
}

# MLB parks: altitude, dome, and park factors (1.000 = neutral, >1 = hitter friendly)
# Park factors sourced from FanGraphs multi-year averages
MLB_VENUES = {
    "COL": {"name": "Coors Field", "altitude_ft": 5200, "dome": False, "park_factor": 1.380, "wind_exposure": 4, "city": "Denver",
             "notes": "Highest park factor in MLB. Ball carries ~9% further. Humidor mitigates slightly."},
    "CIN": {"name": "Great American Ball Park", "altitude_ft": 490, "dome": False, "park_factor": 1.130, "wind_exposure": 5, "city": "Cincinnati"},
    "TEX": {"name": "Globe Life Field", "altitude_ft": 500, "dome": True, "park_factor": 1.050, "wind_exposure": 0, "city": "Arlington"},
    "BOS": {"name": "Fenway Park", "altitude_ft": 20, "dome": False, "park_factor": 1.080, "wind_exposure": 5, "city": "Boston",
             "notes": "Green Monster creates doubles. Favors RHBs."},
    "CHC": {"name": "Wrigley Field", "altitude_ft": 595, "dome": False, "park_factor": 1.060, "wind_exposure": 9, "city": "Chicago",
             "notes": "Wind direction is everything. Wind blowing out adds 2+ runs. Wind in suppresses."},
    "PHI": {"name": "Citizens Bank Park", "altitude_ft": 20, "dome": False, "park_factor": 1.070, "wind_exposure": 5, "city": "Philadelphia"},
    "MIL": {"name": "American Family Field", "altitude_ft": 635, "dome": True, "park_factor": 1.040, "wind_exposure": 0, "city": "Milwaukee"},
    "MIN": {"name": "Target Field", "altitude_ft": 815, "dome": False, "park_factor": 1.010, "wind_exposure": 6, "city": "Minneapolis"},
    "ATL": {"name": "Truist Park", "altitude_ft": 1050, "dome": False, "park_factor": 1.020, "wind_exposure": 3, "city": "Atlanta"},
    "NYY": {"name": "Yankee Stadium", "altitude_ft": 55, "dome": False, "park_factor": 1.090, "wind_exposure": 4, "city": "Bronx",
             "notes": "Short RF porch. HR-friendly for LHBs."},
    "NYM": {"name": "Citi Field", "altitude_ft": 20, "dome": False, "park_factor": 0.930, "wind_exposure": 6, "city": "Queens"},
    "SF":  {"name": "Oracle Park", "altitude_ft": 10, "dome": False, "park_factor": 0.870, "wind_exposure": 8, "city": "San Francisco",
             "notes": "Cold, windy, heavy marine air. Extreme pitcher's park."},
    "LAD": {"name": "Dodger Stadium", "altitude_ft": 515, "dome": False, "park_factor": 0.980, "wind_exposure": 3, "city": "Los Angeles"},
    "SD":  {"name": "Petco Park", "altitude_ft": 17, "dome": False, "park_factor": 0.920, "wind_exposure": 4, "city": "San Diego"},
    "OAK": {"name": "Oakland Coliseum", "altitude_ft": 10, "dome": False, "park_factor": 0.920, "wind_exposure": 7, "city": "Oakland",
             "notes": "Foul territory is massive. Suppresses offense."},
    "SEA": {"name": "T-Mobile Park", "altitude_ft": 20, "dome": True, "park_factor": 0.940, "wind_exposure": 0, "city": "Seattle"},
    "TB":  {"name": "Tropicana Field", "altitude_ft": 45, "dome": True, "park_factor": 0.950, "wind_exposure": 0, "city": "St. Petersburg"},
    "MIA": {"name": "LoanDepot Park", "altitude_ft": 7, "dome": True, "park_factor": 0.910, "wind_exposure": 0, "city": "Miami"},
    "STL": {"name": "Busch Stadium", "altitude_ft": 465, "dome": False, "park_factor": 0.970, "wind_exposure": 4, "city": "St. Louis"},
    "PIT": {"name": "PNC Park", "altitude_ft": 730, "dome": False, "park_factor": 0.960, "wind_exposure": 4, "city": "Pittsburgh"},
    "KC":  {"name": "Kauffman Stadium", "altitude_ft": 800, "dome": False, "park_factor": 0.980, "wind_exposure": 6, "city": "Kansas City"},
    "CWS": {"name": "Guaranteed Rate Field", "altitude_ft": 595, "dome": False, "park_factor": 1.060, "wind_exposure": 7, "city": "Chicago"},
    "DET": {"name": "Comerica Park", "altitude_ft": 600, "dome": False, "park_factor": 0.960, "wind_exposure": 5, "city": "Detroit"},
    "CLE": {"name": "Progressive Field", "altitude_ft": 653, "dome": False, "park_factor": 0.970, "wind_exposure": 5, "city": "Cleveland"},
    "HOU": {"name": "Minute Maid Park", "altitude_ft": 43, "dome": True, "park_factor": 1.040, "wind_exposure": 0, "city": "Houston"},
    "LAA": {"name": "Angel Stadium", "altitude_ft": 157, "dome": False, "park_factor": 0.970, "wind_exposure": 4, "city": "Anaheim"},
    "ARI": {"name": "Chase Field", "altitude_ft": 1100, "dome": True, "park_factor": 1.060, "wind_exposure": 0, "city": "Phoenix"},
    "TOR": {"name": "Rogers Centre", "altitude_ft": 249, "dome": True, "park_factor": 1.010, "wind_exposure": 0, "city": "Toronto"},
    "BAL": {"name": "Camden Yards", "altitude_ft": 30, "dome": False, "park_factor": 1.050, "wind_exposure": 4, "city": "Baltimore"},
    "WAS": {"name": "Nationals Park", "altitude_ft": 25, "dome": False, "park_factor": 0.990, "wind_exposure": 4, "city": "Washington"},
}
