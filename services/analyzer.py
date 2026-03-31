"""
SportyBot Analyzer
Scores and ranks matches based on configurable betting strategies.

Each scoring function returns a confidence value from 0-100 along with
human-readable reasoning.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Optional

from data.data_collector import (
    get_head_to_head,
    get_standings,
    get_team_form,
    get_team_stats,
)


# ── Over / Under scoring ────────────────────────────────────────────────────

def score_match_for_over(
    fixture: dict,
    threshold: float = 1.5,
    league_avg_goals: float = 2.6,
) -> dict:
    """
    Score a fixture for Over <threshold> goals.

    Considers:
      - Both teams' avg goals scored (home/away specific)
      - Both teams' avg goals conceded
      - Head-to-head scoring history
      - League average goals

    Returns:
        {
          "confidence": int (0-100),
          "expected_goals": float,
          "reasons": list[str],
          "selection": "Over {threshold}",
          "odds": float | None,
        }
    """
    home = fixture["home"]
    away = fixture["away"]
    league_id = fixture["league"]["id"]
    season = fixture["league"].get("season")

    reasons: list[str] = []
    signals: list[float] = []  # each signal is 0.0 – 1.0

    # ── Team form (goals scored / conceded recently) ─────────────────────
    home_form = get_team_form(home["id"], last_n=5)
    away_form = get_team_form(away["id"], last_n=5)

    home_avg_scored = home_form["stats"]["avg_goals_scored"]
    home_avg_conceded = home_form["stats"]["avg_goals_conceded"]
    away_avg_scored = away_form["stats"]["avg_goals_scored"]
    away_avg_conceded = away_form["stats"]["avg_goals_conceded"]

    # Estimate expected goals from recent form
    expected_home = (home_avg_scored + away_avg_conceded) / 2
    expected_away = (away_avg_scored + home_avg_conceded) / 2
    expected_total = expected_home + expected_away

    form_signal = _goals_probability(expected_total, threshold)
    signals.append(form_signal)
    reasons.append(
        f"Form: {home['name']} avg {home_avg_scored} GS, "
        f"{away['name']} avg {away_avg_scored} GS"
    )

    # ── Detailed team stats (home/away specific) ─────────────────────────
    try:
        home_stats = get_team_stats(home["id"], league_id, season)
        away_stats = get_team_stats(away["id"], league_id, season)

        home_home_gf = home_stats["goals"]["for"]["home"]
        away_away_gf = away_stats["goals"]["for"]["away"]
        home_home_ga = home_stats["goals"]["against"]["home"]
        away_away_ga = away_stats["goals"]["against"]["away"]

        stat_expected = (home_home_gf + away_away_gf + home_home_ga + away_away_ga) / 2
        stat_signal = _goals_probability(stat_expected, threshold)
        signals.append(stat_signal)
        reasons.append(
            f"Season: {home['name']} home avg {home_home_gf} GF, "
            f"{away['name']} away avg {away_away_gf} GF"
        )
    except Exception:
        # Stats unavailable — skip this signal
        pass

    # ── Head-to-head ─────────────────────────────────────────────────────
    try:
        h2h = get_head_to_head(home["id"], away["id"], last_n=5)
        h2h_avg = h2h["stats"]["avg_total_goals"]
        h2h_signal = _goals_probability(h2h_avg, threshold)
        signals.append(h2h_signal)
        h2h_over_count = sum(
            1 for m in h2h["matches"]
            if _parse_score_total(m.get("score", "0-0")) > threshold
        )
        reasons.append(
            f"H2H: avg {h2h_avg} goals/game, "
            f"{h2h_over_count}/{h2h['stats']['total']} over {threshold}"
        )
    except Exception:
        pass

    # ── League average baseline ──────────────────────────────────────────
    league_signal = _goals_probability(league_avg_goals, threshold)
    signals.append(league_signal * 0.5)  # less weight for generic league stat

    # ── Combine signals ──────────────────────────────────────────────────
    if not signals:
        confidence = 50
    else:
        confidence = int(round(sum(signals) / len(signals) * 100))
    confidence = max(0, min(100, confidence))

    return {
        "fixture": fixture,
        "confidence": confidence,
        "expected_goals": round(expected_total, 2),
        "reasons": reasons,
        "selection": f"Over {threshold}",
        "odds": None,  # will be populated if odds data available
    }


# ── Win scoring ──────────────────────────────────────────────────────────────

def score_match_for_win(fixture: dict, team_id: int) -> dict:
    """
    Score a fixture for a specific team to win outright.

    Considers:
      - Team's recent form (last 5)
      - Home/away advantage
      - Current league position gap
      - H2H record
      - Opponent's recent form

    Returns:
        {
          "confidence": int (0-100),
          "reasons": list[str],
          "selection": "Home Win" | "Away Win",
          "odds": float | None,
        }
    """
    home = fixture["home"]
    away = fixture["away"]
    league_id = fixture["league"]["id"]
    season = fixture["league"].get("season")

    is_home = team_id == home["id"]
    team_name = home["name"] if is_home else away["name"]
    opp_name = away["name"] if is_home else home["name"]
    opp_id = away["id"] if is_home else home["id"]

    reasons: list[str] = []
    signals: list[float] = []  # each 0.0 – 1.0

    # ── Recent form ──────────────────────────────────────────────────────
    team_form = get_team_form(team_id, last_n=5)
    opp_form = get_team_form(opp_id, last_n=5)

    team_wins = team_form["stats"]["wins"]
    team_played = team_form["stats"]["played"]
    opp_wins = opp_form["stats"]["wins"]
    opp_played = opp_form["stats"]["played"]

    form_signal = (team_wins / max(team_played, 1)) * 0.7 + (
        1 - opp_wins / max(opp_played, 1)
    ) * 0.3
    signals.append(form_signal)

    team_form_str = team_form["form"]
    opp_form_str = opp_form["form"]
    reasons.append(f"Form: {team_name} {team_form_str}, {opp_name} {opp_form_str}")

    # ── Win/loss streak bonus ────────────────────────────────────────────
    streak = _count_streak(team_form_str, "W")
    if streak >= 3:
        signals.append(0.8)
        reasons.append(f"{team_name} on {streak}-game win streak")
    opp_loss_streak = _count_streak(opp_form_str, "L")
    if opp_loss_streak >= 3:
        signals.append(0.75)
        reasons.append(f"{opp_name} on {opp_loss_streak}-game losing streak")

    # ── Home/away advantage ──────────────────────────────────────────────
    if is_home:
        signals.append(0.6)  # home advantage baseline
        reasons.append(f"{team_name} playing at home")
    else:
        signals.append(0.4)
        reasons.append(f"{team_name} playing away")

    # ── League position gap ──────────────────────────────────────────────
    try:
        standings = get_standings(league_id, season)
        team_rank = _find_rank(standings, team_id)
        opp_rank = _find_rank(standings, opp_id)
        if team_rank and opp_rank:
            gap = opp_rank - team_rank  # positive = team is higher
            # Normalize: +19 gap → ~0.9, 0 gap → 0.5, -19 → ~0.1
            position_signal = 0.5 + (gap / 40)
            position_signal = max(0.1, min(0.9, position_signal))
            signals.append(position_signal)
            reasons.append(
                f"League position: {team_name} #{team_rank} vs {opp_name} #{opp_rank}"
            )
    except Exception:
        pass

    # ── Head-to-head ─────────────────────────────────────────────────────
    try:
        h2h = get_head_to_head(team_id, opp_id, last_n=5)
        t1_wins = h2h["stats"]["team1_wins"]
        total = h2h["stats"]["total"]
        if total > 0:
            h2h_signal = t1_wins / total
            signals.append(h2h_signal)
            reasons.append(
                f"H2H: {team_name} won {t1_wins}/{total} recent meetings"
            )
    except Exception:
        pass

    # ── Combine ──────────────────────────────────────────────────────────
    if not signals:
        confidence = 50
    else:
        confidence = int(round(sum(signals) / len(signals) * 100))
    confidence = max(0, min(100, confidence))

    return {
        "fixture": fixture,
        "confidence": confidence,
        "reasons": reasons,
        "selection": "Home Win" if is_home else "Away Win",
        "team_id": team_id,
        "team_name": team_name,
        "odds": None,
    }


# ── Combo builder ────────────────────────────────────────────────────────────

def find_best_combo(
    fixtures: list[dict],
    strategy: dict,
) -> dict:
    """
    Build an optimal bet slip from analyzed fixtures.

    Strategy dict example:
        {
            "target_odds": 10.0,
            "tolerance": 1.0,
            "wins": {"count": 2, "min_confidence": 70, "max_combined_odds": 2.5},
            "overs": {"threshold": 1.5, "min_confidence": 75},
        }

    Steps:
      1. Score every fixture for home/away win; keep top-N by confidence.
      2. Score remaining fixtures for Over threshold; rank by confidence.
      3. Greedily add overs until total odds are within target +/- tolerance.

    Returns:
        {
          "win_picks": [...],
          "over_picks": [...],
          "total_odds": float,
          "avg_confidence": float,
        }
    """
    win_cfg = strategy.get("wins", {})
    over_cfg = strategy.get("overs", {})
    target = strategy["target_odds"]
    tolerance = strategy.get("tolerance", 1.0)
    win_count = win_cfg.get("count", 2)
    min_win_conf = win_cfg.get("min_confidence", 70)
    min_over_conf = over_cfg.get("min_confidence", 75)
    over_threshold = over_cfg.get("threshold", 1.5)

    # ── Step 1: Score all fixtures for win ────────────────────────────────
    win_candidates: list[dict] = []
    for f in fixtures:
        # Score home team win
        home_score = score_match_for_win(f, f["home"]["id"])
        if home_score["confidence"] >= min_win_conf:
            # Assign a placeholder odds if not available
            if home_score["odds"] is None:
                home_score["odds"] = _estimate_odds_from_confidence(
                    home_score["confidence"]
                )
            win_candidates.append(home_score)

        # Score away team win (only if favourite away)
        away_score = score_match_for_win(f, f["away"]["id"])
        if away_score["confidence"] >= min_win_conf:
            if away_score["odds"] is None:
                away_score["odds"] = _estimate_odds_from_confidence(
                    away_score["confidence"]
                )
            win_candidates.append(away_score)

    # Sort by confidence descending
    win_candidates.sort(key=lambda x: x["confidence"], reverse=True)

    # Take top N wins, respecting max_combined_odds
    max_win_odds = win_cfg.get("max_combined_odds", 3.0)
    win_picks: list[dict] = []
    win_odds_product = 1.0
    used_fixture_ids: set[int] = set()

    for cand in win_candidates:
        if len(win_picks) >= win_count:
            break
        fid = cand["fixture"]["id"]
        if fid in used_fixture_ids:
            continue
        new_product = win_odds_product * cand["odds"]
        if new_product > max_win_odds:
            continue
        win_picks.append(cand)
        win_odds_product = new_product
        used_fixture_ids.add(fid)

    # ── Step 2: Score remaining fixtures for overs ────────────────────────
    over_candidates: list[dict] = []
    for f in fixtures:
        if f["id"] in used_fixture_ids:
            continue
        over_score = score_match_for_over(f, threshold=over_threshold)
        if over_score["confidence"] >= min_over_conf:
            if over_score["odds"] is None:
                over_score["odds"] = _estimate_over_odds(over_threshold)
            over_candidates.append(over_score)

    over_candidates.sort(key=lambda x: x["confidence"], reverse=True)

    # ── Step 3: Greedily add overs to approach target odds ────────────────
    over_picks: list[dict] = []
    current_odds = win_odds_product

    for cand in over_candidates:
        if current_odds >= target - tolerance:
            break
        over_picks.append(cand)
        current_odds *= cand["odds"]
        used_fixture_ids.add(cand["fixture"]["id"])

    # ── Compile result ────────────────────────────────────────────────────
    all_picks = win_picks + over_picks
    total_odds = 1.0
    total_conf = 0
    for p in all_picks:
        total_odds *= p.get("odds", 1.0)
        total_conf += p["confidence"]

    avg_confidence = int(round(total_conf / max(len(all_picks), 1)))

    return {
        "win_picks": win_picks,
        "over_picks": over_picks,
        "all_picks": all_picks,
        "total_odds": round(total_odds, 2),
        "avg_confidence": avg_confidence,
        "target_odds": target,
        "strategy": strategy,
    }


# ── Internal helpers ─────────────────────────────────────────────────────────

def _goals_probability(expected: float, threshold: float) -> float:
    """
    Rough probability that total goals exceed threshold,
    using a simplified Poisson-ish estimate.

    Returns a value between 0 and 1.
    """
    if expected <= 0:
        return 0.0
    # P(X > threshold) ≈ 1 - CDF_poisson(floor(threshold), expected)
    k = int(math.floor(threshold))
    cumulative = 0.0
    for i in range(k + 1):
        cumulative += (expected ** i) * math.exp(-expected) / math.factorial(i)
    return 1.0 - cumulative


def _parse_score_total(score: str) -> float:
    """Parse '2-1' into total goals (3)."""
    try:
        parts = score.split("-")
        return int(parts[0].strip()) + int(parts[1].strip())
    except (IndexError, ValueError):
        return 0


def _count_streak(form: str, char: str) -> int:
    """Count consecutive occurrences of char from the end of form string."""
    count = 0
    for c in reversed(form):
        if c == char:
            count += 1
        else:
            break
    return count


def _find_rank(standings: list[dict], team_id: int) -> Optional[int]:
    """Find a team's rank in the standings list."""
    for entry in standings:
        if entry["team_id"] == team_id:
            return entry["rank"]
    return None


def _estimate_odds_from_confidence(confidence: int) -> float:
    """
    Map confidence (0-100) to approximate decimal odds.
    High confidence → low odds (strong favourite).
    """
    if confidence >= 90:
        return 1.15
    elif confidence >= 85:
        return 1.25
    elif confidence >= 80:
        return 1.35
    elif confidence >= 75:
        return 1.45
    elif confidence >= 70:
        return 1.55
    elif confidence >= 65:
        return 1.70
    elif confidence >= 60:
        return 1.90
    else:
        return 2.20


def _estimate_over_odds(threshold: float) -> float:
    """Approximate odds for an Over bet based on threshold."""
    if threshold <= 0.5:
        return 1.08
    elif threshold <= 1.5:
        return 1.28
    elif threshold <= 2.5:
        return 1.75
    elif threshold <= 3.5:
        return 2.30
    else:
        return 3.00


# ── Booking code deep analysis ──────────────────────────────────────────────

def analyze_booking_pick(pick: dict) -> dict:
    """
    Deep-analyze a single pick from a SportyBet booking code using historical data.

    Takes a parsed pick dict (from parse_outcomes in telegram_bot) and returns
    an enriched version with:
      - data_confidence: int (0-100) based on actual stats
      - verdict: "strong" / "moderate" / "weak" / "avoid"
      - reasons: list of data-backed explanations
      - suggestion: alternative market if the pick looks bad
      - home_form / away_form data

    Uses team name fuzzy matching to find API-Football team IDs.
    """
    import requests

    home_name = pick.get("home", "?")
    away_name = pick.get("away", "?")
    market = pick.get("market", "?")
    pick_desc = pick.get("pick", "?")
    odds = pick.get("odds", 1.0)
    match_status = pick.get("match_status", "?")

    result = {
        **pick,
        "data_confidence": None,
        "verdict": "unknown",
        "analysis_reasons": [],
        "suggestion": None,
        "home_form_data": None,
        "away_form_data": None,
        "h2h_data": None,
    }

    # Skip already-ended matches
    if match_status == "Ended":
        result["verdict"] = "ended"
        result["analysis_reasons"] = ["Match already played"]
        result["data_confidence"] = pick.get("confidence", 50)
        return result

    # Find team IDs via API-Football search
    home_id = _search_team_id(home_name)
    away_id = _search_team_id(away_name)

    if not home_id or not away_id:
        result["verdict"] = "no_data"
        result["analysis_reasons"] = [
            f"Could not find team data for {'home' if not home_id else 'away'} team"
        ]
        result["data_confidence"] = pick.get("confidence", 50)
        return result

    # Pull form data
    signals: list[float] = []
    reasons: list[str] = []

    try:
        home_form = get_team_form(home_id, last_n=5)
        result["home_form_data"] = home_form
        home_form_str = home_form["form"]
        home_stats = home_form["stats"]
        reasons.append(
            f"{home_name}: {home_form_str} "
            f"({home_stats['wins']}W {home_stats['draws']}D {home_stats['losses']}L, "
            f"{home_stats['avg_goals_scored']} GS/game)"
        )
    except Exception:
        home_form = None
        reasons.append(f"{home_name}: form data unavailable")

    try:
        away_form = get_team_form(away_id, last_n=5)
        result["away_form_data"] = away_form
        away_form_str = away_form["form"]
        away_stats = away_form["stats"]
        reasons.append(
            f"{away_name}: {away_form_str} "
            f"({away_stats['wins']}W {away_stats['draws']}D {away_stats['losses']}L, "
            f"{away_stats['avg_goals_scored']} GS/game)"
        )
    except Exception:
        away_form = None
        reasons.append(f"{away_name}: form data unavailable")

    # H2H
    h2h_data = None
    try:
        h2h_data = get_head_to_head(home_id, away_id, last_n=5)
        result["h2h_data"] = h2h_data
        h2h_stats = h2h_data["stats"]
        if h2h_stats["total"] > 0:
            reasons.append(
                f"H2H ({h2h_stats['total']} games): "
                f"{home_name} {h2h_stats['team1_wins']}W, "
                f"Draws {h2h_stats['draws']}, "
                f"{away_name} {h2h_stats['team2_wins']}W, "
                f"Avg {h2h_stats['avg_total_goals']} goals/game"
            )
    except Exception:
        pass

    # Now score the SPECIFIC pick type
    if "1X2" in market or "1x2" in market.lower():
        # This is a match result pick (Home/Draw/Away)
        confidence = _analyze_match_result_pick(
            pick_desc, home_name, away_name,
            home_form, away_form, h2h_data, signals, reasons
        )
    elif "over" in pick_desc.lower() or "under" in pick_desc.lower():
        confidence = _analyze_over_under_pick(
            pick_desc, home_name, away_name,
            home_form, away_form, h2h_data, signals, reasons
        )
    elif "gg" in market.lower() or "btts" in market.lower() or "GG" in market:
        confidence = _analyze_btts_pick(
            pick_desc, home_form, away_form, h2h_data, signals, reasons
        )
    else:
        # Generic — use odds-based confidence
        confidence = pick.get("confidence", 50)
        reasons.append(f"Market '{market}' — using odds-based rating")

    # Determine verdict
    if confidence >= 75:
        verdict = "strong"
    elif confidence >= 60:
        verdict = "moderate"
    elif confidence >= 45:
        verdict = "weak"
    else:
        verdict = "avoid"

    # Generate suggestion if pick looks bad
    suggestion = None
    if verdict in ("weak", "avoid") and home_form and away_form:
        suggestion = _suggest_alternative(
            home_name, away_name, home_form, away_form, h2h_data, market, pick_desc
        )

    result["data_confidence"] = confidence
    result["verdict"] = verdict
    result["analysis_reasons"] = reasons
    result["suggestion"] = suggestion

    return result


def _analyze_match_result_pick(
    pick_desc, home_name, away_name,
    home_form, away_form, h2h_data, signals, reasons
):
    """Analyze a 1X2 (match result) pick."""
    confidence_points = []

    if "Home" in pick_desc or "1" == pick_desc.strip():
        # Home win picked
        if home_form:
            hs = home_form["stats"]
            win_rate = hs["wins"] / max(hs["played"], 1)
            confidence_points.append(win_rate * 100)

            streak = _count_streak(home_form["form"], "W")
            if streak >= 3:
                confidence_points.append(85)
                reasons.append(f"{home_name} on {streak}-game winning streak")
            elif streak == 0 and home_form["form"] and home_form["form"][-1] == "L":
                confidence_points.append(30)
                reasons.append(f"{home_name} lost their last game")

        if away_form:
            aws = away_form["stats"]
            away_loss_rate = aws["losses"] / max(aws["played"], 1)
            confidence_points.append(away_loss_rate * 80)  # opponent losing = good

            away_losing_streak = _count_streak(away_form["form"], "L")
            if away_losing_streak >= 3:
                confidence_points.append(80)
                reasons.append(f"{away_name} on {away_losing_streak}-game losing streak")

        # Home advantage bonus
        confidence_points.append(60)

        if h2h_data and h2h_data["stats"]["total"] > 0:
            h2h_win_rate = h2h_data["stats"]["team1_wins"] / h2h_data["stats"]["total"]
            confidence_points.append(h2h_win_rate * 100)

    elif "Away" in pick_desc or "2" == pick_desc.strip():
        # Away win picked — harder to back
        if away_form:
            aws = away_form["stats"]
            win_rate = aws["wins"] / max(aws["played"], 1)
            confidence_points.append(win_rate * 100)

            streak = _count_streak(away_form["form"], "W")
            if streak >= 3:
                confidence_points.append(80)
                reasons.append(f"{away_name} on {streak}-game winning streak")

        if home_form:
            hs = home_form["stats"]
            home_loss_rate = hs["losses"] / max(hs["played"], 1)
            confidence_points.append(home_loss_rate * 70)

        # Away disadvantage penalty
        confidence_points.append(40)

        if h2h_data and h2h_data["stats"]["total"] > 0:
            h2h_win_rate = h2h_data["stats"]["team2_wins"] / h2h_data["stats"]["total"]
            confidence_points.append(h2h_win_rate * 100)

    elif "Draw" in pick_desc or "X" == pick_desc.strip():
        # Draw picked
        if home_form:
            draw_rate = home_form["stats"]["draws"] / max(home_form["stats"]["played"], 1)
            confidence_points.append(draw_rate * 100)
        if away_form:
            draw_rate = away_form["stats"]["draws"] / max(away_form["stats"]["played"], 1)
            confidence_points.append(draw_rate * 100)
        if h2h_data and h2h_data["stats"]["total"] > 0:
            h2h_draw_rate = h2h_data["stats"]["draws"] / h2h_data["stats"]["total"]
            confidence_points.append(h2h_draw_rate * 100)

    if confidence_points:
        return int(round(sum(confidence_points) / len(confidence_points)))
    return 50


def _analyze_over_under_pick(
    pick_desc, home_name, away_name,
    home_form, away_form, h2h_data, signals, reasons
):
    """Analyze an Over/Under pick."""
    # Parse threshold from pick_desc like "Over (total=1.5)" or "Over (total=2.5)"
    threshold = 1.5  # default
    if "total=" in pick_desc:
        try:
            threshold = float(pick_desc.split("total=")[1].split(")")[0])
        except (ValueError, IndexError):
            pass
    elif "0.5" in pick_desc:
        threshold = 0.5
    elif "2.5" in pick_desc:
        threshold = 2.5
    elif "3.5" in pick_desc:
        threshold = 3.5

    is_over = "over" in pick_desc.lower()

    # Calculate expected goals
    expected_total = 2.5  # fallback
    if home_form and away_form:
        hs = home_form["stats"]
        aws = away_form["stats"]
        expected_home = (hs["avg_goals_scored"] + aws["avg_goals_conceded"]) / 2
        expected_away = (aws["avg_goals_scored"] + hs["avg_goals_conceded"]) / 2
        expected_total = expected_home + expected_away
        reasons.append(f"Expected goals: {expected_total:.1f} (threshold: {threshold})")

    # Poisson probability
    prob = _goals_probability(expected_total, threshold)
    if not is_over:
        prob = 1.0 - prob

    confidence = int(round(prob * 100))

    # H2H goals check
    if h2h_data and h2h_data["stats"]["total"] > 0:
        h2h_avg = h2h_data["stats"]["avg_total_goals"]
        if is_over:
            h2h_signal = 80 if h2h_avg > threshold else 35
        else:
            h2h_signal = 80 if h2h_avg < threshold else 35
        confidence = int((confidence + h2h_signal) / 2)
        reasons.append(f"H2H avg goals: {h2h_avg:.1f}")

    return max(0, min(100, confidence))


def _analyze_btts_pick(pick_desc, home_form, away_form, h2h_data, signals, reasons):
    """Analyze a Both Teams To Score pick."""
    confidence_points = []

    is_yes = "yes" in pick_desc.lower() or "gg" in pick_desc.lower()

    if home_form:
        hs = home_form["stats"]
        scored_rate = 1.0 if hs["avg_goals_scored"] > 0.5 else 0.3
        conceded_rate = 1.0 if hs["avg_goals_conceded"] > 0.5 else 0.3
        if is_yes:
            confidence_points.append((scored_rate * conceded_rate) * 80)
        else:
            confidence_points.append((1 - scored_rate * conceded_rate) * 80)

    if away_form:
        aws = away_form["stats"]
        scored_rate = 1.0 if aws["avg_goals_scored"] > 0.5 else 0.3
        conceded_rate = 1.0 if aws["avg_goals_conceded"] > 0.5 else 0.3
        if is_yes:
            confidence_points.append((scored_rate * conceded_rate) * 80)
        else:
            confidence_points.append((1 - scored_rate * conceded_rate) * 80)

    if confidence_points:
        return int(round(sum(confidence_points) / len(confidence_points)))
    return 50


def _suggest_alternative(
    home_name, away_name, home_form, away_form, h2h_data, current_market, current_pick
):
    """Suggest a safer alternative market for a weak pick."""
    hs = home_form["stats"]
    aws = away_form["stats"]

    combined_avg_goals = hs["avg_goals_scored"] + aws["avg_goals_scored"]
    combined_avg_conceded = hs["avg_goals_conceded"] + aws["avg_goals_conceded"]
    expected_total = (combined_avg_goals + combined_avg_conceded) / 2

    suggestions = []

    # If both teams score a lot → Over 1.5 is safe
    if expected_total > 2.0:
        over_prob = _goals_probability(expected_total, 1.5)
        if over_prob > 0.7:
            suggestions.append(("Over 1.5 Goals", int(over_prob * 100)))

    # If home team is strong → Home Win
    home_win_rate = hs["wins"] / max(hs["played"], 1)
    if home_win_rate >= 0.6:
        suggestions.append((f"{home_name} Win", int(home_win_rate * 85)))

    # If both teams concede → BTTS Yes
    if hs["avg_goals_conceded"] > 0.8 and aws["avg_goals_conceded"] > 0.8:
        suggestions.append(("Both Teams to Score - Yes", 70))

    # Over 0.5 goals is almost always safe
    if expected_total > 1.5:
        suggestions.append(("Over 0.5 Goals", 92))

    if not suggestions:
        return None

    # Return the highest confidence suggestion that's different from current pick
    suggestions.sort(key=lambda x: x[1], reverse=True)
    for name, conf in suggestions:
        if name.lower() not in current_pick.lower():
            return {"market": name, "confidence": conf}

    return None


# ── Team ID lookup cache ─────────────────────────────────────────────────────

_team_id_cache: dict[str, int | None] = {}


def _search_team_id(team_name: str) -> int | None:
    """
    Search for a team's API-Football ID by name.
    Uses cache to avoid repeated lookups. Falls back to partial matching.
    """
    if team_name in _team_id_cache:
        return _team_id_cache[team_name]

    from config import API_FOOTBALL_KEY, API_FOOTBALL_BASE, CACHE_DIR
    import json
    import hashlib
    import time

    if not API_FOOTBALL_KEY:
        return None

    # Check disk cache first
    cache_key = hashlib.sha256(f"team_search|{team_name}".encode()).hexdigest()
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            if time.time() - data.get("_ts", 0) < 86400 * 30:  # 30 day cache
                _team_id_cache[team_name] = data.get("team_id")
                return data.get("team_id")
        except (json.JSONDecodeError, KeyError):
            pass

    # Search API-Football
    import requests
    try:
        url = f"{API_FOOTBALL_BASE}/teams"
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        resp = requests.get(url, headers=headers, params={"search": team_name}, timeout=10)
        body = resp.json()

        from data_collector import _increment_budget

        _increment_budget()

        results = body.get("response", [])
        team_id = None
        if results:
            # Prefer exact name match, then first result
            for r in results:
                t = r.get("team", {})
                if t.get("name", "").lower() == team_name.lower():
                    team_id = t["id"]
                    break
            if not team_id:
                team_id = results[0].get("team", {}).get("id")

        # Cache to disk
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"_ts": time.time(), "team_id": team_id}))
        _team_id_cache[team_name] = team_id
        return team_id

    except Exception:
        _team_id_cache[team_name] = None
        return None
