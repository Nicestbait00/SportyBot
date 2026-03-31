"""
Analysis Service — fixture scoring pipeline.

No Telegram imports. Can be called from bot handlers, OpenClaw tools, or web API.
"""

from __future__ import annotations

import logging
from datetime import datetime

from core.scorer import score_match, cross_check_with_odds
from data.web_analyzer import _summarize_form

logger = logging.getLogger(__name__)


# ── Verdict helper ───────────────────────────────────────────────────────────

def verdict(conf: int) -> str:
    """Map a confidence score to a verdict label."""
    if conf >= 75:
        return "strong"
    elif conf >= 60:
        return "moderate"
    return "weak"


# ── Extended market pick generator ───────────────────────────────────────────

def add_extended_picks(
    all_scored: list,
    scores: dict,
    markets: dict,
    base_pick: dict,
    data_quality: str,
):
    """Generate picks for extended markets from scorer output + real SportyBet odds."""

    def _get_market_odds(market_key: str, outcome_id: str) -> float:
        mkt = markets.get(market_key, {})
        o = mkt.get("outcomes", {}).get(outcome_id, {})
        try:
            return float(o.get("odds", "0"))
        except (ValueError, TypeError):
            return 0.0

    def _add(market_name, pick_label, score_key, market_key, outcome_id, rating="moderate"):
        sc = scores.get(score_key)
        if not sc:
            return
        odds = _get_market_odds(market_key, outcome_id)
        if odds <= 1.0:
            return
        checked = cross_check_with_odds(sc["confidence"], odds)
        conf = checked["confidence"]
        reasons = sc["reasons"][:]
        if checked["warning"]:
            reasons.append(checked["warning"])
        if conf >= 40:
            all_scored.append({
                **base_pick,
                "market": market_name, "pick": pick_label,
                "odds": odds,
                "confidence": conf, "data_confidence": conf,
                "verdict": verdict(conf),
                "analysis_reasons": reasons,
                "suggestion": None, "data_quality": data_quality,
                "rating": rating,
            })

    # ── Draw (market 1, outcome 2) ──
    _add("1X2", "Draw", "draw", "1", "2")

    # ── Double Chance (market 10) ──
    _add("Double Chance", "1X", "double_chance_1x", "10", "9", "safe")
    _add("Double Chance", "X2", "double_chance_x2", "10", "11", "safe")
    _add("Double Chance", "12", "double_chance_12", "10", "10", "safe")

    # ── Draw No Bet (market 11) ──
    _add("Draw No Bet", "Home", "draw_no_bet_home", "11", "4", "safe")
    _add("Draw No Bet", "Away", "draw_no_bet_away", "11", "5")

    # ── Odd/Even (market 26) ──
    _add("Odd/Even", "Odd", "odd_goals", "26", "70")
    _add("Odd/Even", "Even", "even_goals", "26", "72")

    # ── Home Clean Sheet (market 31) ──
    _add("Home Clean Sheet", "Yes", "home_clean_sheet", "31", "74")

    # ── Away Clean Sheet (market 32) ──
    _add("Away Clean Sheet", "Yes", "away_clean_sheet", "32", "74")

    # ── HT 1X2 (market 60) ──
    _add("HT 1X2", "Home", "ht_home", "60", "1")
    _add("HT 1X2", "Draw", "ht_draw", "60", "2")
    _add("HT 1X2", "Away", "ht_away", "60", "3")

    # ── HT Over/Under (market 68) ──
    for threshold in [0.5, 1.5]:
        mkt_key = f"68|total={threshold}"
        _add("HT Over/Under", f"Over (total={threshold})", f"ht_over_{threshold}", mkt_key, "12")

    # ── HT GG/NG (market 75) ──
    _add("HT GG/NG", "Yes", "ht_btts", "75", "74")

    # ── 1X2 & GG/NG (market 35) ──
    _add("1X2 & GG/NG", "Home & GG", "home_and_gg", "35", "78")
    _add("1X2 & GG/NG", "Home & NG", "home_and_ng", "35", "80")
    _add("1X2 & GG/NG", "Away & GG", "away_and_gg", "35", "86")
    _add("1X2 & GG/NG", "Away & NG", "away_and_ng", "35", "88")

    # ── 1X2 & Over/Under (market 37) ──
    _add("1X2 & Over/Under", "Home & Over (total=2.5)", "home_and_over_2.5", "37|total=2.5", "796")
    _add("1X2 & Over/Under", "Away & Over (total=2.5)", "away_and_over_2.5", "37|total=2.5", "804")

    # ── Over/Under & GG/NG (market 36) ──
    _add("Over/Under & GG/NG", "Over 2.5 & GG", "over_2.5_and_gg", "36|total=2.5", "90")
    _add("Over/Under & GG/NG", "Under 2.5 & NG", "under_2.5_and_ng", "36|total=2.5", "96")

    # ── Home Over/Under (market 19) — team-specific over/under ──
    for threshold in [0.5, 1.5]:
        mkt_key = f"19|total={threshold}"
        _add("Home Over/Under", f"Over (total={threshold})", f"home_over_{threshold}", mkt_key, "12")

    # ── Away Over/Under (market 20) ──
    for threshold in [0.5, 1.5]:
        mkt_key = f"20|total={threshold}"
        _add("Away Over/Under", f"Over (total={threshold})", f"away_over_{threshold}", mkt_key, "12")

    # ── Conditional OR: Result OR Over/Under (markets 854-859) ──
    _add("Home Or Over", "Yes", "home_or_over_2.5", "854|total=2.5", "74")
    _add("Home Or Under", "Yes", "home_or_under_2.5", "855|total=2.5", "74")
    _add("Draw Or Over", "Yes", "draw_or_over_2.5", "856|total=2.5", "74")
    _add("Draw Or Under", "Yes", "draw_or_under_2.5", "857|total=2.5", "74")
    _add("Away Or Over", "Yes", "away_or_over_2.5", "858|total=2.5", "74")
    _add("Away Or Under", "Yes", "away_or_under_2.5", "859|total=2.5", "74")

    # ── Conditional OR: Result OR GG (markets 860-862) ──
    _add("Home Or GG", "Yes", "home_or_gg", "860", "74")
    _add("Draw Or GG", "Yes", "draw_or_gg", "861", "74")
    _add("Away Or GG", "Yes", "away_or_gg", "862", "74")


# ── Thin-data fallback picks ────────────────────────────────────────────────

def add_thin_data_safe_picks(
    all_scored: list[dict],
    markets: dict,
    base_pick: dict,
):
    """Add a small, controlled set of odds-based fallback picks when form data is thin."""

    def _get_market_odds(market_key: str, outcome_id: str) -> float:
        mkt = markets.get(market_key, {})
        o = mkt.get("outcomes", {}).get(outcome_id, {})
        try:
            return float(o.get("odds", "0"))
        except (ValueError, TypeError):
            return 0.0

    def _implied_confidence(odds: float, ceiling: int, floor: int = 50) -> int:
        if odds <= 1.0:
            return 0
        return int(max(floor, min(ceiling, (1 / odds) * 65)))

    def _add(market_name: str, pick_label: str, market_key: str, outcome_id: str, odds_cap: float, conf_cap: int):
        odds = _get_market_odds(market_key, outcome_id)
        if not (1.0 < odds <= odds_cap):
            return
        conf = _implied_confidence(odds, conf_cap)
        all_scored.append({
            **base_pick,
            "market": market_name,
            "pick": pick_label,
            "odds": odds,
            "confidence": conf,
            "data_confidence": conf,
            "verdict": "moderate" if conf >= 52 else "weak",
            "analysis_reasons": [f"Limited match data available — this pick is based on the strong market odds ({odds:.2f}) rather than form analysis"],
            "suggestion": None,
            "data_quality": "limited",
            "rating": "safe",
        })

    _add("Over/Under", "Over (total=0.5)", "18|total=0.5", "12", 1.30, 60)
    _add("Over/Under", "Over (total=1.5)", "18|total=1.5", "12", 1.55, 56)
    _add("Home Over/Under", "Over (total=0.5)", "19|total=0.5", "12", 1.45, 58)
    _add("Away Over/Under", "Over (total=0.5)", "20|total=0.5", "12", 1.45, 58)
    _add("Double Chance", "1X", "10", "9", 1.38, 56)
    _add("Double Chance", "X2", "10", "11", 1.38, 56)
    # Conditional OR — two ways to win, safe at low odds
    _add("Home Or Over", "Yes", "854|total=2.5", "74", 1.50, 58)
    _add("Away Or Over", "Yes", "858|total=2.5", "74", 1.85, 56)
    _add("Home Or GG", "Yes", "860", "74", 1.40, 58)
    _add("Draw Or Over", "Yes", "856|total=2.5", "74", 1.65, 56)
    _add("Away Or GG", "Yes", "862", "74", 1.95, 54)


# ── Per-fixture scoring ─────────────────────────────────────────────────────

def score_fixture(
    ev: dict,
    home_results: list[dict] | None,
    away_results: list[dict] | None,
) -> list[dict]:
    """Score a single SportyBet fixture. Returns list of scored pick dicts.

    Args:
        ev: SportyBet event dict with home, away, eventId, markets, etc.
        home_results: Team match history from football-data.org (or None).
        away_results: Team match history from football-data.org (or None).

    Returns:
        List of scored pick dicts ready for the qualified pool.
    """
    home_name = ev["home"]
    away_name = ev["away"]
    event_id = ev["eventId"]
    tournament = ev.get("tournament", "")
    kick_off = ev.get("estimateStartTime", 0)
    markets = ev.get("markets", {})

    real_1x2 = markets.get("1", {})
    real_gg = markets.get("29", {})

    # Convert kickoff timestamp to date string
    match_date = ""
    if kick_off:
        try:
            match_date = datetime.fromtimestamp(kick_off / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            match_date = ""

    base_pick = {
        "home": home_name,
        "away": away_name,
        "league": tournament,
        "date": match_date,
        "match_status": "Upcoming",
        "is_winning": None,
        "score": "",
        "event_id": event_id,
        "selection": {},
        "tournament": tournament,
        "_source": "sportybet",
        "_sporty_event": ev,
    }

    scored: list[dict] = []

    # Determine if we have form data
    home_form = _summarize_form(home_results) if home_results else None
    away_form = _summarize_form(away_results) if away_results else None

    if home_form and away_form:
        # ── Multi-factor scoring via scorer.py ──
        data_quality = "good"
        scores = score_match(home_form, away_form, home_results, away_results)

        def _get_odds_1x2(outcome_id: str) -> float:
            o = real_1x2.get("outcomes", {}).get(outcome_id, {})
            try:
                return float(o.get("odds", "0"))
            except (ValueError, TypeError):
                return 0.0

        def _get_over_odds(threshold: float) -> float:
            mkt = markets.get(f"18|total={threshold}", {})
            o12 = mkt.get("outcomes", {}).get("12", {})
            try:
                return float(o12.get("odds", "0"))
            except (ValueError, TypeError):
                return 0.0

        def _get_gg_odds() -> float:
            o = real_gg.get("outcomes", {}).get("74", {})
            try:
                return float(o.get("odds", "0"))
            except (ValueError, TypeError):
                return 0.0

        # ── Home Win ──
        hw = scores["home_win"]
        real_home_odds = _get_odds_1x2("1")
        if real_home_odds > 1.0:
            checked = cross_check_with_odds(hw["confidence"], real_home_odds)
            conf = checked["confidence"]
            reasons = hw["reasons"][:]
            if checked["warning"]:
                reasons.append(checked["warning"])
            if conf >= 45:
                scored.append({
                    **base_pick,
                    "market": "1X2", "pick": "Home",
                    "odds": real_home_odds,
                    "confidence": conf, "data_confidence": conf,
                    "verdict": verdict(conf),
                    "analysis_reasons": reasons,
                    "suggestion": None, "data_quality": data_quality,
                    "rating": "safe" if conf >= 70 else "moderate",
                })

        # ── Away Win ──
        aw = scores["away_win"]
        real_away_odds = _get_odds_1x2("3")
        if real_away_odds > 1.0:
            checked = cross_check_with_odds(aw["confidence"], real_away_odds)
            conf = checked["confidence"]
            reasons = aw["reasons"][:]
            if checked["warning"]:
                reasons.append(checked["warning"])
            if conf >= 45:
                scored.append({
                    **base_pick,
                    "market": "1X2", "pick": "Away",
                    "odds": real_away_odds,
                    "confidence": conf, "data_confidence": conf,
                    "verdict": verdict(conf),
                    "analysis_reasons": reasons,
                    "suggestion": None, "data_quality": data_quality,
                    "rating": "safe" if conf >= 70 else "moderate",
                })

        # ── Over 0.5 / 1.5 / 2.5 ──
        for threshold, key in [(0.5, "over_0.5"), (1.5, "over_1.5"), (2.5, "over_2.5")]:
            ov = scores[key]
            real_odds = _get_over_odds(threshold)
            if real_odds > 1.0:
                checked = cross_check_with_odds(ov["confidence"], real_odds)
                conf = checked["confidence"]
                reasons = ov["reasons"][:]
                if checked["warning"]:
                    reasons.append(checked["warning"])
                if conf >= 45:
                    scored.append({
                        **base_pick,
                        "market": "Over/Under",
                        "pick": f"Over (total={threshold})",
                        "odds": real_odds,
                        "confidence": conf, "data_confidence": conf,
                        "verdict": verdict(conf),
                        "analysis_reasons": reasons,
                        "suggestion": None, "data_quality": data_quality,
                        "rating": "safe" if threshold <= 1.5 else "moderate",
                    })

        # ── BTTS ──
        bt = scores["btts"]
        gg_odds = _get_gg_odds()
        if gg_odds > 1.0:
            checked = cross_check_with_odds(bt["confidence"], gg_odds)
            conf = checked["confidence"]
            reasons = bt["reasons"][:]
            if checked["warning"]:
                reasons.append(checked["warning"])
            if conf >= 45:
                scored.append({
                    **base_pick,
                    "market": "GG/NG", "pick": "GG",
                    "odds": gg_odds,
                    "confidence": conf, "data_confidence": conf,
                    "verdict": verdict(conf),
                    "analysis_reasons": reasons,
                    "suggestion": None, "data_quality": data_quality,
                    "rating": "moderate",
                })

        # ── Extended markets ──
        add_extended_picks(scored, scores, markets, base_pick, data_quality)

    else:
        # ── Odds-only analysis (no form data available) ──
        if real_1x2.get("outcomes"):
            add_thin_data_safe_picks(scored, markets, base_pick)

    return scored
