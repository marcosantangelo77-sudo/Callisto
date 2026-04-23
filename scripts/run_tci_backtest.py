"""
TCI Backtest — NCAAW 2026 Tournament vs DraftKings Closing Lines

Tests multiple hypotheses:
1. Higher-TCI team covers the spread
2. Task cohesion (isolated) predicts ATS
3. Coaching tenure edge predicts ATS
4. Experience ratio predicts ATS
5. Combined TCI predicts totals (Over/Under)
6. Social cohesion as NEGATIVE signal (fade)
"""

import math
import sqlite3

db = sqlite3.connect("memory/callisto.db")
db.row_factory = sqlite3.Row

# Load TCI scores
tci = {}
for r in db.execute(
    "SELECT team_name, tci_score, task_cohesion, social_cohesion, stability_score, "
    "experience_ratio, coaching_tenure_years, coaching_stability, class_balance, "
    "continuity_proxy, religious_affiliation FROM tci_scores WHERE season=2026"
).fetchall():
    tci[r["team_name"]] = dict(r)

# Load closing spreads (home team's line)
spreads = {}
for r in db.execute(
    """SELECT m.home_team, m.away_team, m.commence_time, cl.outcome_name, cl.point
       FROM closing_lines_v2 cl
       JOIN markets m ON cl.market_id = m.market_id
       WHERE m.sport = 'basketball_ncaaw' AND m.market_type = 'spreads'"""
).fetchall():
    date = r["commence_time"][:10]
    key = (date, r["home_team"], r["away_team"])
    if r["outcome_name"] == r["home_team"]:
        spreads[key] = r["point"]

# Load totals
total_lines = {}
for r in db.execute(
    """SELECT m.home_team, m.away_team, m.commence_time, cl.point
       FROM closing_lines_v2 cl
       JOIN markets m ON cl.market_id = m.market_id
       WHERE m.sport = 'basketball_ncaaw' AND m.market_type = 'totals'
         AND cl.outcome_name = 'Over'"""
).fetchall():
    date = r["commence_time"][:10]
    key = (date, r["home_team"], r["away_team"])
    total_lines[key] = r["point"]

# Load game results
results = db.execute(
    """SELECT home_team, away_team, home_score, away_score, total_score,
              spread_result, winner, game_date
       FROM game_results WHERE sport = 'basketball_ncaaw' ORDER BY game_date"""
).fetchall()

W = 120
print("=" * W)
print(f"{'NCAAW TCI BACKTEST — 2026 Tournament (52 games vs DraftKings Closing Lines)':^{W}}")
print("=" * W)

# Build analysis records
games = []
missing_tci = set()

for g in results:
    home, away, date = g["home_team"], g["away_team"], g["game_date"]
    key = (date, home, away)
    h_tci, a_tci = tci.get(home), tci.get(away)
    h_spread = spreads.get(key)
    t_line = total_lines.get(key)

    if not h_tci:
        missing_tci.add(home)
    if not a_tci:
        missing_tci.add(away)

    if h_tci and a_tci and h_spread is not None:
        h_score, a_score = g["home_score"], g["away_score"]
        actual_margin = h_score - a_score
        ats_margin = actual_margin + h_spread
        home_covered = ats_margin > 0
        push = ats_margin == 0

        tci_diff = h_tci["tci_score"] - a_tci["tci_score"]
        tci_pick = "home" if tci_diff > 0 else "away"
        tci_pick_covered = home_covered if tci_pick == "home" else not home_covered

        games.append({
            "date": date, "home": home, "away": away,
            "home_tci": h_tci["tci_score"], "away_tci": a_tci["tci_score"],
            "tci_diff": tci_diff,
            "home_spread": h_spread, "actual_margin": actual_margin,
            "ats_margin": ats_margin, "home_covered": home_covered,
            "tci_pick": tci_pick, "tci_pick_covered": tci_pick_covered,
            "push": push,
            "total_line": t_line,
            "actual_total": h_score + a_score,
            "home_task": h_tci.get("task_cohesion", 0),
            "away_task": a_tci.get("task_cohesion", 0),
            "home_coaching": h_tci.get("coaching_tenure_years", 0),
            "away_coaching": a_tci.get("coaching_tenure_years", 0),
            "home_exp": h_tci.get("experience_ratio", 0),
            "away_exp": a_tci.get("experience_ratio", 0),
            "home_social": h_tci.get("social_cohesion", 0),
            "away_social": a_tci.get("social_cohesion", 0),
            "home_stability": h_tci.get("stability_score", 0),
            "away_stability": a_tci.get("stability_score", 0),
        })

print(f"\nGames with both TCI scores + closing lines: {len(games)}/{len(list(results))}")
if missing_tci:
    print(f"Teams missing TCI data: {missing_tci}")

non_push = [g for g in games if not g["push"]]


def z_test(wins, n, p0=0.5):
    p_hat = wins / n if n else 0
    se = math.sqrt(p0 * (1 - p0) / n) if n else 1
    z = (p_hat - p0) / se if se else 0
    p_val = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p_val


def roi_at_110(wins, losses):
    profit = wins * (100 / 110) - losses * 1.0
    n = wins + losses
    return profit / n * 100 if n else 0


def print_hypothesis(title, wins, losses, extra=""):
    n = wins + losses
    pct = wins / n * 100 if n else 0
    z, p = z_test(wins, n)
    roi = roi_at_110(wins, losses)
    print(f"\n{'=' * W}")
    print(title)
    print("=" * W)
    print(f"  Record: {wins}-{losses} ({pct:.1f}%)  n={n}")
    print(f"  Break-even at -110: 52.4%  |  Edge: {pct - 52.4:+.1f}pp")
    print(f"  z={z:.3f}  p={p:.4f}  {'* SIGNIFICANT at .10' if p < 0.10 else '** SIGNIFICANT at .05' if p < 0.05 else 'not significant'}")
    print(f"  Hypothetical ROI at -110: {roi:+.1f}%")
    if extra:
        print(extra)


# === H1: Higher TCI covers ===
w1 = sum(1 for g in non_push if g["tci_pick_covered"])
l1 = len(non_push) - w1
print_hypothesis("H1: Higher-TCI team covers the spread", w1, l1)

# Sub-analysis by TCI differential magnitude
extra = "\n  By TCI differential magnitude:"
for thresh in [0, 5, 10, 15, 20]:
    sub = [g for g in non_push if abs(g["tci_diff"]) >= thresh]
    if sub:
        sw = sum(1 for g in sub if g["tci_pick_covered"])
        sl = len(sub) - sw
        sp = sw / len(sub) * 100
        extra += f"\n    |diff| >= {thresh:2}: {sw}-{sl} ({sp:.1f}%) n={len(sub)}"
print(extra)

# === H2: Task cohesion covers ===
w2 = sum(1 for g in non_push
         if (g["home_task"] > g["away_task"] and g["home_covered"]) or
            (g["away_task"] > g["home_task"] and not g["home_covered"]))
l2 = len(non_push) - w2
print_hypothesis("H2: Higher TASK cohesion team covers ATS", w2, l2)

# === H3: Coaching tenure ===
coach_games = [g for g in non_push if g["home_coaching"] != g["away_coaching"]]
w3 = sum(1 for g in coach_games
         if (g["home_coaching"] > g["away_coaching"] and g["home_covered"]) or
            (g["away_coaching"] > g["home_coaching"] and not g["home_covered"]))
l3 = len(coach_games) - w3
print_hypothesis("H3: Longer coaching tenure covers ATS", w3, l3,
                 f"  (excluded {len(non_push) - len(coach_games)} games with equal tenure)")

# Sub: coaches 6+ yrs vs <6
long_t = [g for g in non_push if max(g["home_coaching"], g["away_coaching"]) >= 6
          and min(g["home_coaching"], g["away_coaching"]) < 6]
if long_t:
    lt_w = sum(1 for g in long_t
               if (g["home_coaching"] >= 6 and g["home_covered"]) or
                  (g["away_coaching"] >= 6 and not g["home_covered"]))
    lt_l = len(long_t) - lt_w
    lt_p = lt_w / len(long_t) * 100
    print(f"  Sub: 6+ yr coach vs <6yr: {lt_w}-{lt_l} ({lt_p:.1f}%) n={len(long_t)}")

# === H4: Experience ratio ===
w4 = sum(1 for g in non_push
         if (g["home_exp"] > g["away_exp"] and g["home_covered"]) or
            (g["away_exp"] > g["home_exp"] and not g["home_covered"]))
l4 = len(non_push) - w4
print_hypothesis("H4: Higher experience ratio (upperclassmen %) covers ATS", w4, l4)

# === H5: Totals ===
tg = [g for g in games if g.get("total_line")]
combined_tcis = sorted([g["home_tci"] + g["away_tci"] for g in tg])
median_comb = combined_tcis[len(combined_tcis) // 2] if combined_tcis else 0

low_tci_g = [g for g in tg if g["home_tci"] + g["away_tci"] < median_comb]
high_tci_g = [g for g in tg if g["home_tci"] + g["away_tci"] >= median_comb]

ov_lo = sum(1 for g in low_tci_g if g["actual_total"] > g["total_line"])
un_lo = sum(1 for g in low_tci_g if g["actual_total"] < g["total_line"])
ov_hi = sum(1 for g in high_tci_g if g["actual_total"] > g["total_line"])
un_hi = sum(1 for g in high_tci_g if g["actual_total"] < g["total_line"])

print(f"\n{'=' * W}")
print("H5: Combined TCI predicts totals")
print("=" * W)
print(f"  Median combined TCI: {median_comb:.1f}")
print(f"  Low combined TCI (< {median_comb:.0f}):  Over {ov_lo} / Under {un_lo}  n={len(low_tci_g)}  (thesis: Over)")
print(f"  High combined TCI (>= {median_comb:.0f}): Over {ov_hi} / Under {un_hi}  n={len(high_tci_g)}  (thesis: Under)")
# Low TCI -> Over hit rate
if low_tci_g:
    lo_non_push = [g for g in low_tci_g if g["actual_total"] != g["total_line"]]
    lo_hit = ov_lo / len(lo_non_push) * 100 if lo_non_push else 0
    print(f"  Low-TCI Over hit rate: {lo_hit:.1f}%")
if high_tci_g:
    hi_non_push = [g for g in high_tci_g if g["actual_total"] != g["total_line"]]
    hi_hit = un_hi / len(hi_non_push) * 100 if hi_non_push else 0
    print(f"  High-TCI Under hit rate: {hi_hit:.1f}%")

# === H6: Fade social cohesion ===
w6 = sum(1 for g in non_push
         if (g["home_social"] < g["away_social"] and g["home_covered"]) or
            (g["away_social"] < g["home_social"] and not g["home_covered"]))
l6 = len(non_push) - w6
print_hypothesis("H6: FADE high social cohesion (lower social cohesion covers)", w6, l6)

# === H7: Stability score edge ===
w7 = sum(1 for g in non_push
         if (g["home_stability"] > g["away_stability"] and g["home_covered"]) or
            (g["away_stability"] > g["home_stability"] and not g["home_covered"]))
l7 = len(non_push) - w7
print_hypothesis("H7: Higher stability score covers ATS", w7, l7)

# === UPSET ANALYSIS: TCI in close games ===
close = [g for g in non_push if abs(g["home_spread"]) <= 7.5]
if close:
    cw = sum(1 for g in close if g["tci_pick_covered"])
    cl_ = len(close) - cw
    print_hypothesis("H8: Higher-TCI covers in CLOSE games (spread <= 7.5)", cw, cl_)

# === GAME-BY-GAME ===
print(f"\n{'=' * W}")
print(f"{'GAME-BY-GAME DETAIL':^{W}}")
print("=" * W)
hdr = f"{'Date':10} {'Home':26} {'Away':26} {'HmTCI':>6} {'AwTCI':>6} {'Diff':>6} {'Sprd':>6} {'Marg':>6} {'ATS':>4} {'TCI':>4}"
print(hdr)
print("-" * W)

for g in games:
    ats = "W" if g["home_covered"] else ("P" if g["push"] else "L")
    tci_r = "HIT" if g["tci_pick_covered"] else "MISS"
    arrow = "H" if g["tci_pick"] == "home" else "A"
    print(
        f"{g['date']}  {g['home'][:24]:24}  {g['away'][:24]:24}"
        f"  {g['home_tci']:5.1f} {g['away_tci']:5.1f} {g['tci_diff']:+5.1f}"
        f"  {g['home_spread']:+5.1f} {g['actual_margin']:+5.0f}"
        f"  {ats}({arrow}) {tci_r}"
    )

# === SUMMARY ===
print(f"\n{'=' * W}")
print(f"{'BACKTEST SUMMARY':^{W}}")
print("=" * W)
hyps = [
    ("H1: Composite TCI ATS", w1, l1),
    ("H2: Task Cohesion ATS", w2, l2),
    ("H3: Coaching Tenure ATS", w3, l3),
    ("H4: Experience Ratio ATS", w4, l4),
    ("H6: Fade Social Cohesion", w6, l6),
    ("H7: Stability Score ATS", w7, l7),
]
if close:
    hyps.append(("H8: Close-game TCI ATS", cw, cl_))

print(f"  {'Hypothesis':30} {'W':>3} {'L':>3} {'Win%':>6} {'Edge':>6} {'ROI':>7} {'z':>6} {'p':>7} {'Sig':>4}")
print(f"  {'-' * 85}")
for name, w, l in hyps:
    n = w + l
    p = w / n * 100 if n else 0
    z, pv = z_test(w, n)
    r = roi_at_110(w, l)
    sig = "*" if pv < 0.10 else ("**" if pv < 0.05 else "")
    print(f"  {name:30} {w:3} {l:3} {p:5.1f}% {p - 52.4:+5.1f}pp {r:+6.1f}% {z:+5.2f}  {pv:.4f} {sig}")

print(f"\n  NOTES:")
print(f"  - n={len(non_push)} is small; need 200+ games for reliable significance")
print(f"  - All hypotheses use -110 standard juice")
print(f"  - Break-even at -110 = 52.4%")
print(f"  - Only {len(tci)} of 68 tournament teams have TCI data (41 computed)")

db.close()
