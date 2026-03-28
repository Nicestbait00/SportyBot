"""
SportyBot Match Scorer
Scores matches for different markets using multi-factor analysis.

Each scorer returns a confidence 0-100 based on concrete, auditable signals.
No magic numbers — every weight is documented.

Extensibility: pass `extra_signals` dict to score_match() to inject additional
data sources. Each scoring function checks for relevant keys and applies
bonus/penalty adjustments on top of the base score.

Supported extra_signals keys (add as data becomes available):
  - "xg_home": float          — expected goals for home team (e.g. from FBref)
  - "xg_away": float          — expected goals for away team
  - "home_injuries": int      — number of key players injured/suspended
  - "away_injuries": int      — number of key players injured/suspended
  - "home_missing_star": bool  — star player missing (top scorer/creator)
  - "away_missing_star": bool  — star player missing
  - "h2h_results": list[dict]  — head-to-head results [{home_goals, away_goals}, ...]
  - "weather": str             — "rain", "snow", "wind" etc (future)
  - "motivation": dict         — {"home": float, "away": float} 0-1 scale
                                 (e.g. relegation battle = 1.0, nothing to play for = 0.3)
"""

from __future__ import annotations
import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def score_match(
    home_form: dict,
    away_form: dict,
    home_results: list,
    away_results: list,
    extra_signals: Optional[dict] = None,
) -> dict:
    """
    Score a match across all markets.

    Args:
        home_form: Summarized form dict for home team
        away_form: Summarized form dict for away team
        home_results: Raw match results for home team
        away_results: Raw match results for away team
        extra_signals: Optional dict of additional data (xG, injuries, h2h, etc.)

    Returns dict with keys for each market:
        "home_win": {"confidence": int, "reasons": list[str]}
        "away_win": {"confidence": int, "reasons": list[str]}
        "over_0.5": {"confidence": int, "reasons": list[str]}
        "over_1.5": {"confidence": int, "reasons": list[str]}
        "over_2.5": {"confidence": int, "reasons": list[str]}
        "btts":     {"confidence": int, "reasons": list[str]}
    """
    sig = extra_signals or {}
    hw = _score_home_win(home_form, away_form, home_results, away_results, sig)
    aw = _score_away_win(home_form, away_form, home_results, away_results, sig)
    over_05 = _score_over(home_form, away_form, home_results, away_results, 0.5, sig)
    over_15 = _score_over(home_form, away_form, home_results, away_results, 1.5, sig)
    over_25 = _score_over(home_form, away_form, home_results, away_results, 2.5, sig)
    btts = _score_btts(home_form, away_form, home_results, away_results, sig)

    return {
        "home_win": hw,
        "away_win": aw,
        "over_0.5": over_05,
        "over_1.5": over_15,
        "over_2.5": over_25,
        "btts": btts,
        # ── Extended markets (derived from core scores) ──
        "draw": _score_draw(home_form, away_form, home_results, away_results, sig),
        "double_chance_1x": _score_double_chance(hw, "draw", home_form, away_form, home_results, away_results, sig, "1X"),
        "double_chance_x2": _score_double_chance(aw, "draw", home_form, away_form, home_results, away_results, sig, "X2"),
        "double_chance_12": _score_double_chance(hw, aw, home_form, away_form, home_results, away_results, sig, "12"),
        "draw_no_bet_home": _score_draw_no_bet(hw, home_form, away_form, sig, "home"),
        "draw_no_bet_away": _score_draw_no_bet(aw, home_form, away_form, sig, "away"),
        "odd_goals": _score_odd_even(home_form, away_form, home_results, away_results, "odd"),
        "even_goals": _score_odd_even(home_form, away_form, home_results, away_results, "even"),
        "home_clean_sheet": _score_clean_sheet(home_form, away_form, home_results, away_results, "home"),
        "away_clean_sheet": _score_clean_sheet(home_form, away_form, home_results, away_results, "away"),
        "ht_home": _score_ht_result(home_form, away_form, home_results, away_results, "home"),
        "ht_draw": _score_ht_result(home_form, away_form, home_results, away_results, "draw"),
        "ht_away": _score_ht_result(home_form, away_form, home_results, away_results, "away"),
        "ht_over_0.5": _score_ht_over(home_form, away_form, home_results, away_results, 0.5),
        "ht_over_1.5": _score_ht_over(home_form, away_form, home_results, away_results, 1.5),
        "ht_btts": _score_ht_btts(home_form, away_form, home_results, away_results),
        # Combo markets
        "home_and_gg": _score_combo(hw, btts, "Home & GG"),
        "home_and_ng": _score_combo_neg(hw, btts, "Home & NG"),
        "away_and_gg": _score_combo(aw, btts, "Away & GG"),
        "away_and_ng": _score_combo_neg(aw, btts, "Away & NG"),
        "home_and_over_2.5": _score_combo(hw, over_25, "Home & Over 2.5"),
        "away_and_over_2.5": _score_combo(aw, over_25, "Away & Over 2.5"),
        "over_2.5_and_gg": _score_combo(over_25, btts, "Over 2.5 & GG"),
        "under_2.5_and_ng": _score_combo_neg(over_25, btts, "Under 2.5 & NG"),
        # Home/Away team totals
        "home_over_0.5": _score_team_over(home_form, home_results, 0.5, "home"),
        "home_over_1.5": _score_team_over(home_form, home_results, 1.5, "home"),
        "away_over_0.5": _score_team_over(away_form, away_results, 0.5, "away"),
        "away_over_1.5": _score_team_over(away_form, away_results, 1.5, "away"),
        # Conditional OR markets — two ways to win
        "home_or_over_2.5": _score_conditional_or(hw, over_25, "Home Or Over 2.5"),
        "home_or_under_2.5": _score_conditional_or(hw, {"confidence": 100 - over_25["confidence"], "reasons": ["Under 2.5"]}, "Home Or Under 2.5"),
        "draw_or_over_2.5": _score_conditional_or(_score_draw(home_form, away_form, home_results, away_results, sig), over_25, "Draw Or Over 2.5"),
        "draw_or_under_2.5": _score_conditional_or(_score_draw(home_form, away_form, home_results, away_results, sig), {"confidence": 100 - over_25["confidence"], "reasons": ["Under 2.5"]}, "Draw Or Under 2.5"),
        "away_or_over_2.5": _score_conditional_or(aw, over_25, "Away Or Over 2.5"),
        "away_or_under_2.5": _score_conditional_or(aw, {"confidence": 100 - over_25["confidence"], "reasons": ["Under 2.5"]}, "Away Or Under 2.5"),
        "home_or_gg": _score_conditional_or(hw, btts, "Home Or GG"),
        "draw_or_gg": _score_conditional_or(_score_draw(home_form, away_form, home_results, away_results, sig), btts, "Draw Or GG"),
        "away_or_gg": _score_conditional_or(aw, btts, "Away Or GG"),
    }


# ── Home Win ────────────────────────────────────────────────────────────────

def _score_home_win(hf: dict, af: dict, hr: list, ar: list, sig: dict = {}) -> dict:
    """
    Multi-factor home win confidence.

    Factors (total weight = 100):
      1. Home win rate overall          (15)
      2. Home win rate AT HOME          (20) — venue matters most for wins
      3. Away team loss rate overall     (15)
      4. Away team loss rate AWAY        (20) — how they perform on the road
      5. Recent form (last 3 games)      (15) — momentum
      6. Goal difference                 (15) — quality signal
    """
    reasons = []

    # 1. Overall home win rate (15 pts)
    home_wr = hf["wins"] / max(hf["played"], 1)
    s1 = home_wr * 15

    # 2. Home win rate at home (20 pts)
    if hf["home_played"] >= 2:
        home_home_wr = hf["home_wins"] / hf["home_played"]
        s2 = home_home_wr * 20
        reasons.append(f"They've won {hf['home_wins']} of {hf['home_played']} games at home")
    else:
        home_home_wr = home_wr
        s2 = home_wr * 15  # Less weight if insufficient home data

    # 3. Away team overall loss rate (15 pts)
    away_lr = af["losses"] / max(af["played"], 1)
    s3 = away_lr * 15

    # 4. Away team loss rate when away (20 pts)
    if af["away_played"] >= 2:
        away_away_lr = af["away_losses"] / af["away_played"]
        s4 = away_away_lr * 20
        reasons.append(f"The away team has lost {af['away_losses']} of {af['away_played']} games on the road")
    else:
        away_away_lr = away_lr
        s4 = away_lr * 15

    # 5. Recent momentum — last 3 results weighted heavier (15 pts)
    recent_home = hr[:3] if len(hr) >= 3 else hr
    recent_away = ar[:3] if len(ar) >= 3 else ar

    home_recent_wins = sum(1 for r in recent_home if r["result"] == "W")
    away_recent_losses = sum(1 for r in recent_away if r["result"] == "L")

    momentum = (home_recent_wins / max(len(recent_home), 1) +
                away_recent_losses / max(len(recent_away), 1)) / 2
    s5 = momentum * 15

    if home_recent_wins >= 2:
        reasons.append(f"Home team on a {home_recent_wins}-game winning streak")
    if away_recent_losses >= 2:
        reasons.append(f"The away team lost {away_recent_losses} of their last 3 games")

    # 6. Goal difference quality (15 pts)
    home_gd_per_game = (hf["avg_scored"] - hf["avg_conceded"])
    away_gd_per_game = (af["avg_scored"] - af["avg_conceded"])

    # Normalize: +2 GD/game → full marks, -2 → 0
    gd_advantage = (home_gd_per_game - away_gd_per_game) / 4  # range ~ -1 to 1
    gd_advantage = max(0, min(1, (gd_advantage + 1) / 2))  # normalize to 0-1
    s6 = gd_advantage * 15

    raw = s1 + s2 + s3 + s4 + s5 + s6

    # ── Extra signals adjustments ──
    raw += _apply_win_signals(sig, "home", reasons)

    confidence = max(0, min(95, int(raw)))

    reasons.insert(0, f"Home team form: {hf['form_string']} — won {hf['wins']} of last {hf['played']} games")

    return {"confidence": confidence, "reasons": reasons}


def _score_away_win(hf: dict, af: dict, hr: list, ar: list, sig: dict = {}) -> dict:
    """Mirror of home win — away team's perspective."""
    reasons = []

    away_wr = af["wins"] / max(af["played"], 1)
    s1 = away_wr * 15

    if af["away_played"] >= 2:
        away_away_wr = af["away_wins"] / af["away_played"]
        s2 = away_away_wr * 20
        reasons.append(f"They've won {af['away_wins']} of {af['away_played']} games away from home")
    else:
        away_away_wr = away_wr
        s2 = away_wr * 15

    home_lr = hf["losses"] / max(hf["played"], 1)
    s3 = home_lr * 15

    if hf["home_played"] >= 2:
        home_home_lr = hf["home_losses"] / hf["home_played"]
        s4 = home_home_lr * 20
        reasons.append(f"The home team has lost {hf['home_losses']} of {hf['home_played']} home games")
    else:
        home_home_lr = home_lr
        s4 = home_lr * 15

    recent_away = ar[:3] if len(ar) >= 3 else ar
    recent_home = hr[:3] if len(hr) >= 3 else hr

    away_recent_wins = sum(1 for r in recent_away if r["result"] == "W")
    home_recent_losses = sum(1 for r in recent_home if r["result"] == "L")

    momentum = (away_recent_wins / max(len(recent_away), 1) +
                home_recent_losses / max(len(recent_home), 1)) / 2
    s5 = momentum * 15

    gd_advantage = (af["avg_scored"] - af["avg_conceded"] - hf["avg_scored"] + hf["avg_conceded"]) / 4
    gd_advantage = max(0, min(1, (gd_advantage + 1) / 2))
    s6 = gd_advantage * 15

    raw = s1 + s2 + s3 + s4 + s5 + s6

    # ── Extra signals adjustments ──
    raw += _apply_win_signals(sig, "away", reasons)

    confidence = max(0, min(95, int(raw)))

    reasons.insert(0, f"Away team form: {af['form_string']} — won {af['wins']} of last {af['played']} games")

    return {"confidence": confidence, "reasons": reasons}


# ── Over/Under ──────────────────────────────────────────────────────────────

def _score_over(hf: dict, af: dict, hr: list, ar: list, threshold: float, sig: dict = {}) -> dict:
    """
    Multi-factor over confidence.

    Factors (total weight = 100):
      1. Historical over rate — BOTH teams (30)
         How many of their last 10 games actually went over this line?
      2. Venue-adjusted expected goals (20)
         Home team's home scoring + away team's away conceding
      3. Poisson probability from expected goals (20)
      4. Recent scoring trend — last 3 games (15)
         Are they scoring MORE or LESS than their average recently?
      5. Combined attack vs defence mismatch (15)
         Strong attack vs weak defence = goals
    """
    reasons = []

    # 1. Historical over rate — the most reliable signal (30 pts)
    home_over_count = sum(
        1 for r in hr
        if (r["goals_for"] + r["goals_against"]) > threshold
    )
    away_over_count = sum(
        1 for r in ar
        if (r["goals_for"] + r["goals_against"]) > threshold
    )

    home_over_rate = home_over_count / max(len(hr), 1)
    away_over_rate = away_over_count / max(len(ar), 1)
    combined_over_rate = (home_over_rate + away_over_rate) / 2
    s1 = combined_over_rate * 30

    reasons.append(
        f"Over {threshold} goals landed in {home_over_count} of {len(hr)} home team games "
        f"and {away_over_count} of {len(ar)} away team games"
    )

    # 2. Venue-adjusted expected goals (20 pts)
    # Home team's scoring at home + away team's conceding away = more realistic
    if hf["home_played"] >= 2 and af["away_played"] >= 2:
        venue_expected = (
            (hf["home_avg_scored"] + af["away_avg_conceded"]) / 2 +
            (af["away_avg_scored"] + hf["home_avg_conceded"]) / 2
        )
    else:
        venue_expected = (
            (hf["avg_scored"] + af["avg_conceded"]) / 2 +
            (af["avg_scored"] + hf["avg_conceded"]) / 2
        )

    # How far above the threshold is expected goals?
    excess = max(0, venue_expected - threshold)
    # 1 goal excess → ~15/20 pts, 2+ → full marks
    s2 = min(1, excess / 2) * 20

    reasons.append(f"Based on venue stats, we expect around {venue_expected:.1f} goals in this match")

    # 3. Poisson probability (20 pts)
    poisson_prob = _poisson_over_prob(venue_expected, threshold)
    s3 = poisson_prob * 20

    # 4. Recent scoring trend — last 3 games (15 pts)
    recent_home = hr[:3] if len(hr) >= 3 else hr
    recent_away = ar[:3] if len(ar) >= 3 else ar

    recent_home_goals = sum(r["goals_for"] + r["goals_against"] for r in recent_home)
    recent_away_goals = sum(r["goals_for"] + r["goals_against"] for r in recent_away)

    recent_avg_total = (
        recent_home_goals / max(len(recent_home), 1) +
        recent_away_goals / max(len(recent_away), 1)
    ) / 2

    # Are recent games higher scoring than average?
    overall_avg = (hf["avg_scored"] + hf["avg_conceded"] + af["avg_scored"] + af["avg_conceded"]) / 2
    trend_boost = max(0, (recent_avg_total - overall_avg) / 2)  # Positive = trending up
    s4 = min(1, (recent_avg_total - threshold) / 2) * 10 + min(1, trend_boost + 0.5) * 5
    s4 = max(0, s4)

    if recent_avg_total > threshold + 0.5:
        reasons.append(f"Recent games are averaging {recent_avg_total:.1f} goals — scoring is trending up")

    # 5. Attack vs defence mismatch (15 pts)
    # Home attack vs away defence + away attack vs home defence
    home_attack_strength = hf["avg_scored"]
    away_defence_weakness = af["avg_conceded"]
    away_attack_strength = af["avg_scored"]
    home_defence_weakness = hf["avg_conceded"]

    attack_mismatch = (
        max(0, home_attack_strength - 1.0) +  # Bonus for scoring above 1.0/game
        max(0, away_defence_weakness - 1.0) +  # Bonus for conceding above 1.0/game
        max(0, away_attack_strength - 0.8) +
        max(0, home_defence_weakness - 0.8)
    ) / 4
    s5 = min(1, attack_mismatch) * 15

    raw = s1 + s2 + s3 + s4 + s5

    # ── Extra signals adjustments ──
    raw += _apply_goals_signals(sig, threshold, reasons)

    confidence = max(0, min(95, int(raw)))

    return {"confidence": confidence, "reasons": reasons}


# ── BTTS ────────────────────────────────────────────────────────────────────

def _score_btts(hf: dict, af: dict, hr: list, ar: list, sig: dict = {}) -> dict:
    """
    Multi-factor BTTS (Both Teams To Score) confidence.

    Factors (total weight = 100):
      1. Historical BTTS rate — both teams (35)
      2. Both teams scoring consistency (25)
         How often does each team score at least 1?
      3. Defensive vulnerability (20)
         How often does each team concede at least 1?
      4. Venue-specific scoring (20)
         Home team scores at home + away team scores away
    """
    reasons = []

    # 1. Historical BTTS rate (35 pts)
    home_btts = sum(
        1 for r in hr
        if r["goals_for"] >= 1 and r["goals_against"] >= 1
    )
    away_btts = sum(
        1 for r in ar
        if r["goals_for"] >= 1 and r["goals_against"] >= 1
    )

    home_btts_rate = home_btts / max(len(hr), 1)
    away_btts_rate = away_btts / max(len(ar), 1)
    combined_btts_rate = (home_btts_rate + away_btts_rate) / 2
    s1 = combined_btts_rate * 35

    reasons.append(
        f"Both teams scored in {home_btts} of {len(hr)} home team games "
        f"and {away_btts} of {len(ar)} away team games"
    )

    # 2. Scoring consistency — how often each team scores at least 1 (25 pts)
    home_scores = sum(1 for r in hr if r["goals_for"] >= 1)
    away_scores = sum(1 for r in ar if r["goals_for"] >= 1)

    home_score_rate = home_scores / max(len(hr), 1)
    away_score_rate = away_scores / max(len(ar), 1)

    # BTTS needs BOTH to score — use the minimum as the bottleneck
    scoring_consistency = min(home_score_rate, away_score_rate)
    s2 = scoring_consistency * 25

    if scoring_consistency >= 0.7:
        reasons.append(f"Both teams find the net regularly — home scores in {home_score_rate*100:.0f}% of games, away in {away_score_rate*100:.0f}%")

    # 3. Defensive vulnerability — how often each team concedes (20 pts)
    home_concedes = sum(1 for r in hr if r["goals_against"] >= 1)
    away_concedes = sum(1 for r in ar if r["goals_against"] >= 1)

    home_concede_rate = home_concedes / max(len(hr), 1)
    away_concede_rate = away_concedes / max(len(ar), 1)

    defence_weakness = min(home_concede_rate, away_concede_rate)
    s3 = defence_weakness * 20

    # 4. Venue-specific (20 pts)
    if hf["home_played"] >= 2:
        home_venue_gs = hf["home_avg_scored"]
    else:
        home_venue_gs = hf["avg_scored"]

    if af["away_played"] >= 2:
        away_venue_gs = af["away_avg_scored"]
    else:
        away_venue_gs = af["avg_scored"]

    # Both need to be scoring > 0.5/game at their venue
    venue_scoring = min(home_venue_gs, away_venue_gs)
    s4 = min(1, venue_scoring) * 20

    if venue_scoring >= 1.0:
        reasons.append(f"At their venues, home team averages {home_venue_gs:.1f} goals/game and away team {away_venue_gs:.1f}")

    raw = s1 + s2 + s3 + s4

    # ── Extra signals adjustments ──
    raw += _apply_goals_signals(sig, 0.5, reasons)  # BTTS ~ goals market

    confidence = max(0, min(95, int(raw)))

    return {"confidence": confidence, "reasons": reasons}


# ── Draw ──────────────────────────────────────────────────────────────────

def _score_draw(hf: dict, af: dict, hr: list, ar: list, sig: dict = {}) -> dict:
    """Score likelihood of a draw."""
    reasons = []

    # Draw rate for both teams
    home_draws = hf["draws"] / max(hf["played"], 1)
    away_draws = af["draws"] / max(af["played"], 1)
    combined = (home_draws + away_draws) / 2
    s1 = combined * 40

    reasons.append(f"Home team drew {hf['draws']} of {hf['played']} games, away team drew {af['draws']} of {af['played']}")

    # Closeness of team quality (small GD gap = more draws)
    home_gd = hf["avg_scored"] - hf["avg_conceded"]
    away_gd = af["avg_scored"] - af["avg_conceded"]
    gd_gap = abs(home_gd - away_gd)
    closeness = max(0, 1 - gd_gap / 2)
    s2 = closeness * 30

    if closeness > 0.6:
        reasons.append(f"These teams are closely matched — very similar goal difference")

    # Recent draws
    recent_home_draws = sum(1 for r in hr[:5] if r["result"] == "D")
    recent_away_draws = sum(1 for r in ar[:5] if r["result"] == "D")
    s3 = ((recent_home_draws + recent_away_draws) / 10) * 30

    raw = s1 + s2 + s3
    confidence = max(0, min(95, int(raw)))
    return {"confidence": confidence, "reasons": reasons}


# ── Double Chance ────────────────────────────────────────────────────────

def _score_double_chance(win_score: dict, other: any, hf: dict, af: dict, hr: list, ar: list, sig: dict, label: str) -> dict:
    """Score double chance market (1X, X2, 12)."""
    reasons = []

    if label == "1X":
        # Home or Draw — complement of Away Win
        home_non_loss = (hf["wins"] + hf["draws"]) / max(hf["played"], 1)
        conf = int(min(95, home_non_loss * 80 + 10))
        reasons.append(f"Home team avoids defeat in {home_non_loss*100:.0f}% of games")
    elif label == "X2":
        away_non_loss = (af["wins"] + af["draws"]) / max(af["played"], 1)
        conf = int(min(95, away_non_loss * 80 + 10))
        reasons.append(f"Away team avoids defeat in {away_non_loss*100:.0f}% of games")
    elif label == "12":
        # Either team wins — complement of Draw
        home_draws = hf["draws"] / max(hf["played"], 1)
        away_draws = af["draws"] / max(af["played"], 1)
        draw_prob = (home_draws + away_draws) / 2
        conf = int(min(95, (1 - draw_prob) * 85 + 5))
        reasons.append(f"A decisive result (no draw) happens in {(1-draw_prob)*100:.0f}% of these teams' games")
    else:
        conf = 50

    return {"confidence": conf, "reasons": reasons}


# ── Draw No Bet ──────────────────────────────────────────────────────────

def _score_draw_no_bet(win_score: dict, hf: dict, af: dict, sig: dict, side: str) -> dict:
    """Score Draw No Bet — essentially a boosted win confidence."""
    # DNB is safer than straight win — draws refund your stake
    base_conf = win_score["confidence"]
    # Boost by estimated draw probability (since draws don't lose)
    draw_boost = min(15, int((hf["draws"] + af["draws"]) / max(hf["played"] + af["played"], 1) * 30))
    conf = min(95, base_conf + draw_boost)
    reasons = win_score["reasons"][:] + [f"If it's a draw you get your stake back — adds {draw_boost}% safety"]
    return {"confidence": conf, "reasons": reasons}


# ── Odd/Even ─────────────────────────────────────────────────────────────

def _score_odd_even(hf: dict, af: dict, hr: list, ar: list, parity: str) -> dict:
    """Score Odd or Even total goals."""
    reasons = []

    # Count odd/even totals in recent games
    all_results = hr + ar
    totals = [r["goals_for"] + r["goals_against"] for r in all_results]

    if parity == "odd":
        count = sum(1 for t in totals if t % 2 == 1)
    else:
        count = sum(1 for t in totals if t % 2 == 0)

    rate = count / max(len(totals), 1)
    # Odd/Even is close to 50/50 — confidence rarely goes above 65
    conf = int(min(70, 30 + rate * 45))
    reasons.append(f"An {parity} number of goals was scored in {count} of {len(totals)} recent games ({rate*100:.0f}%)")

    return {"confidence": conf, "reasons": reasons}


# ── Clean Sheet ──────────────────────────────────────────────────────────

def _score_clean_sheet(hf: dict, af: dict, hr: list, ar: list, side: str) -> dict:
    """Score clean sheet probability."""
    reasons = []

    if side == "home":
        # Home team keeping a clean sheet = opponent doesn't score
        cs_count = sum(1 for r in hr if r["goals_against"] == 0)
        cs_rate = cs_count / max(len(hr), 1)
        opp_fail = sum(1 for r in ar if r["goals_for"] == 0)
        opp_fail_rate = opp_fail / max(len(ar), 1)
        combined = (cs_rate * 0.6 + opp_fail_rate * 0.4)
        reasons.append(f"Home team kept a clean sheet in {cs_count} of {len(hr)} games, away team failed to score in {opp_fail} of {len(ar)}")
    else:
        cs_count = sum(1 for r in ar if r["goals_against"] == 0)
        cs_rate = cs_count / max(len(ar), 1)
        opp_fail = sum(1 for r in hr if r["goals_for"] == 0)
        opp_fail_rate = opp_fail / max(len(hr), 1)
        combined = (cs_rate * 0.6 + opp_fail_rate * 0.4)
        reasons.append(f"Away team kept a clean sheet in {cs_count} of {len(ar)} games, home team failed to score in {opp_fail} of {len(hr)}")

    conf = int(min(90, combined * 85 + 5))
    return {"confidence": conf, "reasons": reasons}


# ── Half-Time Markets ────────────────────────────────────────────────────

def _score_ht_result(hf: dict, af: dict, hr: list, ar: list, result_type: str) -> dict:
    """Score half-time result. Uses full-time data as proxy with dampening."""
    reasons = []

    # HT is harder to predict — dampen full-time signals
    if result_type == "home":
        base_rate = hf["wins"] / max(hf["played"], 1)
        conf = int(min(80, base_rate * 55 + 10))
        reasons.append(f"Home team wins {hf['wins']} of {hf['played']} full-time — adjusted down for half-time")
    elif result_type == "away":
        base_rate = af["wins"] / max(af["played"], 1)
        conf = int(min(80, base_rate * 50 + 8))
        reasons.append(f"Away team wins {af['wins']} of {af['played']} full-time — adjusted down for half-time")
    else:
        # Draws are more common at HT
        draw_rate = (hf["draws"] + af["draws"]) / max(hf["played"] + af["played"], 1)
        conf = int(min(80, draw_rate * 60 + 25))
        reasons.append(f"Draws are more common at half-time — draw tendency is {draw_rate*100:.0f}%")

    return {"confidence": conf, "reasons": reasons}


def _score_ht_over(hf: dict, af: dict, hr: list, ar: list, threshold: float) -> dict:
    """Score 1st Half Over. Uses overall scoring rate * ~0.45 (HT share of goals)."""
    reasons = []

    ht_factor = 0.45  # ~45% of goals scored in 1st half on average
    expected_total = (hf["avg_scored"] + af["avg_conceded"] + af["avg_scored"] + hf["avg_conceded"]) / 2
    ht_expected = expected_total * ht_factor

    excess = max(0, ht_expected - threshold)
    conf = int(min(85, 25 + excess * 35))
    reasons.append(f"We expect ~{ht_expected:.1f} goals by half-time based on {expected_total:.1f} expected for the full match")

    return {"confidence": conf, "reasons": reasons}


def _score_ht_btts(hf: dict, af: dict, hr: list, ar: list) -> dict:
    """Score 1st Half BTTS. Much harder than FT BTTS — dampen heavily."""
    reasons = []

    # FT BTTS rate * dampening factor
    home_btts = sum(1 for r in hr if r["goals_for"] >= 1 and r["goals_against"] >= 1)
    away_btts = sum(1 for r in ar if r["goals_for"] >= 1 and r["goals_against"] >= 1)
    ft_rate = (home_btts / max(len(hr), 1) + away_btts / max(len(ar), 1)) / 2

    # HT BTTS happens maybe 40% as often as FT BTTS
    ht_rate = ft_rate * 0.4
    conf = int(min(70, ht_rate * 80 + 10))
    reasons.append(f"Both teams score full-time {ft_rate*100:.0f}% of the time — by half-time that drops to ~{ht_rate*100:.0f}%")

    return {"confidence": conf, "reasons": reasons}


# ── Team Totals ──────────────────────────────────────────────────────────

def _score_team_over(form: dict, results: list, threshold: float, side: str) -> dict:
    """Score team-specific Over (home/away team to score over X)."""
    reasons = []

    over_count = sum(1 for r in results if r["goals_for"] > threshold)
    over_rate = over_count / max(len(results), 1)
    avg_scored = form["avg_scored"]

    s1 = over_rate * 50
    excess = max(0, avg_scored - threshold)
    s2 = min(1, excess / 1.5) * 30

    # Poisson for team goals
    poisson_prob = _poisson_over_prob(avg_scored, threshold)
    s3 = poisson_prob * 20

    conf = int(min(95, s1 + s2 + s3))
    reasons.append(f"{side.title()} team scored over {threshold} in {over_count} of {len(results)} games — they average {avg_scored:.1f} goals/game")

    return {"confidence": conf, "reasons": reasons}


# ── Combo Market Helpers ─────────────────────────────────────────────────

def _score_combo(score_a: dict, score_b: dict, label: str) -> dict:
    """Score a combo market (both conditions must hit). Multiply probabilities."""
    prob_a = score_a["confidence"] / 100
    prob_b = score_b["confidence"] / 100
    combined = prob_a * prob_b
    conf = int(min(90, combined * 100))
    reasons = [f"Both conditions need to hit — combined chance is {conf}%"]
    reasons.extend(score_a["reasons"][:1])
    reasons.extend(score_b["reasons"][:1])
    return {"confidence": conf, "reasons": reasons}


def _score_combo_neg(win_score: dict, neg_score: dict, label: str) -> dict:
    """Score combo where second condition is NEGATED (e.g. Home & NG = Home win + NOT BTTS)."""
    prob_a = win_score["confidence"] / 100
    prob_b_neg = 1 - (neg_score["confidence"] / 100)
    combined = prob_a * prob_b_neg
    conf = int(min(90, combined * 100))
    reasons = [f"Both conditions need to hit — combined chance is {conf}%"]
    return {"confidence": conf, "reasons": reasons}


def _score_conditional_or(score_a: dict, score_b: dict, label: str) -> dict:
    """Score a conditional OR market — bet wins if EITHER condition hits.

    P(A or B) = P(A) + P(B) - P(A and B)
    Two ways to win → higher confidence than individual bets, lower odds.
    """
    prob_a = score_a["confidence"] / 100
    prob_b = score_b["confidence"] / 100
    # P(A or B) using inclusion-exclusion
    combined = prob_a + prob_b - (prob_a * prob_b)
    conf = int(min(95, combined * 100))
    reasons = [f"Either condition can win this bet — combined chance is {conf}%"]
    reasons.extend(score_a["reasons"][:1])
    reasons.extend(score_b["reasons"][:1])
    return {"confidence": conf, "reasons": reasons}


# ── Extra Signal Processors ────────────────────────────────────────────────
# Each processor checks for specific keys in the signals dict and returns
# a bonus/penalty (-15 to +15). Add new processors here as data sources
# become available.

def _apply_win_signals(sig: dict, side: str, reasons: list) -> float:
    """Apply extra signals that affect win predictions. Returns score adjustment."""
    adj = 0.0

    # ── xG-based adjustment (when available) ──
    if "xg_home" in sig and "xg_away" in sig:
        xg_diff = sig["xg_home"] - sig["xg_away"]
        if side == "away":
            xg_diff = -xg_diff
        # +1.0 xG advantage → +8 pts, capped at ±10
        xg_bonus = max(-10, min(10, xg_diff * 8))
        adj += xg_bonus
        reasons.append(f"Expected goals model: home {sig['xg_home']:.1f} vs away {sig['xg_away']:.1f}")

    # ── Injury impact ──
    own_key = f"{side}_injuries"
    opp_key = "away_injuries" if side == "home" else "home_injuries"
    if own_key in sig or opp_key in sig:
        own_inj = sig.get(own_key, 0)
        opp_inj = sig.get(opp_key, 0)
        # Each key injury for your side = -3, each for opponent = +2
        inj_adj = opp_inj * 2 - own_inj * 3
        inj_adj = max(-12, min(8, inj_adj))
        adj += inj_adj
        if inj_adj != 0:
            reasons.append(f"{side.title()} team missing {own_inj} key player(s), opponent missing {opp_inj}")

    # ── Star player missing ──
    own_star = sig.get(f"{side}_missing_star", False)
    opp_star = sig.get(("away_missing_star" if side == "home" else "home_missing_star"), False)
    if own_star:
        adj -= 8
        reasons.append(f"⚠ {side.title()} team's star player is missing — big blow")
    if opp_star:
        adj += 5
        reasons.append(f"Opponent's star player is missing — advantage")

    # ── Head-to-head ──
    if "h2h_results" in sig and len(sig["h2h_results"]) >= 3:
        h2h = sig["h2h_results"]
        if side == "home":
            wins = sum(1 for m in h2h if m["home_goals"] > m["away_goals"])
        else:
            wins = sum(1 for m in h2h if m["away_goals"] > m["home_goals"])
        h2h_rate = wins / len(h2h)
        h2h_adj = (h2h_rate - 0.33) * 15  # Above 33% = positive
        h2h_adj = max(-8, min(8, h2h_adj))
        adj += h2h_adj
        if abs(h2h_adj) >= 3:
            reasons.append(f"Head-to-head: won {wins} of the last {len(h2h)} meetings")

    # ── Motivation ──
    if "motivation" in sig:
        own_mot = sig["motivation"].get(side, 0.5)
        opp_mot = sig["motivation"].get("away" if side == "home" else "home", 0.5)
        mot_diff = (own_mot - opp_mot) * 10
        mot_diff = max(-6, min(6, mot_diff))
        adj += mot_diff
        if abs(mot_diff) >= 3:
            reasons.append(f"This team has more to play for — motivation edge")

    return adj


def _apply_goals_signals(sig: dict, threshold: float, reasons: list) -> float:
    """Apply extra signals that affect goals-based markets (Over/Under, BTTS)."""
    adj = 0.0

    # ── xG-based goals expectation ──
    if "xg_home" in sig and "xg_away" in sig:
        xg_total = sig["xg_home"] + sig["xg_away"]
        xg_excess = xg_total - threshold
        # +1 goal above threshold → +8, -1 below → -8
        xg_adj = max(-10, min(10, xg_excess * 8))
        adj += xg_adj
        reasons.append(f"Expected goals model predicts {xg_total:.1f} total goals vs the {threshold} line")

    # ── Injuries → fewer goals? ──
    total_injuries = sig.get("home_injuries", 0) + sig.get("away_injuries", 0)
    if total_injuries >= 3:
        # Many injuries across both teams can go either way, but
        # attacking injuries generally reduce goals
        inj_adj = -min(6, total_injuries * 1.5)
        adj += inj_adj
        reasons.append(f"{total_injuries} key players missing across both teams — could reduce goals")

    # ── Star attackers missing reduces goals ──
    stars_out = (1 if sig.get("home_missing_star") else 0) + (1 if sig.get("away_missing_star") else 0)
    if stars_out:
        star_adj = -stars_out * 5
        adj += star_adj
        reasons.append(f"Star attacker(s) missing — expect fewer goals")

    # ── H2H goals history ──
    if "h2h_results" in sig and len(sig["h2h_results"]) >= 3:
        h2h = sig["h2h_results"]
        h2h_avg = sum(m["home_goals"] + m["away_goals"] for m in h2h) / len(h2h)
        h2h_excess = h2h_avg - threshold
        h2h_adj = max(-8, min(8, h2h_excess * 5))
        adj += h2h_adj
        if abs(h2h_adj) >= 3:
            reasons.append(f"These teams average {h2h_avg:.1f} goals when they meet")

    return adj


# ── Helpers ─────────────────────────────────────────────────────────────────

def _poisson_over_prob(expected: float, threshold: float) -> float:
    """P(total goals > threshold) using Poisson distribution."""
    if expected <= 0:
        return 0.0
    k = int(math.floor(threshold))
    cumulative = 0.0
    for i in range(k + 1):
        cumulative += (expected ** i) * math.exp(-expected) / math.factorial(i)
    return 1.0 - cumulative


def cross_check_with_odds(confidence: int, implied_odds: float) -> dict:
    """
    Cross-check model confidence against market odds.
    Returns adjusted confidence and a warning if there's a big divergence.
    """
    if implied_odds <= 1.0:
        return {"confidence": confidence, "warning": None}

    implied_prob = (1 / implied_odds) * 100  # Market's view

    divergence = confidence - implied_prob

    warning = None
    adjusted = confidence

    if divergence > 20:
        # Model is much more confident than the market — trust market partially
        adjusted = int(confidence * 0.7 + implied_prob * 0.3)
        warning = (
            f"⚠ Our model says {confidence}% but the bookies price it at {implied_prob:.0f}% "
            f"— adjusted to {adjusted}%. The market may know something we don't."
        )
    elif divergence < -20:
        # Market more confident — potential value bet
        adjusted = int(confidence * 0.7 + implied_prob * 0.3)
        warning = (
            f"💡 Bookies rate this at {implied_prob:.0f}% vs our {confidence}% "
            f"— adjusted to {adjusted}%. Could be value here."
        )

    adjusted = max(0, min(95, adjusted))
    return {"confidence": adjusted, "warning": warning}
