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
    return {
        "home_win": _score_home_win(home_form, away_form, home_results, away_results, sig),
        "away_win": _score_away_win(home_form, away_form, home_results, away_results, sig),
        "over_0.5": _score_over(home_form, away_form, home_results, away_results, 0.5, sig),
        "over_1.5": _score_over(home_form, away_form, home_results, away_results, 1.5, sig),
        "over_2.5": _score_over(home_form, away_form, home_results, away_results, 2.5, sig),
        "btts": _score_btts(home_form, away_form, home_results, away_results, sig),
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
        reasons.append(f"Home record: {hf['home_wins']}W/{hf['home_played']} at home")
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
        reasons.append(f"Away losses on road: {af['away_losses']}L/{af['away_played']}")
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
        reasons.append(f"Home on {home_recent_wins}W streak in last 3")
    if away_recent_losses >= 2:
        reasons.append(f"Away lost {away_recent_losses}/3 recent")

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

    reasons.insert(0, f"Form: {hf['form_string']} ← recent ({hf['wins']}W/{hf['played']})")

    return {"confidence": confidence, "reasons": reasons}


def _score_away_win(hf: dict, af: dict, hr: list, ar: list, sig: dict = {}) -> dict:
    """Mirror of home win — away team's perspective."""
    reasons = []

    away_wr = af["wins"] / max(af["played"], 1)
    s1 = away_wr * 15

    if af["away_played"] >= 2:
        away_away_wr = af["away_wins"] / af["away_played"]
        s2 = away_away_wr * 20
        reasons.append(f"Away record: {af['away_wins']}W/{af['away_played']} on road")
    else:
        away_away_wr = away_wr
        s2 = away_wr * 15

    home_lr = hf["losses"] / max(hf["played"], 1)
    s3 = home_lr * 15

    if hf["home_played"] >= 2:
        home_home_lr = hf["home_losses"] / hf["home_played"]
        s4 = home_home_lr * 20
        reasons.append(f"Home losses at home: {hf['home_losses']}L/{hf['home_played']}")
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

    reasons.insert(0, f"Form: {af['form_string']} ← recent ({af['wins']}W/{af['played']})")

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
        f"Over {threshold} hit rate: "
        f"Home {home_over_count}/{len(hr)} ({home_over_rate*100:.0f}%), "
        f"Away {away_over_count}/{len(ar)} ({away_over_rate*100:.0f}%)"
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

    reasons.append(f"Expected goals: {venue_expected:.1f} (venue-adjusted)")

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
        reasons.append(f"Recent avg total: {recent_avg_total:.1f} goals/game (trending up)")

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
        f"BTTS rate: Home {home_btts}/{len(hr)} ({home_btts_rate*100:.0f}%), "
        f"Away {away_btts}/{len(ar)} ({away_btts_rate*100:.0f}%)"
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
        reasons.append(f"Both teams score often: {home_score_rate*100:.0f}% / {away_score_rate*100:.0f}%")

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
        reasons.append(f"Venue scoring: Home {home_venue_gs:.1f}/g, Away {away_venue_gs:.1f}/g")

    raw = s1 + s2 + s3 + s4

    # ── Extra signals adjustments ──
    raw += _apply_goals_signals(sig, 0.5, reasons)  # BTTS ~ goals market

    confidence = max(0, min(95, int(raw)))

    return {"confidence": confidence, "reasons": reasons}


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
        reasons.append(f"xG: {sig['xg_home']:.1f} vs {sig['xg_away']:.1f} ({'+' if xg_bonus > 0 else ''}{xg_bonus:.0f})")

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
            reasons.append(f"Injuries: {side} missing {own_inj}, opp missing {opp_inj} ({'+' if inj_adj > 0 else ''}{inj_adj:.0f})")

    # ── Star player missing ──
    own_star = sig.get(f"{side}_missing_star", False)
    opp_star = sig.get(("away_missing_star" if side == "home" else "home_missing_star"), False)
    if own_star:
        adj -= 8
        reasons.append(f"⚠ {side.title()} star player missing (-8)")
    if opp_star:
        adj += 5
        reasons.append(f"Opponent star player missing (+5)")

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
            reasons.append(f"H2H: {wins}/{len(h2h)} wins ({'+' if h2h_adj > 0 else ''}{h2h_adj:.0f})")

    # ── Motivation ──
    if "motivation" in sig:
        own_mot = sig["motivation"].get(side, 0.5)
        opp_mot = sig["motivation"].get("away" if side == "home" else "home", 0.5)
        mot_diff = (own_mot - opp_mot) * 10
        mot_diff = max(-6, min(6, mot_diff))
        adj += mot_diff
        if abs(mot_diff) >= 3:
            reasons.append(f"Motivation edge: {'+' if mot_diff > 0 else ''}{mot_diff:.0f}")

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
        reasons.append(f"xG total: {xg_total:.1f} vs line {threshold} ({'+' if xg_adj > 0 else ''}{xg_adj:.0f})")

    # ── Injuries → fewer goals? ──
    total_injuries = sig.get("home_injuries", 0) + sig.get("away_injuries", 0)
    if total_injuries >= 3:
        # Many injuries across both teams can go either way, but
        # attacking injuries generally reduce goals
        inj_adj = -min(6, total_injuries * 1.5)
        adj += inj_adj
        reasons.append(f"Combined injuries: {total_injuries} key players out ({inj_adj:.0f})")

    # ── Star attackers missing reduces goals ──
    stars_out = (1 if sig.get("home_missing_star") else 0) + (1 if sig.get("away_missing_star") else 0)
    if stars_out:
        star_adj = -stars_out * 5
        adj += star_adj
        reasons.append(f"Star attacker(s) missing ({star_adj})")

    # ── H2H goals history ──
    if "h2h_results" in sig and len(sig["h2h_results"]) >= 3:
        h2h = sig["h2h_results"]
        h2h_avg = sum(m["home_goals"] + m["away_goals"] for m in h2h) / len(h2h)
        h2h_excess = h2h_avg - threshold
        h2h_adj = max(-8, min(8, h2h_excess * 5))
        adj += h2h_adj
        if abs(h2h_adj) >= 3:
            reasons.append(f"H2H avg goals: {h2h_avg:.1f} ({'+' if h2h_adj > 0 else ''}{h2h_adj:.0f})")

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
            f"⚠ Our analysis says {confidence}% but market odds imply {implied_prob:.0f}% "
            f"→ adjusted to {adjusted}% (market may know something we don't)"
        )
    elif divergence < -20:
        # Market more confident — potential value bet
        adjusted = int(confidence * 0.7 + implied_prob * 0.3)
        warning = (
            f"💡 Market odds imply {implied_prob:.0f}% vs our {confidence}% "
            f"→ adjusted to {adjusted}% (possible value)"
        )

    adjusted = max(0, min(95, adjusted))
    return {"confidence": adjusted, "warning": warning}
