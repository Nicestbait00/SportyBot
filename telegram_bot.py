"""
SportyBot Telegram Bot
Provides /leagues, /pick, /refresh, /budget, /status, /check commands.
Interactive check flow: paste codes → review all games → pick target odds → get best combo.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from config import (
    AVAILABLE_MARKETS,
    DEFAULT_ENABLED_MARKETS,
    LEAGUE_CATEGORIES,
    LEAGUE_NAMES,
    LEAGUES,
    STRATEGY_PRESETS,
    load_user_config,
    save_user_config,
)
from data_collector import get_api_budget, get_fixtures, get_fixtures_lookahead, refresh_all
from analyzer import find_best_combo
from web_analyzer import analyze_pick as web_analyze_pick, get_team_results, _summarize_form, _poisson_over_prob
from scorer import score_match, cross_check_with_odds
import gemini_chat
from sportybet_events import (
    build_event_index, fetch_all_events, filter_events, find_event,
    build_booking_selection, create_booking_code,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Conversation states for /check flow
CHECK_CODES, CHECK_TARGET_ODDS, CHECK_EXCLUDE, CHECK_CONFIRM, CHECK_EXPAND, CHECK_REVIEW = range(6)
# Conversation states for /pick flow
PICK_TYPE, PICK_COUNT, PICK_ODDS, PICK_MODE, PICK_REVIEW = range(10, 15)
# Conversation states for /strategy custom flow
STRAT_CUSTOM_CONFIDENCE, STRAT_CUSTOM_MARKETS, STRAT_CUSTOM_OVER, STRAT_CUSTOM_MIN_ODDS = range(20, 24)


# ── Helpers ──────────────────────────────────────────────────────────────────

def format_pick(pick: dict, idx: int) -> str:
    """Format a single pick for Telegram display."""
    f = pick["fixture"]
    home = f["home"]["name"]
    away = f["away"]["name"]
    selection = pick["selection"]
    conf = pick["confidence"]
    odds = pick.get("odds", "?")
    reasons = pick.get("reasons", [])

    lines = [f"*{idx}. {home} vs {away}*"]
    lines.append(f"   Selection: `{selection}` | Odds: `{odds}` | Confidence: `{conf}%`")
    if reasons:
        lines.append(f"   _{reasons[0]}_")
    return "\n".join(lines)


def format_analysis(result: dict) -> str:
    """Format full analysis result for Telegram."""
    if not result.get("all_picks"):
        return "No picks found matching your strategy. Try /refresh first or adjust leagues."

    lines = ["🎯 *SportyBot Analysis*\n"]

    if result["win_picks"]:
        lines.append("*Straight Wins:*")
        for i, p in enumerate(result["win_picks"], 1):
            lines.append(format_pick(p, i))
        lines.append("")

    if result["over_picks"]:
        lines.append("*Over Picks:*")
        start = len(result["win_picks"]) + 1
        for i, p in enumerate(result["over_picks"], start):
            lines.append(format_pick(p, i))
        lines.append("")

    lines.append(f"*Total Odds:* `{result['total_odds']:.2f}`")
    lines.append(f"*Avg Confidence:* `{result['avg_confidence']}%`")
    lines.append(f"*Selections:* `{len(result['all_picks'])}`")
    lines.append(f"\n_Target was {result['target_odds']} odds_")

    return "\n".join(lines)


def fetch_booking_code(code: str) -> dict | None:
    """Fetch and parse a single SportyBet booking code. Returns parsed data or None."""
    import requests as req
    try:
        url = f"https://www.sportybet.com/api/ng/orders/share/{code}"
        resp = req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
    except Exception:
        return None

    if data.get("bizCode") != 10000 or not data.get("data"):
        return None

    return data["data"]


def parse_outcomes(data: dict) -> list[dict]:
    """Parse SportyBet share data into a flat list of picks with ratings."""
    outcomes = data.get("outcomes", [])
    selections = data.get("ticket", {}).get("selections", [])

    # Build selection lookup for specifiers
    sel_map = {s["eventId"]: s for s in selections}

    picks = []
    for outcome in outcomes:
        home = outcome.get("homeTeamName", "?")
        away = outcome.get("awayTeamName", "?")
        match_status = outcome.get("matchStatus", "?")
        tournament = (
            outcome.get("sport", {})
            .get("category", {})
            .get("tournament", {})
            .get("name", "?")
        )
        event_id = outcome.get("eventId", "")

        # Get market info
        markets = outcome.get("markets", [])
        market_desc = "?"
        pick_desc = "?"
        odds = 1.0
        is_winning = None

        if markets:
            m = markets[0]
            market_desc = m.get("desc", "?")
            if m.get("outcomes"):
                o = m["outcomes"][0]
                pick_desc = o.get("desc", "?")
                try:
                    odds = float(o.get("odds", "1.0"))
                except (ValueError, TypeError):
                    odds = 1.0
                is_winning = o.get("isWinning")

        # Add specifier for Over/Under
        sel = sel_map.get(event_id, {})
        specifier = sel.get("specifier", "")
        if specifier:
            pick_desc = f"{pick_desc} ({specifier})"

        # Rate the pick
        if match_status == "Ended":
            if is_winning == 1:
                rating = "won"
                confidence = 100
            elif is_winning == 0:
                rating = "lost"
                confidence = 0
            else:
                rating = "void"
                confidence = 50
        else:
            if odds < 1.25:
                rating = "very_safe"
                confidence = 90
            elif odds < 1.40:
                rating = "safe"
                confidence = 80
            elif odds < 1.60:
                rating = "moderate"
                confidence = 65
            elif odds < 1.80:
                rating = "medium"
                confidence = 55
            elif odds < 2.20:
                rating = "risky"
                confidence = 40
            else:
                rating = "very_risky"
                confidence = 25

        score_str = outcome.get("setScore", "")

        picks.append({
            "home": home,
            "away": away,
            "tournament": tournament,
            "market": market_desc,
            "pick": pick_desc,
            "odds": odds,
            "rating": rating,
            "confidence": confidence,
            "match_status": match_status,
            "is_winning": is_winning,
            "score": score_str,
            "event_id": event_id,
            "selection": sel,
        })

    return picks


def format_picks_review(all_picks: list[dict], codes: list[str]) -> str:
    """Format all picks from multiple codes into a review summary (basic, no deep analysis)."""
    lines = [f"Review: {len(all_picks)} games from {len(codes)} code(s)\n"]
    pending = [p for p in all_picks if p["match_status"] != "Ended"]
    ended = [p for p in all_picks if p["match_status"] == "Ended"]

    if ended:
        won = sum(1 for p in ended if p["rating"] == "won")
        lost = sum(1 for p in ended if p["rating"] == "lost")
        lines.append(f"Completed: {won} won, {lost} lost\n")

    for i, p in enumerate(pending, 1):
        lines.append(
            f"{i}. {p['home']} vs {p['away']}\n"
            f"   {p['market']}: {p['pick']} @ {p['odds']:.2f}"
        )
    return "\n".join(lines)


def format_deep_review(all_picks: list[dict], codes: list[str]) -> str:
    """Format deep-analyzed picks with verdicts, reasons, and suggestions."""
    verdict_icons = {
        "strong": "🟢",
        "moderate": "🟡",
        "weak": "🟠",
        "avoid": "🔴",
        "ended": "⏹",
        "won": "✅",
        "lost": "❌",
        "no_data": "❓",
        "error": "⚠️",
        "unknown": "❓",
    }

    lines = [f"📊 Deep Analysis — {len(all_picks)} games from {len(codes)} code(s)\n"]

    pending = [p for p in all_picks if p.get("verdict") not in ("ended", "won", "lost")]
    ended = [p for p in all_picks if p.get("verdict") in ("ended", "won", "lost")]

    # Show ended results first (brief)
    if ended:
        won = sum(1 for p in ended if p.get("is_winning") == 1)
        lost = sum(1 for p in ended if p.get("is_winning") == 0)
        lines.append(f"Completed: {won} won, {lost} lost, {len(ended) - won - lost} other\n")

    if pending:
        # Sort by data_confidence (strongest first)
        pending_sorted = sorted(
            pending,
            key=lambda p: p.get("data_confidence") or 0,
            reverse=True,
        )

        strong_count = sum(1 for p in pending if p.get("verdict") == "strong")
        moderate_count = sum(1 for p in pending if p.get("verdict") == "moderate")
        weak_count = sum(1 for p in pending if p.get("verdict") in ("weak", "avoid"))

        lines.append(f"Verdict: {strong_count} strong, {moderate_count} moderate, {weak_count} weak/avoid\n")

        data_quality_icons = {
            "good": "📊",
            "fair": "📉",
            "limited": "⚠️",
            "none": "❌",
            "unknown": "❓",
        }

        for i, p in enumerate(pending_sorted, 1):
            icon = verdict_icons.get(p.get("verdict", "unknown"), "?")
            conf = p.get("data_confidence", "?")
            verdict_label = (p.get("verdict") or "unknown").upper()
            dq = p.get("data_quality", "unknown")
            dq_icon = data_quality_icons.get(dq, "❓")

            source = "📌" if p.get("_source") == "booking_code" else "🔍" if p.get("_source") == "league_scan" else ""
            source_label = " [from code]" if p.get("_source") == "booking_code" else " [league pick]" if p.get("_source") == "league_scan" else ""
            lines.append(
                f"{icon} {i}. {p['home']} vs {p['away']}{source_label}\n"
                f"   {p['market']}: {p['pick']} @ {p['odds']:.2f}\n"
                f"   Verdict: {verdict_label} ({conf}%) | Data: {dq_icon} {dq}"
            )

            # Show key reasons (max 2)
            analysis_reasons = p.get("analysis_reasons", [])
            for reason in analysis_reasons[:2]:
                lines.append(f"   > {reason}")

            # Show suggestion if pick is weak
            suggestion = p.get("suggestion")
            if suggestion:
                lines.append(
                    f"   💡 Suggestion: swap to {suggestion['market']} "
                    f"({suggestion['confidence']}% confidence)"
                )

            lines.append("")  # blank line between picks

        # Total odds of all pending
        total_odds = 1.0
        for p in pending:
            total_odds *= p["odds"]
        lines.append(f"Total pending odds: {total_odds:.2f}")

    return "\n".join(lines)


def build_best_combo(picks: list[dict], target_odds: float) -> dict:
    """
    From a pool of analyzed picks, select the safest combination
    that gets closest to the target odds.

    Uses data_confidence (from deep analysis) when available,
    falls back to basic confidence. Excludes "avoid" verdict picks.
    """
    # Only use pending (unplayed) picks, exclude "avoid" verdict and odds-only picks
    pending = [
        p for p in picks
        if p["match_status"] != "Ended"
        and p.get("verdict") != "avoid"
        and p.get("data_quality") != "limited"
    ]

    if not pending:
        return {"selected": [], "total_odds": 1.0, "avg_confidence": 0}

    # Use data_confidence if available, otherwise basic confidence
    def get_conf(p):
        return p.get("data_confidence") or p.get("confidence") or 50

    # Sort by data confidence descending (safest first)
    pending_sorted = sorted(pending, key=get_conf, reverse=True)

    selected = []
    current_odds = 1.0

    for p in pending_sorted:
        if current_odds >= target_odds:
            break
        selected.append(p)
        current_odds *= p["odds"]

    # If we overshot, try removing the riskiest (last) pick
    if len(selected) > 1 and current_odds > target_odds * 1.5:
        test_odds = 1.0
        for p in selected[:-1]:
            test_odds *= p["odds"]
        if abs(test_odds - target_odds) < abs(current_odds - target_odds):
            selected = selected[:-1]
            current_odds = test_odds

    avg_conf = int(sum(get_conf(p) for p in selected) / max(len(selected), 1))

    return {
        "selected": selected,
        "total_odds": current_odds,
        "avg_confidence": avg_conf,
    }


def format_combo_result(combo: dict, target: float) -> str:
    """Format the final recommended combo with analysis data."""
    selected = combo["selected"]
    if not selected:
        return "Could not build a combo from these picks. Not enough safe pending games."

    verdict_icons = {
        "strong": "🟢", "moderate": "🟡", "weak": "🟠",
        "avoid": "🔴", "no_data": "❓", "unknown": "❓",
    }

    lines = [f"🎯 Recommended combo for ~{target:.0f} odds\n"]

    for i, p in enumerate(selected, 1):
        verdict = p.get("verdict", "unknown")
        icon = verdict_icons.get(verdict, "?")
        conf = p.get("data_confidence") or p.get("confidence", "?")

        lines.append(
            f"{icon} {i}. {p['home']} vs {p['away']}\n"
            f"    {p['market']}: {p['pick']} @ {p['odds']:.2f} "
            f"[{verdict.upper()} — {conf}%]"
        )

        # Show top reason
        reasons = p.get("analysis_reasons", [])
        if reasons:
            lines.append(f"    > {reasons[0]}")

    lines.append(f"\nTotal Odds: {combo['total_odds']:.2f}")
    lines.append(f"Selections: {len(selected)}")
    lines.append(f"Avg Confidence: {combo['avg_confidence']}%")

    # Dropped picks (if any were "avoid")
    lines.append("")
    if combo["total_odds"] < target * 0.8:
        lines.append(
            f"Under target — some risky picks were dropped. "
            f"Add more codes or lower your target."
        )
    elif abs(combo["total_odds"] - target) < 1:
        lines.append("Right on target!")
    elif combo["total_odds"] > target:
        lines.append(f"Slightly over ({combo['total_odds']:.1f} vs {target:.0f}).")

    return "\n".join(lines)


async def send_long_message(update_or_msg, text: str):
    """Send text, splitting if > 4000 chars."""
    msg_target = update_or_msg.message if hasattr(update_or_msg, "message") else update_or_msg
    if len(text) <= 4000:
        await msg_target.reply_text(text)
    else:
        # Split by lines, send in chunks
        lines = text.split("\n")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3900:
                await msg_target.reply_text(chunk)
                chunk = line
            else:
                chunk = chunk + "\n" + line if chunk else line
        if chunk:
            await msg_target.reply_text(chunk)


# ── Extended market pick generator ────────────────────────────────────────────

def _add_extended_picks(
    all_scored: list,
    scores: dict,
    markets: dict,
    base_pick: dict,
    data_quality: str,
    _verdict,
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
                "verdict": _verdict(conf),
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


# ── Config helpers ────────────────────────────────────────────────────────────

def _get_pick_config(user_config: dict) -> dict:
    """Return the effective pick configuration.

    New config system uses direct values (min_confidence, min_odds, enabled_markets).
    Falls back to legacy strategy presets for old saved configs.
    """
    # New-style config: direct values present
    if "min_confidence" in user_config and "enabled_markets" in user_config:
        return {
            "min_confidence": user_config.get("min_confidence", 75),
            "min_odds": user_config.get("min_odds", 1.05),
            "preferred_markets": user_config.get("enabled_markets", DEFAULT_ENABLED_MARKETS),
        }
    # Legacy: strategy preset or custom dict
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return strat
    return STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])


def _get_strategy_config(user_config: dict) -> dict:
    """Alias for backward compatibility — delegates to _get_pick_config."""
    return _get_pick_config(user_config)


def _get_config_label(user_config: dict) -> str:
    """Human-readable label for the user's current config."""
    if "min_confidence" in user_config and "enabled_markets" in user_config:
        markets = user_config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
        return (
            f"Min {user_config['min_confidence']}% conf, "
            f"≥{user_config.get('min_odds', 1.05)} odds, "
            f"{len(markets)} markets"
        )
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return strat.get("label", "Custom")
    preset = STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])
    return preset["label"]


def _get_strategy_label(user_config: dict) -> str:
    """Alias for backward compatibility."""
    return _get_config_label(user_config)


def _build_league_keyboard(active: set[int]) -> InlineKeyboardMarkup:
    """Build the league picker keyboard with category shortcuts."""
    buttons = []

    category_row = []
    for key, category in LEAGUE_CATEGORIES.items():
        category_ids = set(category["league_ids"])
        enabled = bool(category_ids) and category_ids.issubset(active)
        category_row.append(
            InlineKeyboardButton(
                f"{'✅' if enabled else '⬜'} {category['label']}",
                callback_data=f"league_cat_{key}",
            )
        )
    if category_row:
        buttons.append(category_row)

    for category in LEAGUE_CATEGORIES.values():
        row = []
        for lid in category["league_ids"]:
            name = LEAGUE_NAMES.get(lid, str(lid))
            check = "✅" if lid in active else "⬜"
            row.append(
                InlineKeyboardButton(
                    f"{check} {name}",
                    callback_data=f"league_toggle_{lid}",
                )
            )
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    categorized = {
        lid
        for category in LEAGUE_CATEGORIES.values()
        for lid in category["league_ids"]
    }
    other_leagues = sorted(
        (
            (lid, name)
            for lid, name in LEAGUE_NAMES.items()
            if lid not in categorized
        ),
        key=lambda item: item[1],
    )

    row = []
    for lid, name in other_leagues:
        check = "✅" if lid in active else "⬜"
        row.append(
            InlineKeyboardButton(
                f"{check} {name}",
                callback_data=f"league_toggle_{lid}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("🧹 Clear All", callback_data="league_clear_all"),
            InlineKeyboardButton("✅ Done", callback_data="league_done"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _format_league_selection_text(active: set[int]) -> str:
    """Render a short summary for the league picker."""
    preset_lines = []
    for category in LEAGUE_CATEGORIES.values():
        preset_lines.append(f"{category['label']}: {category['description']}")

    active_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in sorted(active)]
    active_summary = ", ".join(active_names) if active_names else "None selected yet"

    return (
        "Select leagues to analyze.\n"
        "Quick presets:\n"
        + "\n".join(preset_lines)
        + f"\n\nActive: {active_summary}\n"
        "Tap a category or individual leagues."
    )


def _clear_pick_runtime(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear transient /pick state."""
    for key in [
        "pick_all_scored",
        "pick_combo",
        "pick_target",
        "pick_excluded",
        "pick_shuffle_seed",
        "_pick_msg",
        "pick_market_slots",
        "pick_request",
        "pick_bundle",
        "pick_bundle_mode",
        "pick_bundle_fallback",
        "pick_bundle_review_mode",
        "pick_bundle_allow_reuse",
        "active_ticket_index",
        "pick_change_idx",
        "pick_change_alts",
    ]:
        context.user_data.pop(key, None)


def _default_pick_request() -> dict:
    """Default request model for the /pick flow."""
    return {
        "ticket_type": None,
        "ticket_count": None,
        "ticket_mode": None,
        "target_odds": None,
    }


def _ensure_pick_request(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Load and normalize the in-progress pick request."""
    req = context.user_data.get("pick_request")
    if not isinstance(req, dict):
        req = _default_pick_request()

    if req.get("ticket_type") not in ("single", "multiple"):
        req["ticket_type"] = None

    raw_ticket_count = req.get("ticket_count")
    ticket_count = None
    if raw_ticket_count not in (None, ""):
        try:
            ticket_count = int(raw_ticket_count)
        except (TypeError, ValueError):
            ticket_count = None
    if ticket_count is not None:
        ticket_count = max(1, min(5, ticket_count))
    req["ticket_count"] = ticket_count

    if req.get("ticket_mode") not in ("unique", "dynamic", "single"):
        req["ticket_mode"] = None

    target = req.get("target_odds")
    if target is not None:
        try:
            req["target_odds"] = float(target)
        except (TypeError, ValueError):
            req["target_odds"] = None

    if req["ticket_type"] == "single":
        req["ticket_count"] = 1
        req["ticket_mode"] = "single"
    elif req["ticket_type"] == "multiple" and req["ticket_count"] is not None:
        req["ticket_count"] = max(2, req["ticket_count"])

    context.user_data["pick_request"] = req
    return req


async def _send_or_edit(message, text: str, reply_markup=None, edit: bool = False, parse_mode=None):
    """Reply or edit a Telegram message depending on context."""
    if edit and hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def _prompt_pick_ticket_type(message, edit: bool = False):
    """Ask whether the user wants a single or multiple tickets."""
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Single Ticket", callback_data="pick_type_single"),
            InlineKeyboardButton("Multiple Tickets", callback_data="pick_type_multiple"),
        ]
    ])
    await _send_or_edit(
        message,
        "🎫 What do you want to build?\nChoose a single ticket or a multi-ticket bundle.",
        reply_markup=buttons,
        edit=edit,
    )
    return PICK_TYPE


async def _prompt_pick_ticket_count(message, edit: bool = False):
    """Ask how many tickets to generate."""
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("2", callback_data="pick_count_2"),
            InlineKeyboardButton("3", callback_data="pick_count_3"),
            InlineKeyboardButton("4", callback_data="pick_count_4"),
            InlineKeyboardButton("5", callback_data="pick_count_5"),
        ]
    ])
    await _send_or_edit(
        message,
        "🔢 How many tickets do you want?\nPick between 2 and 5.",
        reply_markup=buttons,
        edit=edit,
    )
    return PICK_COUNT


async def _prompt_pick_target_odds(message, edit: bool = False):
    """Ask for target odds per ticket."""
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("3 odds", callback_data="pick_target_3"),
            InlineKeyboardButton("5 odds", callback_data="pick_target_5"),
            InlineKeyboardButton("10 odds", callback_data="pick_target_10"),
        ],
        [
            InlineKeyboardButton("15 odds", callback_data="pick_target_15"),
            InlineKeyboardButton("20 odds", callback_data="pick_target_20"),
            InlineKeyboardButton("50 odds", callback_data="pick_target_50"),
        ],
    ])
    await _send_or_edit(
        message,
        "🎯 What odds are you targeting per ticket?\nPick below or type a custom number.",
        reply_markup=buttons,
        edit=edit,
    )
    return PICK_ODDS


async def _prompt_pick_ticket_mode(message, edit: bool = False):
    """Ask how a multi-ticket bundle should be generated."""
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Unique Tickets", callback_data="pick_mode_unique")],
        [InlineKeyboardButton("Dynamic Tickets", callback_data="pick_mode_dynamic")],
    ])
    await _send_or_edit(
        message,
        "🧠 How should the bundle work?\n"
        "Unique tickets = same games, different markets.\n"
        "Dynamic tickets = different games across tickets.",
        reply_markup=buttons,
        edit=edit,
    )
    return PICK_MODE


async def _continue_pick_request_flow(message, context, edit: bool = False):
    """Advance the guided /pick flow until all required fields are present."""
    req = _ensure_pick_request(context)
    ticket_count = req.get("ticket_count")

    if not req.get("ticket_type"):
        return await _prompt_pick_ticket_type(message, edit=edit)
    if req["ticket_type"] == "multiple" and (ticket_count is None or ticket_count < 2):
        return await _prompt_pick_ticket_count(message, edit=edit)
    if not req.get("target_odds"):
        return await _prompt_pick_target_odds(message, edit=edit)
    if req["ticket_type"] == "multiple" and req.get("ticket_mode") not in ("unique", "dynamic"):
        return await _prompt_pick_ticket_mode(message, edit=edit)

    context.user_data["pick_target"] = float(req["target_odds"])
    context.user_data["pick_bundle_mode"] = req.get("ticket_mode", "single")
    return await _pick_analyze_from_message(message, context, float(req["target_odds"]))


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and quick setup."""
    from config import TIMEFRAME_PRESETS
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config["leagues"]]
    tf_label = TIMEFRAME_PRESETS.get(config.get("timeframe", "7days"), {}).get("label", "7 days")

    msg = (
        "Hey! Welcome to SportyBot ⚽\n\n"
        "I crunch form data, odds, and stats to find the best betting combos — "
        "then book them straight to SportyBet.\n\n"
        "Just tell me what you need:\n"
        "  \"Give me 20 odds\"\n"
        "  \"3 wins, 5 over 1.5 for 30 odds\"\n"
        "  \"How's Arsenal doing?\"\n\n"
        "When I show you picks, you can:\n"
        "  \"remove 1, 3\" — drop games\n"
        "  \"change 5 to over 2.5\" — swap markets\n"
        "  \"why pick 3?\" — get the data breakdown\n"
        "  \"book it\" — generate SportyBet code\n\n"
        "Quick commands:\n"
        "/pick — Get picks  •  /check — Analyze a code\n"
        "/leagues — Toggle leagues  •  /strategy — Settings\n\n"
        f"📋 {', '.join(league_names) if league_names else 'All leagues'} | {tf_label}\n"
        f"⚙️ {_get_config_label(config)}"
    )
    await update.message.reply_text(msg)

    # Save chat ID
    chat_id_str = str(chat_id)
    if chat_id_str not in config.get("telegram_chat_ids", []):
        config.setdefault("telegram_chat_ids", []).append(chat_id_str)
        save_user_config(config, chat_id=chat_id)


async def cmd_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show inline keyboard to toggle leagues."""
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    config = load_user_config(chat_id=chat_id)
    active = set(config.get("leagues", []))

    await update.message.reply_text(
        _format_league_selection_text(active),
        reply_markup=_build_league_keyboard(active),
    )


async def callback_league_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league toggle button press."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data
    if data == "league_done":
        config = load_user_config(chat_id=chat_id)
        names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config["leagues"]]
        active = set(config.get("leagues", []))
        preset_hits = [
            category["label"]
            for category in LEAGUE_CATEGORIES.values()
            if set(category["league_ids"]).issubset(active)
        ]
        preset_line = f"Presets active: {', '.join(preset_hits)}\n" if preset_hits else ""
        await query.edit_message_text(
            preset_line +
            f"Leagues set: {', '.join(names) or 'None'}\n\n"
            "Now run /refresh to cache data, then /pick to get selections.",
        )
        return

    config = load_user_config(chat_id=chat_id)
    leagues = list(config.get("leagues", []))

    if data == "league_clear_all":
        leagues = []
    elif data.startswith("league_cat_"):
        category_key = data.replace("league_cat_", "")
        category = LEAGUE_CATEGORIES.get(category_key)
        if category:
            category_ids = category["league_ids"]
            if all(lid in leagues for lid in category_ids):
                leagues = [lid for lid in leagues if lid not in category_ids]
            else:
                leagues = list(dict.fromkeys(leagues + category_ids))
    else:
        lid = int(data.replace("league_toggle_", ""))
        if lid in leagues:
            leagues.remove(lid)
        else:
            leagues.append(lid)

    config["leagues"] = leagues
    save_user_config(config, chat_id=chat_id)

    active = set(leagues)
    await query.edit_message_text(
        _format_league_selection_text(active),
        reply_markup=_build_league_keyboard(active),
    )


async def pick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the guided /pick flow."""
    _clear_pick_runtime(context)
    context.user_data["chat_id"] = update.effective_chat.id
    args = context.args or []
    req = _ensure_pick_request(context)

    if args:
        try:
            target = float(args[0])
            req["ticket_type"] = "single"
            req["ticket_count"] = 1
            req["ticket_mode"] = "single"
            req["target_odds"] = target
            context.user_data["pick_request"] = req
            context.user_data["pick_target"] = target
            return await _continue_pick_request_flow(update.message, context)
        except ValueError:
            pass

    return await _continue_pick_request_flow(update.message, context)


async def pick_receive_type_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive ticket type as free text."""
    text = update.message.text.strip().lower()
    req = _ensure_pick_request(context)
    if "multi" in text:
        req["ticket_type"] = "multiple"
        req["ticket_count"] = None
        req["ticket_mode"] = None
    elif "single" in text or "one" in text:
        req["ticket_type"] = "single"
        req["ticket_count"] = 1
        req["ticket_mode"] = "single"
    else:
        await update.message.reply_text("Reply with `single` or `multiple`, or tap a button.", parse_mode="Markdown")
        return PICK_TYPE
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(update.message, context)


async def pick_receive_type_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive ticket type from button."""
    query = update.callback_query
    await query.answer()
    req = _ensure_pick_request(context)
    ticket_type = query.data.replace("pick_type_", "")
    req["ticket_type"] = ticket_type
    if ticket_type == "single":
        req["ticket_count"] = 1
        req["ticket_mode"] = "single"
    else:
        req["ticket_count"] = None
        req["ticket_mode"] = None
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(query.message, context, edit=True)


async def pick_receive_count_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive ticket count as free text."""
    try:
        count = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a number from 2 to 5, or tap a button.")
        return PICK_COUNT
    if count < 2 or count > 5:
        await update.message.reply_text("Ticket bundles are capped between 2 and 5.")
        return PICK_COUNT
    req = _ensure_pick_request(context)
    req["ticket_count"] = count
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(update.message, context)


async def pick_receive_count_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive ticket count from button."""
    query = update.callback_query
    await query.answer()
    req = _ensure_pick_request(context)
    req["ticket_count"] = int(query.data.replace("pick_count_", ""))
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(query.message, context, edit=True)


async def pick_receive_odds_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds as text."""
    try:
        target = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 10) or /cancel.")
        return PICK_ODDS
    req = _ensure_pick_request(context)
    req["target_odds"] = target
    context.user_data["pick_request"] = req
    context.user_data["pick_target"] = target
    return await _continue_pick_request_flow(update.message, context)


async def pick_receive_odds_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds from button."""
    query = update.callback_query
    await query.answer()
    target = float(query.data.replace("pick_target_", ""))
    req = _ensure_pick_request(context)
    req["target_odds"] = target
    context.user_data["pick_request"] = req
    context.user_data["pick_target"] = target
    return await _continue_pick_request_flow(query.message, context, edit=True)


async def pick_receive_mode_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive multi-ticket mode as free text."""
    text = update.message.text.strip().lower()
    req = _ensure_pick_request(context)
    if "unique" in text:
        req["ticket_mode"] = "unique"
    elif "dynamic" in text or "different" in text:
        req["ticket_mode"] = "dynamic"
    else:
        await update.message.reply_text("Reply with `unique` or `dynamic`, or tap a button.", parse_mode="Markdown")
        return PICK_MODE
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(update.message, context)


async def pick_receive_mode_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive multi-ticket mode from button."""
    query = update.callback_query
    await query.answer()
    req = _ensure_pick_request(context)
    req["ticket_mode"] = query.data.replace("pick_mode_", "")
    context.user_data["pick_request"] = req
    return await _continue_pick_request_flow(query.message, context, edit=True)


async def _pick_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, target: float):
    """Run analysis and show results (called from text input)."""
    return await _pick_analyze_from_message(update.message, context, target)


async def _pick_analyze_from_message(message, context, target: float):
    """Core pick analysis — fetches fixtures from SportyBet, scores with football-data.org form."""
    import random, json, hashlib
    from pathlib import Path

    chat_id = context.user_data.get("chat_id")
    config = load_user_config(chat_id=chat_id)
    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")

    # Check analysis cache first (valid for 3 hours)
    cache_key_raw = f"pick_sportybet|{date_str}|{sorted(leagues)}|{timeframe}"
    cache_key = hashlib.sha256(cache_key_raw.encode()).hexdigest()[:16]
    cache_dir = Path(__file__).resolve().parent / ".cache" / "analysis"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{cache_key}.json"

    cached_scored = None
    if cache_path.exists():
        try:
            import time as _time
            cdata = json.loads(cache_path.read_text())
            if _time.time() - cdata.get("_ts", 0) < 10800:  # 3 hour cache
                cached_scored = cdata.get("picks", [])
                logger.info(f"Using cached analysis: {len(cached_scored)} picks")
        except Exception:
            pass

    if cached_scored:
        await message.reply_text(
            f"⚡ Using cached analysis ({len(cached_scored)} scored picks).\n"
            f"Target: ~{target:.0f} odds",
        )
        context.user_data["pick_all_scored"] = cached_scored
        context.user_data["pick_excluded"] = set()
        return await _pick_build_combo(message, context)

    # Resolve friendly names for display
    from config import LEAGUE_NAMES, TIMEFRAME_PRESETS
    league_display = ", ".join(LEAGUE_NAMES.get(lid, str(lid)) for lid in leagues) if leagues else "All leagues"
    timeframe_display = TIMEFRAME_PRESETS.get(timeframe, {}).get("label", timeframe)

    await message.reply_text(
        f"🔍 Fetching upcoming matches from SportyBet...\n"
        f"Leagues: {league_display}\n"
        f"Window: {timeframe_display}\n"
        f"Target: ~{target:.0f} odds\n"
        f"First run — caching results for instant reshuffles.",
    )

    # Step 1: Get fixtures directly from SportyBet (has event IDs + real odds)
    # Build tournament name filter from user's league config
    from config import LEAGUE_SPORTYBET_NAMES
    allowed_tournaments = None
    if leagues:
        allowed_tournaments = []
        for lid in leagues:
            allowed_tournaments.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    try:
        all_sporty_events = await asyncio.to_thread(fetch_all_events, max_pages=5, allowed_tournaments=allowed_tournaments or None)
    except Exception as e:
        logger.warning(f"SportyBet fetch failed: {e}")
        all_sporty_events = []

    if not all_sporty_events:
        await message.reply_text(
            "Could not fetch fixtures from SportyBet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=f"pick_target_{target}")],
            ]),
        )
        return PICK_ODDS

    # Filter by league + timeframe
    sporty_events = filter_events(all_sporty_events, league_ids=leagues, timeframe=timeframe)

    if not sporty_events:
        await message.reply_text(
            f"No matches found for {league_display} in {timeframe_display}.\n"
            f"Try /timeframe to change window or /leagues to add leagues."
        )
        return ConversationHandler.END

    total_matches = len(sporty_events)
    progress_msg = await message.reply_text(
        f"Found {total_matches} matches ({len(all_sporty_events)} total on SportyBet).\n"
        f"Analyzing form data... 0/{total_matches}"
    )

    # Step 2: Score every fixture using football-data.org form data
    all_scored = []
    analyzed_count = 0
    _last_progress = 0

    for ev in sporty_events:
        home_name = ev["home"]
        away_name = ev["away"]
        event_id = ev["eventId"]
        tournament = ev.get("tournament", "")
        kick_off = ev.get("estimateStartTime", 0)
        markets = ev.get("markets", {})

        # Get real odds from SportyBet markets
        # 1X2 is keyed as "1", GG/NG as "29"
        # Over/Under uses compound keys: "18|total=0.5", "18|total=1.5", "18|total=2.5" etc.
        real_1x2 = markets.get("1", {})
        real_gg = markets.get("29", {})

        try:
            home_results = await asyncio.to_thread(get_team_results, home_name, count=10)
            away_results = await asyncio.to_thread(get_team_results, away_name, count=10)

            if not home_results or not away_results:
                # Still include with odds-only analysis if we have markets
                if not real_1x2.get("outcomes"):
                    continue
                # Use odds-based estimation when no form data
                home_form = None
                away_form = None
            else:
                home_form = _summarize_form(home_results)
                away_form = _summarize_form(away_results)

            analyzed_count += 1

            # Update progress every 5 matches
            if analyzed_count - _last_progress >= 5:
                _last_progress = analyzed_count
                try:
                    await progress_msg.edit_text(
                        f"Found {total_matches} matches.\n"
                        f"Analyzing form data... {analyzed_count}/{total_matches}"
                    )
                except Exception:
                    pass  # Telegram rate limit, ignore

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
                "_sporty_event": ev,  # Keep full event for booking
            }

            if home_form and away_form:
                # ── Multi-factor scoring via scorer.py ──
                data_quality = "good"
                scores = score_match(home_form, away_form, home_results, away_results)

                # Helper to get real odds from SportyBet
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

                def _verdict(conf: int) -> str:
                    if conf >= 75:
                        return "strong"
                    elif conf >= 60:
                        return "moderate"
                    return "weak"

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
                        all_scored.append({
                            **base_pick,
                            "market": "1X2", "pick": "Home",
                            "odds": real_home_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
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
                        all_scored.append({
                            **base_pick,
                            "market": "1X2", "pick": "Away",
                            "odds": real_away_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
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
                            all_scored.append({
                                **base_pick,
                                "market": "Over/Under",
                                "pick": f"Over (total={threshold})",
                                "odds": real_odds,
                                "confidence": conf, "data_confidence": conf,
                                "verdict": _verdict(conf),
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
                        all_scored.append({
                            **base_pick,
                            "market": "GG/NG", "pick": "GG",
                            "odds": gg_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
                            "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": data_quality,
                            "rating": "moderate",
                        })

                # ── Extended markets ──
                _add_extended_picks(
                    all_scored, scores, markets, base_pick,
                    data_quality, _verdict,
                )

            else:
                # ── Odds-only analysis (no form data available) ──
                # Cap confidence low — odds are market prices, not analysis
                home_outcome = real_1x2.get("outcomes", {}).get("1", {})
                try:
                    real_home_odds = float(home_outcome.get("odds", "0"))
                except (ValueError, TypeError):
                    real_home_odds = 0

                if 1.01 < real_home_odds < 1.50:
                    # Cap at 40% — no form data means low confidence regardless of odds
                    implied_conf = int(min(40, (1 / real_home_odds) * 50))
                    all_scored.append({
                        **base_pick,
                        "market": "1X2", "pick": "Home",
                        "odds": real_home_odds,
                        "confidence": implied_conf,
                        "data_confidence": implied_conf,
                        "verdict": "weak",
                        "analysis_reasons": [f"⚠ No form data — odds-only estimate: {real_home_odds:.2f}"],
                        "suggestion": None, "data_quality": "limited",
                        "rating": "weak",
                    })

        except Exception as e:
            logger.warning(f"Error analyzing {home_name} vs {away_name}: {e}")

    if not all_scored:
        await message.reply_text(
            "Could not analyze any fixtures.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Retry", callback_data=f"pick_target_{target}")],
            ]),
        )
        return PICK_ODDS

    try:
        await progress_msg.edit_text(
            f"✅ Analyzed {analyzed_count} matches — {len(all_scored)} picks scored.\n"
            f"Building best combo for ~{target:.0f} odds..."
        )
    except Exception:
        await message.reply_text(
            f"✅ Analyzed {analyzed_count} matches — {len(all_scored)} picks scored.\n"
            f"Building best combo for ~{target:.0f} odds..."
        )

    # Cache the analysis for 3 hours (strip _sporty_event for JSON serialization)
    try:
        import time as _time
        cache_picks = []
        for p in all_scored:
            cp = {k: v for k, v in p.items() if k != "_sporty_event"}
            cache_picks.append(cp)
        cache_path.write_text(json.dumps({"_ts": _time.time(), "picks": cache_picks}))
        logger.info(f"Cached {len(cache_picks)} scored picks to {cache_path.name}")
    except Exception as e:
        logger.warning(f"Failed to cache analysis: {e}")

    # Store full pool and build first combo
    context.user_data["pick_all_scored"] = all_scored
    context.user_data["pick_excluded"] = set()

    return await _pick_build_combo(message, context)


def _confidence_verdict(conf: int) -> str:
    """Map confidence score to verdict label."""
    if conf >= 75:
        return "strong"
    elif conf >= 60:
        return "moderate"
    return "weak"


def _pick_key(p: dict) -> str:
    """Stable content-based key for a pick (immune to list reordering)."""
    return f"{p.get('home','')}|{p.get('away','')}|{p.get('market','')}|{p.get('pick','')}"


def _pick_threshold(p: dict) -> float | None:
    """Extract the Over/Under threshold from a pick string like 'Over (total=1.5)'."""
    pick_str = p.get("pick", "")
    if "total=" in pick_str:
        try:
            return float(pick_str.split("total=")[1].rstrip(")"))
        except (ValueError, IndexError):
            pass
    return None


def _is_safe_team_total_pick(p: dict) -> bool:
    """Return True for low-line team total markets we want to surface more often."""
    market = p.get("market", "")
    threshold = _pick_threshold(p)
    return market in {"Home Over/Under", "Away Over/Under"} and threshold in (0.5, 1.5)


def _pick_selection_score(p: dict) -> float:
    """Score a pick for combo ordering, with a bias toward safer goal markets."""
    conf = float(p.get("data_confidence", p.get("confidence", 50)) or 50)
    odds = float(p.get("odds", 1.0) or 1.0)
    market = p.get("market", "")
    threshold = _pick_threshold(p)

    bonus = 0.0
    if market in {"Home Over/Under", "Away Over/Under"}:
        if threshold == 0.5:
            bonus += 10.0
        elif threshold == 1.5:
            bonus += 6.0
    elif market == "Over/Under":
        if threshold == 0.5:
            bonus += 4.0
        elif threshold == 1.5:
            bonus += 2.0
    elif market == "Double Chance":
        bonus += 3.0
    elif market == "Draw No Bet":
        bonus += 2.0

    odds_penalty = max(0.0, odds - 1.8) * 6.0
    return conf + bonus - odds_penalty


def _pick_qualifies_for_combo(p: dict, min_confidence: int, min_pick_odds: float) -> bool:
    """Apply combo thresholds, with a small allowance for very safe team totals."""
    odds = float(p.get("odds", 0) or 0)
    if p.get("data_quality") == "limited" or odds < min_pick_odds:
        return False

    conf = int(p.get("data_confidence", p.get("confidence", 50)) or 50)
    if conf >= min_confidence:
        return True

    if not _is_safe_team_total_pick(p):
        return False

    threshold = _pick_threshold(p)
    if threshold == 0.5 and odds <= 1.55:
        return conf >= max(55, min_confidence - 7)
    if threshold == 1.5 and odds <= 1.80:
        return conf >= max(60, min_confidence - 5)
    return False


def _match_key(p: dict) -> str:
    """Stable match identifier for scored picks."""
    return f"{p.get('home', '')}_{p.get('away', '')}"


def _ticket_totals(picks: list[dict]) -> tuple[float, int]:
    """Return total odds and average confidence for a ticket."""
    total_odds = 1.0
    total_conf = 0
    for pick in picks:
        total_odds *= float(pick.get("odds", 1.0) or 1.0)
        total_conf += int(pick.get("data_confidence", pick.get("confidence", 50)) or 50)
    avg_conf = int(total_conf / max(len(picks), 1))
    return round(total_odds, 2), avg_conf


def _make_ticket_entry(
    ticket_id: int,
    mode: str,
    target_odds: float,
    picks: list[dict],
    fallback_notes: list[str] | None = None,
    reused_fixtures: bool = False,
    excluded_pick_keys: set[str] | None = None,
) -> dict:
    """Build the runtime ticket object stored in pick_bundle."""
    total_odds, avg_conf = _ticket_totals(picks)
    notes = list(fallback_notes or [])
    return {
        "id": ticket_id,
        "mode": mode,
        "target_odds": target_odds,
        "picks": [dict(p) for p in picks],
        "total_odds": total_odds,
        "avg_confidence": avg_conf,
        "fallback_applied": bool(notes),
        "fallback_notes": notes,
        "reused_fixtures": reused_fixtures,
        "_excluded_pick_keys": set(excluded_pick_keys or set()),
    }


def _build_fallback_profiles(pick_cfg: dict) -> list[dict]:
    """Build ordered soft-fallback profiles for bundle generation."""
    base_conf = int(pick_cfg.get("min_confidence", 75))
    base_odds = float(pick_cfg.get("min_odds", 1.05))
    base_markets = list(dict.fromkeys(pick_cfg.get("preferred_markets", DEFAULT_ENABLED_MARKETS)))
    conf_floor = max(55, base_conf - 15)
    safe_market_order = ["Home Over/Under", "Away Over/Under", "Double Chance", "Draw No Bet", "Over/Under"]

    profiles = []
    seen = set()

    def add_profile(min_conf: int, min_odds: float, markets: list[str], notes: list[str]):
        key = (min_conf, round(min_odds, 2), tuple(markets))
        if key in seen:
            return
        seen.add(key)
        profiles.append({
            "min_confidence": min_conf,
            "min_odds": round(min_odds, 2),
            "preferred_markets": list(markets),
            "notes": list(notes),
        })

    add_profile(base_conf, base_odds, base_markets, [])

    current_conf = base_conf
    while current_conf > conf_floor:
        current_conf = max(conf_floor, current_conf - 5)
        add_profile(
            current_conf,
            base_odds,
            base_markets,
            [f"Min confidence lowered to {current_conf}%"],
        )

    current_odds = base_odds
    current_conf = max(conf_floor, current_conf)
    while current_odds > 1.05 + 1e-9:
        current_odds = max(1.05, round(current_odds - 0.05, 2))
        add_profile(
            current_conf,
            current_odds,
            base_markets,
            [
                f"Min confidence lowered to {current_conf}%",
                f"Min odds lowered to {current_odds:.2f}",
            ],
        )

    expanded = list(base_markets)
    expansion_notes = [
        f"Min confidence lowered to {current_conf}%",
        f"Min odds lowered to {current_odds:.2f}",
    ]
    for market in safe_market_order:
        if market in expanded:
            continue
        expanded.append(market)
        add_profile(
            current_conf,
            current_odds,
            expanded,
            expansion_notes + [f"Added market: {market}"],
        )

    return profiles


def _build_qualified_pool(
    all_scored: list[dict],
    excluded: set[str],
    pick_cfg: dict,
    market_slots: list[dict] | None = None,
    shuffle_seed: int = 0,
) -> list[dict]:
    """Filter and sort the scored pool using an effective config."""
    available = [
        p for p in all_scored
        if _pick_key(p) not in excluded and p.get("data_quality") != "limited"
    ]

    preferred_markets = pick_cfg.get("preferred_markets", DEFAULT_ENABLED_MARKETS)
    min_confidence = pick_cfg.get("min_confidence", 75)
    min_pick_odds = pick_cfg.get("min_odds", 1.05)

    if not market_slots:
        available = [p for p in available if p.get("market", "") in preferred_markets]

    if shuffle_seed > 0:
        import random
        rng = random.Random(shuffle_seed)
        available.sort(
            key=lambda p: _pick_selection_score(p) + rng.uniform(-4.0, 4.0),
            reverse=True,
        )
    else:
        available.sort(key=_pick_selection_score, reverse=True)

    if market_slots:
        return [
            p for p in available
            if p.get("odds", 0) > 1.0 and p.get("data_quality") != "limited"
        ]

    return [
        p for p in available
        if _pick_qualifies_for_combo(p, min_confidence, min_pick_odds)
    ]


def _select_ticket_from_pool(
    qualified: list[dict],
    target: float,
    market_slots: list[dict] | None = None,
    disallowed_match_keys: set[str] | None = None,
) -> list[dict]:
    """Build one ticket from a qualified pool using the existing greedy selector."""
    disallowed_match_keys = disallowed_match_keys or set()

    selected = []
    used_matches = set()
    current_odds = 1.0

    if market_slots:
        for slot in market_slots:
            if slot.get("fill"):
                continue
            slot_market = slot["market"]
            slot_count = slot.get("count", 1)
            slot_threshold = slot.get("threshold")

            slot_picks = [
                p for p in qualified
                if p["market"] == slot_market
                and _match_key(p) not in used_matches
                and _match_key(p) not in disallowed_match_keys
            ]
            if slot_market == "Over/Under" and slot_threshold is not None:
                slot_picks = [p for p in slot_picks if _pick_threshold(p) == slot_threshold]

            slot_picks.sort(key=_pick_selection_score, reverse=True)
            for pick in slot_picks[:slot_count]:
                match_key = _match_key(pick)
                selected.append(pick)
                used_matches.add(match_key)
                current_odds *= pick["odds"]

        fill_slots = [slot for slot in market_slots if slot.get("fill")]
        if fill_slots and current_odds < target:
            fill_slot = fill_slots[0]
            fill_picks = [
                p for p in qualified
                if p["market"] == fill_slot["market"]
                and _match_key(p) not in used_matches
                and _match_key(p) not in disallowed_match_keys
            ]
            if fill_slot["market"] == "Over/Under" and fill_slot.get("threshold") is not None:
                fill_picks = [p for p in fill_picks if _pick_threshold(p) == fill_slot["threshold"]]
            fill_picks.sort(key=_pick_selection_score, reverse=True)
            for pick in fill_picks:
                if current_odds >= target:
                    break
                match_key = _match_key(pick)
                selected.append(pick)
                used_matches.add(match_key)
                current_odds *= pick["odds"]

    league_counts: dict[str, int] = {}
    max_per_league = max(3, len(qualified) // 5) if qualified else 3
    for pick in qualified:
        if current_odds >= target:
            break
        match_key = _match_key(pick)
        if match_key in used_matches or match_key in disallowed_match_keys:
            continue
        league = pick.get("league", "")
        if league_counts.get(league, 0) >= max_per_league:
            continue
        selected.append(pick)
        used_matches.add(match_key)
        current_odds *= pick["odds"]
        league_counts[league] = league_counts.get(league, 0) + 1

    return selected


def _select_unique_ticket_from_pool(
    qualified: list[dict],
    reference_picks: list[dict],
    target: float,
    forbidden_pick_keys: set[str],
) -> list[dict]:
    """Build a unique ticket on the same fixtures using different markets."""
    forced_match_keys = [_match_key(p) for p in reference_picks]
    ref_by_match = {_match_key(p): p for p in reference_picks}
    by_match: dict[str, list[dict]] = {}

    for pick in qualified:
        match_key = _match_key(pick)
        if match_key not in ref_by_match or _pick_key(pick) in forbidden_pick_keys:
            continue
        by_match.setdefault(match_key, []).append(pick)

    selected: list[dict] = []
    candidate_lists: dict[str, list[dict]] = {}
    for match_key in forced_match_keys:
        ref_pick = ref_by_match[match_key]
        candidates = sorted(
            by_match.get(match_key, []),
            key=lambda p: _pick_selection_score(p) - abs(float(p.get("odds", 1.0)) - float(ref_pick.get("odds", 1.0))) * 15,
            reverse=True,
        )
        if not candidates:
            return []
        selected.append(candidates[0])
        candidate_lists[match_key] = candidates

    current_odds, _ = _ticket_totals(selected)
    if current_odds >= target:
        return selected

    while current_odds < target:
        best_upgrade = None
        for idx, current_pick in enumerate(selected):
            match_key = _match_key(current_pick)
            candidates = candidate_lists.get(match_key, [])
            current_key = _pick_key(current_pick)
            try:
                current_idx = next(i for i, cand in enumerate(candidates) if _pick_key(cand) == current_key)
            except StopIteration:
                continue
            for alt in candidates[current_idx + 1:]:
                if float(alt.get("odds", 1.0)) <= float(current_pick.get("odds", 1.0)):
                    continue
                new_total = current_odds / max(float(current_pick.get("odds", 1.0)), 1.0) * float(alt.get("odds", 1.0))
                score_drop = _pick_selection_score(current_pick) - _pick_selection_score(alt)
                candidate_rank = (
                    0 if new_total >= target else 1,
                    abs(target - new_total),
                    score_drop,
                )
                if best_upgrade is None or candidate_rank < best_upgrade["rank"]:
                    best_upgrade = {"index": idx, "pick": alt, "new_total": new_total, "rank": candidate_rank}

        if best_upgrade is None:
            break

        selected[best_upgrade["index"]] = best_upgrade["pick"]
        current_odds = best_upgrade["new_total"]

    return selected


def _generate_dynamic_bundle(
    all_scored: list[dict],
    ticket_count: int,
    target: float,
    pick_cfg: dict,
    excluded: set[str],
    market_slots: list[dict] | None = None,
    shuffle_seed: int = 0,
) -> tuple[list[dict], dict | None, bool]:
    """Build a dynamic multi-ticket bundle with soft fallback."""
    best_bundle: list[dict] = []
    best_profile = None
    profiles = _build_fallback_profiles(pick_cfg)

    for profile in profiles:
        bundle = []
        used_match_keys: set[str] = set()
        qualified = _build_qualified_pool(all_scored, excluded, profile, market_slots, shuffle_seed)
        for ticket_id in range(1, ticket_count + 1):
            picks = _select_ticket_from_pool(qualified, target, market_slots, used_match_keys)
            if not picks:
                break
            bundle.append(_make_ticket_entry(ticket_id, "dynamic", target, picks, profile.get("notes", [])))
            used_match_keys.update({_match_key(p) for p in picks})
        if len(bundle) > len(best_bundle):
            best_bundle = bundle
            best_profile = profile
        if len(bundle) == ticket_count:
            return bundle, profile, False

    if not best_bundle or best_profile is None:
        return best_bundle, best_profile, False

    reuse_bundle = [
        _make_ticket_entry(
            ticket["id"],
            ticket["mode"],
            ticket["target_odds"],
            ticket["picks"],
            ticket["fallback_notes"],
            reused_fixtures=ticket.get("reused_fixtures", False),
            excluded_pick_keys=ticket.get("_excluded_pick_keys", set()),
        )
        for ticket in best_bundle
    ]
    qualified = _build_qualified_pool(all_scored, excluded, best_profile, market_slots, shuffle_seed)
    for ticket_id in range(len(reuse_bundle) + 1, ticket_count + 1):
        picks = _select_ticket_from_pool(qualified, target, market_slots, set())
        if not picks:
            break
        notes = list(best_profile.get("notes", [])) + ["Reused fixtures after pool exhaustion"]
        reuse_bundle.append(_make_ticket_entry(ticket_id, "dynamic", target, picks, notes, reused_fixtures=True))

    if len(reuse_bundle) > len(best_bundle):
        return reuse_bundle, best_profile, True
    return best_bundle, best_profile, False


def _generate_unique_bundle(
    all_scored: list[dict],
    ticket_count: int,
    target: float,
    pick_cfg: dict,
    excluded: set[str],
    shuffle_seed: int = 0,
) -> tuple[list[dict], dict | None]:
    """Build a unique-ticket bundle that keeps the same fixtures across tickets."""
    best_bundle: list[dict] = []
    best_profile = None
    profiles = _build_fallback_profiles(pick_cfg)

    for profile in profiles:
        qualified = _build_qualified_pool(all_scored, excluded, profile, None, shuffle_seed)
        base_picks = _select_ticket_from_pool(qualified, target, None, set())
        if not base_picks:
            continue

        bundle = [_make_ticket_entry(1, "unique", target, base_picks, profile.get("notes", []))]
        used_pick_keys = {_pick_key(p) for p in base_picks}

        for ticket_id in range(2, ticket_count + 1):
            picks = _select_unique_ticket_from_pool(qualified, base_picks, target, used_pick_keys)
            if not picks:
                break
            bundle.append(_make_ticket_entry(ticket_id, "unique", target, picks, profile.get("notes", [])))
            used_pick_keys.update({_pick_key(p) for p in picks})

        if len(bundle) > len(best_bundle):
            best_bundle = bundle
            best_profile = profile
        if len(bundle) == ticket_count:
            return bundle, profile

    return best_bundle, best_profile


def _generate_pick_bundle(context) -> tuple[list[dict], dict | None, bool]:
    """Generate a single or multi-ticket bundle from the scored pool."""
    req = _ensure_pick_request(context)
    all_scored = context.user_data.get("pick_all_scored", [])
    excluded = context.user_data.get("pick_excluded", set())
    shuffle_seed = context.user_data.get("pick_shuffle_seed", 0)
    chat_id = context.user_data.get("chat_id")
    config = load_user_config(chat_id=chat_id)
    pick_cfg = _get_pick_config(config)
    market_slots = context.user_data.get("pick_market_slots")
    target = float(req.get("target_odds") or context.user_data.get("pick_target", 10))

    if req.get("ticket_type") != "multiple":
        qualified = _build_qualified_pool(all_scored, excluded, pick_cfg, market_slots, shuffle_seed)
        picks = _select_ticket_from_pool(qualified, target, market_slots, set())
        if not picks:
            return [], None, False
        return [_make_ticket_entry(1, "single", target, picks, [])], pick_cfg, False

    ticket_count = int(req.get("ticket_count", 2))
    ticket_mode = req.get("ticket_mode", "dynamic")
    if ticket_mode == "unique":
        bundle, profile = _generate_unique_bundle(all_scored, ticket_count, target, pick_cfg, excluded, shuffle_seed)
        return bundle, profile, False

    bundle, profile, reused = _generate_dynamic_bundle(all_scored, ticket_count, target, pick_cfg, excluded, None, shuffle_seed)
    return bundle, profile, reused


def _build_ticket_review_text(ticket: dict, heading: str | None = None) -> str:
    """Format a single ticket for Telegram review."""
    picks = ticket.get("picks", [])
    lines = [heading or f"🎫 Ticket {ticket.get('id', 1)}"]
    lines.append("")

    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠"}
    for idx, pick in enumerate(picks, 1):
        icon = verdict_icons.get(pick.get("verdict", ""), "❓")
        market_label = pick.get("pick", "").replace("(total=", "").replace(")", "")
        conf = pick.get("data_confidence", pick.get("confidence", "?"))
        lines.append(f"{icon} {idx}. {pick.get('home')} vs {pick.get('away')}")
        lines.append(f"   {pick.get('market')}: {market_label} @ ~{pick.get('odds', 0):.2f} [{conf}%]")
        reasons = pick.get("analysis_reasons", [])
        if reasons:
            lines.append(f"   > {reasons[0]}")
        lines.append("")

    lines.append(f"Total Odds: ~{ticket.get('total_odds', 1.0):.2f}")
    lines.append(f"Selections: {len(picks)}")
    lines.append(f"Avg Confidence: {ticket.get('avg_confidence', 0)}%")
    if ticket.get("fallback_notes"):
        lines.append(f"Fallback: {'; '.join(ticket['fallback_notes'])}")
    if ticket.get("reused_fixtures"):
        lines.append("Reuse note: fixtures were reused after pool exhaustion")
    return "\n".join(lines)


async def _show_pick_bundle_summary(message, context, edit: bool = False):
    """Show the multi-ticket bundle summary with high-level actions."""
    bundle = context.user_data.get("pick_bundle", [])
    req = _ensure_pick_request(context)
    if not bundle:
        await _send_or_edit(message, "No tickets available right now.", edit=edit)
        return ConversationHandler.END

    lines = [
        f"🎫 Bundle ready: {len(bundle)} ticket(s)",
        f"Mode: {req.get('ticket_mode', 'dynamic').title()}",
        f"Target per ticket: ~{float(req.get('target_odds') or 0):.0f} odds",
        "",
    ]

    for ticket in bundle:
        status_bits = [f"~{ticket['total_odds']:.2f} odds", f"{ticket['avg_confidence']}% avg"]
        if ticket.get("fallback_notes"):
            status_bits.append("fallback used")
        if ticket.get("reused_fixtures"):
            status_bits.append("fixtures reused")
        lines.append(f"Ticket {ticket['id']}: " + " | ".join(status_bits))

    buttons = [
        [InlineKeyboardButton(f"✏️ Edit Ticket {ticket['id']}", callback_data=f"pick_bundle_edit_{idx}")]
        for idx, ticket in enumerate(bundle)
    ]
    buttons.append([
        InlineKeyboardButton("🔄 Reshuffle Bundle", callback_data="pick_bundle_reshuffle"),
        InlineKeyboardButton("✅ Book All", callback_data="pick_bundle_book"),
    ])

    await _send_or_edit(
        message,
        "\n".join(lines) + "\n\nChoose a ticket to edit or book the whole bundle.",
        reply_markup=InlineKeyboardMarkup(buttons),
        edit=edit,
    )
    return PICK_REVIEW


async def _show_pick_bundle_ticket_detail(message, context, edit: bool = False):
    """Show the currently active ticket inside a bundle."""
    bundle = context.user_data.get("pick_bundle", [])
    idx = context.user_data.get("active_ticket_index", 0)
    if idx < 0 or idx >= len(bundle):
        return await _show_pick_bundle_summary(message, context, edit=edit)

    ticket = bundle[idx]
    combo = ticket.get("picks", [])
    context.user_data["pick_combo"] = combo
    context.user_data["pick_excluded"] = set(ticket.get("_excluded_pick_keys", set()))

    buttons = []
    for pick_idx, _pick in enumerate(combo):
        buttons.append([
            InlineKeyboardButton(f"❌ {pick_idx + 1}", callback_data=f"pick_exclude_{pick_idx}"),
            InlineKeyboardButton(f"🔄 {pick_idx + 1}", callback_data=f"pick_change_{pick_idx}"),
        ])
    buttons.append([
        InlineKeyboardButton("📖 Why these?", callback_data="pick_explain_all"),
        InlineKeyboardButton("🔀 Reshuffle Ticket", callback_data="pick_reshuffle"),
    ])
    buttons.append([
        InlineKeyboardButton("⬅️ Back to Bundle", callback_data="pick_bundle_back"),
        InlineKeyboardButton("✅ Book All", callback_data="pick_bundle_book"),
    ])

    await _send_or_edit(
        message,
        _build_ticket_review_text(ticket, heading=f"🎫 Ticket {ticket['id']} of {len(bundle)}"),
        reply_markup=InlineKeyboardMarkup(buttons),
        edit=edit,
    )
    return PICK_REVIEW


def _rebuild_bundle_ticket(context) -> dict | None:
    """Rebuild the active ticket in a bundle using the stored bundle profile."""
    bundle = context.user_data.get("pick_bundle", [])
    idx = context.user_data.get("active_ticket_index", 0)
    if idx < 0 or idx >= len(bundle):
        return None

    req = _ensure_pick_request(context)
    profile = context.user_data.get("pick_bundle_fallback") or _get_pick_config(load_user_config(chat_id=context.user_data.get("chat_id")))
    all_scored = context.user_data.get("pick_all_scored", [])
    excluded = set(context.user_data.get("pick_excluded", set()))
    qualified = _build_qualified_pool(all_scored, excluded, profile, None, context.user_data.get("pick_shuffle_seed", 0))
    target = float(req.get("target_odds") or context.user_data.get("pick_target", 10))
    current_ticket = bundle[idx]

    if req.get("ticket_mode") == "unique":
        reference_picks = bundle[0].get("picks", [])
        forbidden = set(excluded)
        for other_idx, ticket in enumerate(bundle):
            if other_idx == idx:
                continue
            forbidden.update({_pick_key(p) for p in ticket.get("picks", [])})
        picks = _select_unique_ticket_from_pool(qualified, reference_picks, target, forbidden)
    else:
        other_match_keys = set()
        for other_idx, ticket in enumerate(bundle):
            if other_idx == idx:
                continue
            other_match_keys.update({_match_key(p) for p in ticket.get("picks", [])})
        picks = _select_ticket_from_pool(qualified, target, None, other_match_keys)
        if not picks and current_ticket.get("reused_fixtures"):
            picks = _select_ticket_from_pool(qualified, target, None, set())

    if not picks:
        return None

    rebuilt = _make_ticket_entry(
        current_ticket["id"],
        current_ticket.get("mode", req.get("ticket_mode", "dynamic")),
        target,
        picks,
        current_ticket.get("fallback_notes", []),
        reused_fixtures=current_ticket.get("reused_fixtures", False),
        excluded_pick_keys=excluded,
    )
    bundle[idx] = rebuilt
    context.user_data["pick_bundle"] = bundle
    context.user_data["pick_combo"] = rebuilt["picks"]
    return rebuilt


def _sync_active_bundle_ticket_from_combo(context) -> dict | None:
    """Persist the current pick_combo back into the active bundle ticket."""
    bundle = context.user_data.get("pick_bundle", [])
    idx = context.user_data.get("active_ticket_index", 0)
    if idx < 0 or idx >= len(bundle):
        return None

    current = bundle[idx]
    picks = [dict(p) for p in context.user_data.get("pick_combo", [])]
    rebuilt = _make_ticket_entry(
        current["id"],
        current.get("mode", "dynamic"),
        current.get("target_odds", context.user_data.get("pick_target", 10)),
        picks,
        current.get("fallback_notes", []),
        reused_fixtures=current.get("reused_fixtures", False),
        excluded_pick_keys=set(context.user_data.get("pick_excluded", set())),
    )
    bundle[idx] = rebuilt
    context.user_data["pick_bundle"] = bundle
    return rebuilt


async def _show_single_ticket_combo(message, context, ticket: dict, edit: bool = False):
    """Render the standard single-ticket review UI."""
    selected = ticket.get("picks", [])
    target = ticket.get("target_odds", context.user_data.get("pick_target", 10))
    if not selected:
        await _send_or_edit(
            message,
            "Couldn't find enough strong picks for that target right now.\n"
            "Try widening the window with /timeframe or adding more leagues with /leagues.",
            edit=edit,
        )
        return ConversationHandler.END

    chat_id = context.user_data.get("chat_id")
    config = load_user_config(chat_id=chat_id)
    from config import LEAGUE_NAMES as _LN, TIMEFRAME_PRESETS as _TP

    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")
    league_str = ", ".join(_LN.get(lid, str(lid)) for lid in leagues) if leagues else "All"
    timeframe_str = _TP.get(timeframe, {}).get("label", timeframe)

    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠"}
    lines = [f"🎯 Picks for ~{target:.0f} odds | {league_str} | {timeframe_str}", ""]

    for idx, pick in enumerate(selected, 1):
        icon = verdict_icons.get(pick.get("verdict", ""), "❓")
        market_label = pick.get("pick", "").replace("(total=", "").replace(")", "")
        conf = pick.get("data_confidence", pick.get("confidence", 0))
        lines.append(f"{icon} {idx}. {pick.get('home')} vs {pick.get('away')}")
        lines.append(f"   [{pick.get('league', '')}] {pick.get('market')}: {market_label} @ ~{pick.get('odds', 0):.2f} [{conf}%]")
        reasons = pick.get("analysis_reasons", [])
        if reasons:
            lines.append(f"   > {reasons[0]}")
        lines.append("")

    lines.append(f"Total Odds: ~{ticket.get('total_odds', 1.0):.2f}")
    lines.append(f"Selections: {len(selected)}")
    lines.append(f"Avg Confidence: {ticket.get('avg_confidence', 0)}%")

    buttons = []
    for idx, _pick in enumerate(selected):
        buttons.append([
            InlineKeyboardButton(f"❌ {idx + 1}", callback_data=f"pick_exclude_{idx}"),
            InlineKeyboardButton(f"🔄 {idx + 1}", callback_data=f"pick_change_{idx}"),
        ])
    buttons.append([
        InlineKeyboardButton("📖 Why these?", callback_data="pick_explain_all"),
        InlineKeyboardButton("🔄 Reshuffle", callback_data="pick_reshuffle"),
    ])
    buttons.append([
        InlineKeyboardButton("✅ Lock it in", callback_data="pick_confirm"),
    ])

    await _send_or_edit(
        message,
        "\n".join(lines) + "\n\nEdit, explain, reshuffle, or lock it in:",
        reply_markup=InlineKeyboardMarkup(buttons),
        edit=edit,
    )
    return PICK_REVIEW


async def _pick_build_combo(message, context):
    """Build and render either a single ticket or a multi-ticket bundle."""
    if context.user_data.get("_check_mode"):
        chat_id = context.user_data.get("chat_id")
        config = load_user_config(chat_id=chat_id)
        pick_cfg = _get_pick_config(config)
        qualified = _build_qualified_pool(
            context.user_data.get("pick_all_scored", []),
            context.user_data.get("pick_excluded", set()),
            pick_cfg,
            None,
            context.user_data.get("pick_shuffle_seed", 0),
        )
        picks = _select_ticket_from_pool(
            qualified,
            float(context.user_data.get("pick_target", 10)),
            None,
            set(),
        )
        if not picks:
            await _send_or_edit(message, "Couldn't build a combo from these picks.", edit=False)
            return ConversationHandler.END
        ticket = _make_ticket_entry(1, "single", float(context.user_data.get("pick_target", 10)), picks, [])
        context.user_data["pick_combo"] = ticket["picks"]
        return await _show_single_ticket_combo(message, context, ticket, edit=False)

    req = _ensure_pick_request(context)
    if req.get("ticket_type") == "multiple":
        if context.user_data.get("pick_bundle_review_mode") == "ticket" and context.user_data.get("pick_bundle"):
            rebuilt = _rebuild_bundle_ticket(context)
            if rebuilt is None:
                await _send_or_edit(
                    message,
                    "Couldn't rebuild that ticket with the current constraints. Try reshuffling the bundle instead.",
                    edit=False,
                )
                return PICK_REVIEW
            return await _show_pick_bundle_ticket_detail(message, context, edit=False)

        bundle, profile, reused = _generate_pick_bundle(context)
        if not bundle:
            await _send_or_edit(
                message,
                "Couldn't find enough valid tickets for that bundle right now.\n"
                "Try fewer tickets, lower target odds, or a wider timeframe.",
                edit=False,
            )
            return ConversationHandler.END

        context.user_data["pick_bundle"] = bundle
        context.user_data["pick_bundle_fallback"] = profile
        context.user_data["pick_bundle_review_mode"] = "bundle"
        context.user_data["pick_bundle_mode"] = req.get("ticket_mode", "dynamic")
        context.user_data["pick_combo"] = bundle[0]["picks"]
        if reused:
            context.user_data["pick_bundle_allow_reuse"] = True
        return await _show_pick_bundle_summary(message, context, edit=False)

    bundle, profile, _ = _generate_pick_bundle(context)
    if not bundle:
        await _send_or_edit(
            message,
            "Couldn't find enough strong picks for that target right now.\n"
            "Try widening the window with /timeframe or adding more leagues with /leagues.",
            edit=False,
        )
        return ConversationHandler.END

    ticket = bundle[0]
    context.user_data["pick_bundle"] = bundle
    context.user_data["pick_bundle_fallback"] = profile
    context.user_data["pick_bundle_review_mode"] = "single"
    context.user_data["pick_combo"] = ticket["picks"]
    return await _show_single_ticket_combo(message, context, ticket, edit=False)


async def _pick_confirm_and_book(message, context):
    """Shared booking logic for a single ticket or full bundle."""
    bundle = context.user_data.get("pick_bundle", [])
    if bundle and not context.user_data.get("_check_mode"):
        await message.reply_text(f"📲 Generating SportyBet booking codes for {len(bundle)} ticket(s)...")
        result_lines = ["🎫 *Bundle Booking Codes*\n"]
        any_success = False
        for ticket in bundle:
            booking_result = await _book_ticket_picks(ticket.get("picks", []))
            result_lines.append(f"*Ticket {ticket['id']}* — ~{ticket['total_odds']:.2f} odds")
            if booking_result["code"]:
                any_success = True
                result_lines.append(f"Code: `{booking_result['code']}`")
                result_lines.append(f"Selections: {booking_result['selection_count']}")
            else:
                result_lines.append("Code: booking failed")
            if booking_result["failed"]:
                result_lines.append("Issues:")
                for item in booking_result["failed"]:
                    result_lines.append(f"- {item}")
            result_lines.append("")

        await send_long_message(message, "\n".join(result_lines))
        if any_success:
            await message.reply_text(
                "Load each code separately in SportyBet:\n"
                "Betslip → Load Booking Code → Paste"
            )
        _clear_pick_runtime(context)
        return ConversationHandler.END

    combo = context.user_data.get("pick_combo", [])
    if not combo and bundle:
        combo = bundle[0].get("picks", [])
    if not combo:
        await message.reply_text("No picks to book. Use /pick to start.")
        return ConversationHandler.END

    await message.reply_text("📲 Generating your SportyBet booking code...")
    booking_result = await _book_ticket_picks(combo)
    total_odds, _avg_conf = _ticket_totals(combo)

    if booking_result["code"]:
        await message.reply_text(
            f"🎫 *SportyBet Booking Code:* `{booking_result['code']}`\n\n"
            f"Tap the code to copy, then:\n"
            f"SportyBet App → Betslip → Load Booking Code → Paste\n\n"
            f"Selections: {booking_result['selection_count']} | Total Odds: ~{total_odds:.2f}",
            parse_mode="Markdown",
        )
    else:
        await message.reply_text(
            "⚠️ Booking code generation failed.\n"
            "SportyBet API might be temporarily unavailable.\n"
            "You can manually select the games above."
        )
    if booking_result["failed"] and booking_result["selection_count"]:
        await message.reply_text(
            "⚠️ Some picks couldn't be booked:\n" + "\n".join(booking_result["failed"])
        )
    elif booking_result["failed"]:
        await message.reply_text(
            "⚠️ Could not book — no valid selections.\n" + "\n".join(booking_result["failed"])
        )

    _clear_pick_runtime(context)
    return ConversationHandler.END


async def _book_ticket_picks(combo: list[dict]) -> dict:
    """Build and submit one booking ticket to SportyBet."""
    if not combo:
        return {"code": None, "failed": ["No picks in ticket"], "selection_count": 0}

    booking_selections = []
    failed_bookings = []
    for p in combo:
        event_id = p.get("event_id", "")
        sporty_event = p.get("_sporty_event")
        logger.info(f"Booking: {p['home']} vs {p['away']} | market={p['market']} pick={p['pick']} | event_id={event_id} | has_sporty_event={bool(sporty_event)}")

        if not event_id:
            failed_bookings.append(f"{p['home']} vs {p['away']}: no event ID")
            continue

        if sporty_event:
            sel = await asyncio.to_thread(build_booking_selection, sporty_event, p["market"], p["pick"])
        else:
            found = await asyncio.to_thread(find_event, p["home"], p["away"])
            logger.info(f"  find_event result: {bool(found)}")
            if found:
                sel = await asyncio.to_thread(build_booking_selection, found, p["market"], p["pick"])
            else:
                failed_bookings.append(f"{p['home']} vs {p['away']}: not found on SportyBet")
                continue

        logger.info(f"  selection: {sel}")
        if sel:
            booking_selections.append(sel)
        else:
            failed_bookings.append(f"{p['home']} vs {p['away']}: could not build selection")

    code = None
    if booking_selections:
        code = await asyncio.to_thread(create_booking_code, booking_selections)
    return {
        "code": code,
        "failed": failed_bookings,
        "selection_count": len(booking_selections),
    }


async def _show_pick_change_options(message, context, idx: int, edit: bool = False, intro: str | None = None):
    """Show alternative markets for one pick inside the current combo."""
    combo = context.user_data.get("pick_combo", [])
    if idx < 0 or idx >= len(combo):
        return await _pick_build_combo(message, context)

    pick = combo[idx]
    context.user_data["pick_change_idx"] = idx

    all_scored = context.user_data.get("pick_all_scored", [])
    match_key = _match_key(pick)
    current_pick_key = _pick_key(pick)

    match_alts = [
        p for p in all_scored
        if _match_key(p) == match_key
        and _pick_key(p) != current_pick_key
        and p.get("odds", 0) > 1.0
    ]
    match_alts.sort(key=lambda p: p.get("confidence", 0), reverse=True)

    deduped_alts = []
    seen_alt_keys = set()
    for alt in match_alts:
        alt_key = _pick_key(alt)
        if alt_key in seen_alt_keys:
            continue
        seen_alt_keys.add(alt_key)
        deduped_alts.append(alt)
    match_alts = deduped_alts[:4]

    if not match_alts:
        await _send_or_edit(
            message,
            "No alternative markets found for this match.\nRebuilding...",
            edit=edit,
        )
        if context.user_data.get("pick_bundle_review_mode") == "ticket":
            return await _show_pick_bundle_ticket_detail(message, context, edit=True)
        return await _pick_build_combo(message, context)

    context.user_data["pick_change_alts"] = match_alts

    lines = [f"🔄 *{pick['home']} vs {pick['away']}*", ""]
    if intro:
        lines.append(intro)
        lines.append("")
    lines.append(
        f"Current: {pick['market']}: {pick['pick']} @ {pick['odds']:.2f} "
        f"[{pick.get('confidence', '?')}%]"
    )
    lines.append("")
    lines.append("Best alternatives by analysis:")

    buttons = []
    for alt_idx, alt in enumerate(match_alts):
        conf = alt.get("confidence", 0)
        odds = alt.get("odds", 0)
        market = alt.get("market", "")
        pick_str = alt.get("pick", "")

        if market == "Over/Under":
            threshold = _pick_threshold(alt)
            label = f"Over {threshold}" if threshold else pick_str
        elif market == "1X2":
            label = f"{pick_str} Win" if pick_str in ("Home", "Away") else pick_str
        elif market == "GG/NG":
            label = "BTTS Yes" if pick_str == "GG" else pick_str
        elif market == "HT Over/Under":
            threshold = _pick_threshold(alt)
            label = f"HT Over {threshold}" if threshold else f"HT {pick_str}"
        else:
            label = f"{market}: {pick_str}"

        display = f"{label} @ {odds:.2f} [{conf}%]"
        lines.append(f"  {display}")

        reasons = alt.get("analysis_reasons", [])
        if reasons:
            lines.append(f"    ↳ {reasons[0]}")

        buttons.append([InlineKeyboardButton(display, callback_data=f"pick_mkt_{idx}_alt_{alt_idx}")])

    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick_mkt_back")])

    await _send_or_edit(
        message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        edit=edit,
        parse_mode="Markdown",
    )
    return PICK_REVIEW


async def pick_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle exclude/reshuffle/confirm buttons in pick flow."""
    query = update.callback_query
    await query.answer()
    data = query.data
    bundle_mode = context.user_data.get("pick_bundle_review_mode")

    if data == "pick_bundle_book":
        return await _pick_confirm_and_book(query.message, context)

    if data == "pick_bundle_reshuffle":
        seed = context.user_data.get("pick_shuffle_seed", 0) + 1
        context.user_data["pick_shuffle_seed"] = seed
        context.user_data["pick_bundle_review_mode"] = "bundle"
        await query.edit_message_text(f"🔄 Rebuilding bundle (seed {seed})...")
        return await _pick_build_combo(query.message, context)

    if data == "pick_bundle_back":
        _sync_active_bundle_ticket_from_combo(context)
        context.user_data["pick_bundle_review_mode"] = "bundle"
        return await _show_pick_bundle_summary(query.message, context, edit=True)

    if data.startswith("pick_bundle_edit_"):
        idx = int(data.replace("pick_bundle_edit_", ""))
        bundle = context.user_data.get("pick_bundle", [])
        if 0 <= idx < len(bundle):
            context.user_data["active_ticket_index"] = idx
            context.user_data["pick_bundle_review_mode"] = "ticket"
            context.user_data["pick_combo"] = [dict(p) for p in bundle[idx].get("picks", [])]
            context.user_data["pick_excluded"] = set(bundle[idx].get("_excluded_pick_keys", set()))
            context.user_data.pop("pick_change_idx", None)
            context.user_data.pop("pick_change_alts", None)
            return await _show_pick_bundle_ticket_detail(query.message, context, edit=True)
        return PICK_REVIEW

    if data == "pick_explain_all":
        combo = context.user_data.get("pick_combo", [])
        if combo:
            await _explain_picks(query.message, context, combo, [])
        return PICK_REVIEW

    if data == "pick_confirm":
        combo = context.user_data.get("pick_combo", [])
        if not combo:
            await query.edit_message_text("No picks. Start over with /pick.")
            return ConversationHandler.END

        total_odds = 1.0
        lines = ["✅ Final Selection:\n"]
        for i, p in enumerate(combo, 1):
            market_label = p["pick"].replace("(total=", "").replace(")", "") if "total=" in p["pick"] else p["pick"]
            lines.append(f"{i}. {p['home']} vs {p['away']}")
            lines.append(f"   {p['market']}: {market_label} @ {p['odds']:.2f}")
            total_odds *= p["odds"]
        lines.append(f"\nTotal Odds: {total_odds:.2f}")
        lines.append(f"Selections: {len(combo)}")

        await query.edit_message_text("\n".join(lines))

        return await _pick_confirm_and_book(query.message, context)

    elif data == "pick_reshuffle":
        seed = context.user_data.get("pick_shuffle_seed", 0) + 1
        context.user_data["pick_shuffle_seed"] = seed
        await query.edit_message_text(f"🔄 Reshuffling (seed {seed})...")
        return await _pick_build_combo(query.message, context)

    elif data.startswith("pick_swap_"):
        idx = int(data.replace("pick_swap_", ""))
        return await _show_pick_change_options(
            query.message,
            context,
            idx,
            edit=True,
            intro="Swap suggestions now open the market picker so you can choose the best replacement.",
        )

    elif data.startswith("pick_change_"):
        idx = int(data.replace("pick_change_", ""))
        return await _show_pick_change_options(query.message, context, idx, edit=True)

    elif data.startswith("pick_mkt_"):
        # Handle market selection for a specific pick
        if data == "pick_mkt_back":
            return await _pick_build_combo(query.message, context)

        combo = context.user_data.get("pick_combo", [])
        idx = context.user_data.get("pick_change_idx", -1)
        if idx < 0 or idx >= len(combo):
            return await _pick_build_combo(query.message, context)

        pick = combo[idx]

        # Parse: pick_mkt_{combo_idx}_alt_{alt_idx}
        parts = data.split("_")
        # parts: ['pick', 'mkt', combo_idx, 'alt', alt_idx]
        if len(parts) >= 5 and parts[3] == "alt":
            alt_idx = int(parts[4])
            alts = context.user_data.get("pick_change_alts", [])
            if 0 <= alt_idx < len(alts):
                alt = alts[alt_idx]
                old_desc = f"{pick['market']}: {pick['pick']} @ {pick['odds']:.2f}"

                # Copy all relevant fields from the alternative
                pick["market"] = alt["market"]
                pick["pick"] = alt["pick"]
                pick["odds"] = alt.get("odds", pick["odds"])
                pick["confidence"] = alt.get("confidence", pick["confidence"])
                pick["data_confidence"] = alt.get("data_confidence", pick.get("data_confidence", pick["confidence"]))
                pick["verdict"] = alt.get("verdict", pick.get("verdict", "moderate"))
                pick["analysis_reasons"] = alt.get("analysis_reasons", [])
                pick["suggestion"] = None

                await query.edit_message_text(
                    f"🔄 Changed #{idx+1}: {old_desc}\n"
                    f"   → {pick['market']}: {pick['pick']} @ {pick['odds']:.2f}\nRebuilding..."
                )
            else:
                await query.edit_message_text("Alternative not found.\nRebuilding...")
        else:
            await query.edit_message_text("Rebuilding...")

        if bundle_mode == "ticket":
            _sync_active_bundle_ticket_from_combo(context)
            return await _show_pick_bundle_ticket_detail(query.message, context, edit=True)
        return await _pick_build_combo(query.message, context)

    elif data.startswith("pick_exclude_"):
        idx = int(data.replace("pick_exclude_", ""))
        combo = context.user_data.get("pick_combo", [])
        if 0 <= idx < len(combo):
            removed = combo[idx]
            context.user_data.setdefault("pick_excluded", set()).add(_pick_key(removed))
            await query.edit_message_text(f"❌ Excluded: {removed['home']} vs {removed['away']} ({removed['pick']})\nRebuilding...")
        return await _pick_build_combo(query.message, context)

    return PICK_REVIEW


async def pick_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle natural language during PICK_REVIEW — semantic understanding via Gemini."""
    text = update.message.text.strip()
    combo = context.user_data.get("pick_combo", [])
    if not combo:
        await update.message.reply_text("No active combo. Use /pick to start.")
        return ConversationHandler.END

    # Route through semantic layer (Gemini understands intent)
    result = await asyncio.to_thread(
        gemini_chat.parse_review_message, text, len(combo)
    )

    intent = result.get("intent", "chat")
    params = result.get("params", {})
    reply = result.get("reply", "")

    # ── Book ──
    if intent == "book":
        if reply:
            await update.message.reply_text(reply)
        return await _pick_confirm_and_book(update.message, context)

    # ── Reshuffle ──
    if intent == "reshuffle":
        seed = context.user_data.get("pick_shuffle_seed", 0) + 1
        context.user_data["pick_shuffle_seed"] = seed
        await update.message.reply_text(reply or "🔄 Let me find you a different set...")
        return await _pick_build_combo(update.message, context)

    # ── Explain ──
    if intent == "explain":
        pick_nums = params.get("picks", [])
        return await _explain_picks(update.message, context, combo, pick_nums)

    # ── Edit ──
    if intent == "edit":
        # Extract actions from params (Gemini structured) or fall back to regex
        actions = params.get("actions")
        if not actions:
            actions = await _parse_combo_edit(text, len(combo))

        if not actions:
            await update.message.reply_text(
                reply or "Hmm, I'm not sure what you want to change. You can say things like:\n"
                "• \"drop 1 and 3\"\n"
                "• \"make 2 an over 1.5\"\n"
                "• \"switch 4 to home win\""
            )
            return PICK_REVIEW

        return await _apply_combo_edits(update.message, context, combo, actions)

    # ── Chat (unclear / conversational) ──
    if reply:
        await update.message.reply_text(reply)
    else:
        await update.message.reply_text(
            "I'm here if you need anything! You can:\n"
            "• Edit picks (\"remove 2\", \"change 5 to over 1.5\")\n"
            "• Ask why (\"explain pick 3\", \"why that one?\")\n"
            "• Confirm (\"book it\", \"looks good\")\n"
            "• Reshuffle (\"give me different ones\")"
        )
    return PICK_REVIEW


async def _explain_picks(message, context, combo, pick_nums):
    """Generate data-backed explanations for picks."""
    if not pick_nums:
        pick_nums = list(range(1, len(combo) + 1))  # Explain all

    lines = ["📖 *Pick Synopsis*\n"]

    for num in pick_nums:
        idx = num - 1
        if idx < 0 or idx >= len(combo):
            continue
        p = combo[idx]

        conf = p.get("data_confidence", p.get("confidence", 0))
        dq = p.get("data_quality", "unknown")
        market_label = p.get("pick", "").replace("(total=", "").replace(")", "")

        lines.append(f"*{num}. {p['home']} vs {p['away']}*")
        lines.append(f"   {p['market']}: {market_label} @ {p['odds']:.2f} [{conf}%]\n")

        # Data quality context
        if dq == "good":
            lines.append("   📊 Full form data — last 10 matches per team, venue-split")
        elif dq == "fair":
            lines.append("   📉 Partial form data available")
        elif dq == "limited":
            lines.append("   ⚠️ Limited data — odds-based estimate only")

        # All analysis reasons (the meat of the explanation)
        reasons = p.get("analysis_reasons", [])
        if reasons:
            lines.append("   *The case for this pick:*")
            for r in reasons:
                lines.append(f"   • {r}")
        else:
            lines.append("   No detailed breakdown available for this one.")

        # Verdict explanation
        verdict = p.get("verdict", "")
        if verdict == "strong":
            lines.append(f"\n   ✅ Strong — the data backs this one well")
        elif verdict == "moderate":
            lines.append(f"\n   🟡 Moderate — solid case but not a slam dunk")
        elif verdict == "weak":
            lines.append(f"\n   🟠 Weaker — mainly here to reach the odds target")

        lines.append("")

    await send_long_message(message, "\n".join(lines))

    # Return to the right review state
    if context.user_data.get("_check_mode"):
        return CHECK_REVIEW
    return PICK_REVIEW


async def _apply_combo_edits(message, context, combo, actions):
    """Apply edit actions to the combo (shared between /pick and /check)."""
    all_scored = context.user_data.get("pick_all_scored", context.user_data.get("check_all_scored", []))
    excluded = context.user_data.get("pick_excluded", context.user_data.get("check_excluded", set()))
    changes_made = []
    needs_rebuild = False

    for action in actions:
        idx = action.get("index", 0) - 1  # User uses 1-based
        if idx < 0 or idx >= len(combo):
            continue

        if action["type"] == "remove":
            excluded.add(_pick_key(combo[idx]))
            changes_made.append(f"❌ Dropped #{idx+1}: {combo[idx]['home']} vs {combo[idx]['away']}")
            needs_rebuild = True

        elif action["type"] == "change":
            target_market = action.get("market", "")
            target_threshold = action.get("threshold")
            match_key = f"{combo[idx]['home']}_{combo[idx]['away']}"

            candidates = [
                p for p in all_scored
                if f"{p['home']}_{p['away']}" == match_key
                and p.get("odds", 0) > 1.0
            ]

            replacement = None
            if target_market in ("over", "Over/Under"):
                t = target_threshold or 1.5
                for c in sorted(candidates, key=lambda p: p.get("confidence", 0), reverse=True):
                    if c["market"] == "Over/Under" and _pick_threshold(c) == t:
                        replacement = c
                        break
            elif target_market in ("home", "home win", "1X2"):
                for c in sorted(candidates, key=lambda p: p.get("confidence", 0), reverse=True):
                    if c["market"] == "1X2" and c.get("pick") == "Home":
                        replacement = c
                        break
            elif target_market in ("away", "away win"):
                for c in sorted(candidates, key=lambda p: p.get("confidence", 0), reverse=True):
                    if c["market"] == "1X2" and c.get("pick") == "Away":
                        replacement = c
                        break
            elif target_market in ("btts", "gg", "GG/NG"):
                for c in sorted(candidates, key=lambda p: p.get("confidence", 0), reverse=True):
                    if c["market"] == "GG/NG":
                        replacement = c
                        break
            elif target_market in ("draw", "x"):
                for c in sorted(candidates, key=lambda p: p.get("confidence", 0), reverse=True):
                    if c["market"] == "1X2" and c.get("pick") == "Draw":
                        replacement = c
                        break

            if replacement:
                old = f"{combo[idx]['market']}: {combo[idx]['pick']}"
                combo[idx] = replacement
                changes_made.append(
                    f"🔄 #{idx+1}: {old} → {replacement['market']}: {replacement['pick']} "
                    f"@ {replacement['odds']:.2f} [{replacement['confidence']}%]"
                )
            else:
                changes_made.append(f"⚠️ #{idx+1}: couldn't find {target_market} for that match")

    # Update excluded set
    context.user_data["pick_excluded"] = excluded
    if "check_excluded" in context.user_data:
        context.user_data["check_excluded"] = excluded

    if changes_made:
        await message.reply_text("\n".join(changes_made) + "\n\nRebuilding...")

    if context.user_data.get("pick_bundle_review_mode") == "ticket" and not needs_rebuild:
        _sync_active_bundle_ticket_from_combo(context)
        return await _show_pick_bundle_ticket_detail(message, context, edit=False)

    # Route back to appropriate build function
    if context.user_data.get("_check_mode"):
        return await _check_build_and_show(message, context)
    return await _pick_build_combo(message, context)


async def _parse_combo_edit(text: str, combo_size: int) -> list[dict]:
    """Parse natural language combo edits into structured actions.

    Returns list of:
      {"type": "remove", "index": int}
      {"type": "change", "index": int, "market": str, "threshold": float|None}
    """
    # Try Gemini first
    try:
        import gemini_chat
        result = await asyncio.to_thread(
            gemini_chat.chat,
            f"Parse this combo edit instruction for a {combo_size}-pick betting combo. "
            f"Return JSON with key 'actions' containing a list of actions.\n"
            f"Each action: {{\"type\": \"remove\"|\"change\", \"index\": <1-based game number>, "
            f"\"market\": \"over\"|\"home\"|\"away\"|\"btts\" (for change only), "
            f"\"threshold\": <float like 0.5, 1.5, 2.5> (for over only)}}\n\n"
            f"User says: {text}",
            {},
        )
        # Gemini returns {"intent": ..., "reply": ..., "params": ...}
        # But we asked for actions in the reply — try parsing
        reply = result.get("reply", "")
        params = result.get("params", {})
        if "actions" in params:
            return params["actions"]
        # Try parsing reply as JSON
        import json
        try:
            parsed = json.loads(reply)
            if "actions" in parsed:
                return parsed["actions"]
        except (json.JSONDecodeError, TypeError):
            pass
    except Exception as e:
        logger.debug(f"Gemini edit parse failed: {e}")

    # Fallback: regex parsing
    return _regex_parse_combo_edit(text)


def _regex_parse_combo_edit(text: str) -> list[dict]:
    """Regex fallback for parsing combo edit instructions."""
    import re
    actions = []
    text_lower = text.lower()

    # "remove 1, 2 and 5" or "drop games 1, 3, 5"
    remove_match = re.search(r"(?:remove|drop|delete|exclude)\s*(?:games?\s*)?(.+?)(?:\.|$|,\s*(?:change|swap))", text_lower)
    if remove_match:
        nums = re.findall(r"\d+", remove_match.group(1))
        for n in nums:
            actions.append({"type": "remove", "index": int(n)})

    # "change 7 to over 1.5" or "swap 3 to home win"
    change_patterns = re.finditer(
        r"(?:change|swap)\s*(?:games?\s*)?(\d+(?:\s*(?:,|and)\s*\d+)*)\s*to\s+(.+?)(?:\.|$|,\s*(?:change|swap|remove))",
        text_lower,
    )
    for m in change_patterns:
        nums = re.findall(r"\d+", m.group(1))
        target = m.group(2).strip()

        market = None
        threshold = None

        if "over" in target:
            market = "over"
            t_match = re.search(r"(\d+\.?\d*)", target)
            threshold = float(t_match.group(1)) if t_match else 1.5
        elif "home" in target or "home win" in target:
            market = "home"
        elif "away" in target or "away win" in target:
            market = "away"
        elif "btts" in target or "gg" in target or "both" in target:
            market = "btts"

        if market:
            for n in nums:
                actions.append({"type": "change", "index": int(n), "market": market, "threshold": threshold})

    return actions


async def pick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the pick flow."""
    _clear_pick_runtime(context)
    await update.message.reply_text("Pick cancelled.")
    return ConversationHandler.END


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refresh cached data — fetches fixtures from football-data.org (2-week window)."""
    config = load_user_config(chat_id=update.effective_chat.id)
    leagues = config.get("leagues", [])

    if not leagues:
        await update.message.reply_text("No leagues configured. Run /leagues first.")
        return

    days_ahead = config.get("days_ahead", 7)
    today = datetime.now()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    await update.message.reply_text(
        f"🔄 Refreshing {len(leagues)} leagues ({date_from} to {date_to})..."
    )

    total_fixtures = 0
    league_results = []

    for lid in leagues:
        name = LEAGUE_NAMES.get(lid, str(lid))
        try:
            fixtures = await asyncio.to_thread(get_fixtures_lookahead, lid, date_from=date_from, date_to=date_to)
            total_fixtures += len(fixtures)
            league_results.append(f"  {name}: {len(fixtures)} matches")
        except Exception as e:
            league_results.append(f"  {name}: ERROR - {e}")

    msg = (
        f"✅ Refresh complete\n\n"
        + "\n".join(league_results) + "\n\n"
        f"Total fixtures: {total_fixtures}\n"
        f"Window: {date_from} to {date_to}\n\n"
        f"Run /pick to get selections."
    )
    await update.message.reply_text(msg)


async def cmd_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show API budget status."""
    budget = await asyncio.to_thread(get_api_budget)
    bar_len = 20
    used_bars = int(budget["used"] / budget["limit"] * bar_len)
    bar = "█" * used_bars + "░" * (bar_len - used_bars)

    msg = (
        f"API Budget ({budget['date']})\n\n"
        f"[{bar}]\n"
        f"Used: {budget['used']}/{budget['limit']}\n"
        f"Remaining: {budget['remaining']}"
    )
    await update.message.reply_text(msg)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current configuration and data freshness."""
    from config import TIMEFRAME_PRESETS

    config = load_user_config(chat_id=update.effective_chat.id)
    budget = await asyncio.to_thread(get_api_budget)
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config.get("leagues", [])]

    pick_cfg = _get_pick_config(config)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
    min_conf = config.get("min_confidence", pick_cfg.get("min_confidence", 75))
    min_odds = config.get("min_odds", pick_cfg.get("min_odds", 1.05))
    timeframe = config.get("timeframe", "7days")
    timeframe_label = TIMEFRAME_PRESETS.get(timeframe, TIMEFRAME_PRESETS["7days"]).get("label", "7 days")

    msg = (
        f"SportyBot Status\n\n"
        f"Leagues: {', '.join(league_names) or 'None'}\n"
        f"Confidence: ≥{min_conf}%\n"
        f"Min Odds: ≥{min_odds}\n"
        f"Markets: {', '.join(enabled)}\n"
        f"Timeframe: {timeframe_label}\n"
        f"API Budget: {budget['remaining']}/{budget['limit']} remaining\n"
        f"Date: {budget['date']}"
    )
    await update.message.reply_text(msg)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings panel — confidence, markets, min odds."""
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    config = load_user_config(chat_id=chat_id)

    min_conf = config.get("min_confidence", 75)
    min_odds = config.get("min_odds", 1.05)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)

    lines = [
        "⚙️ *Pick Settings*\n",
        f"Confidence: ≥{min_conf}%",
        f"Min Odds: ≥{min_odds}",
        f"Markets: {', '.join(enabled)}",
        "",
        "Tap to adjust:",
    ]

    buttons = [
        [InlineKeyboardButton(f"🎯 Confidence ({min_conf}%)", callback_data="strat_conf_menu")],
        [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
        [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
    ]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strategy_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings panel navigation."""
    query = update.callback_query
    await query.answer()
    data = query.data

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)

    if data == "strat_conf_menu":
        # Show confidence range buttons
        current = config.get("min_confidence", 75)
        buttons = [
            [
                InlineKeyboardButton(f"{'✅ ' if current == 50 else ''}50%", callback_data="strat_conf_50"),
                InlineKeyboardButton(f"{'✅ ' if current == 60 else ''}60%", callback_data="strat_conf_60"),
                InlineKeyboardButton(f"{'✅ ' if current == 65 else ''}65%", callback_data="strat_conf_65"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == 70 else ''}70%", callback_data="strat_conf_70"),
                InlineKeyboardButton(f"{'✅ ' if current == 75 else ''}75%", callback_data="strat_conf_75"),
                InlineKeyboardButton(f"{'✅ ' if current == 80 else ''}80%", callback_data="strat_conf_80"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == 85 else ''}85%", callback_data="strat_conf_85"),
                InlineKeyboardButton(f"{'✅ ' if current == 90 else ''}90%", callback_data="strat_conf_90"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")],
        ]
        await query.edit_message_text(
            f"🎯 Minimum Confidence\nCurrent: {current}%\n\n"
            "Lower = more picks (riskier)\nHigher = fewer picks (safer)\n\n"
            "Recommended: 75% for balanced results",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_CONFIDENCE

    elif data == "strat_mkt_menu":
        # Show market toggle panel
        enabled = set(config.get("enabled_markets", DEFAULT_ENABLED_MARKETS))
        buttons = []
        for mkt_name, mkt_info in AVAILABLE_MARKETS.items():
            check = "✅" if mkt_name in enabled else "⬜"
            state = "on" if mkt_name in enabled else "off"
            buttons.append([InlineKeyboardButton(
                f"{check} {mkt_name} — {mkt_info['description']}",
                callback_data=f"strat_mkt_{state}_{mkt_name}",
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="strat_back")])
        await query.edit_message_text(
            "📊 Market Selection\nTap to toggle on/off:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_MARKETS

    elif data == "strat_odds_menu":
        # Show min odds buttons
        current = config.get("min_odds", 1.05)
        options = [1.01, 1.05, 1.10, 1.15, 1.20, 1.30, 1.40, 1.60]
        row1 = []
        row2 = []
        for i, o in enumerate(options):
            mark = "✅ " if abs(current - o) < 0.005 else ""
            btn = InlineKeyboardButton(f"{mark}{o:.2f}", callback_data=f"strat_minodds_{o:.2f}")
            if i < 4:
                row1.append(btn)
            else:
                row2.append(btn)
        buttons = [row1, row2, [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")]]
        await query.edit_message_text(
            f"💰 Minimum Odds Per Pick\nCurrent: {current}\n\n"
            "Lower = allows safer low-odds picks (e.g. Over 0.5)\n"
            "Higher = filters for bigger odds per pick",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_MIN_ODDS

    elif data == "strat_back":
        # Back to main settings
        min_conf = config.get("min_confidence", 75)
        min_odds = config.get("min_odds", 1.05)
        enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
        buttons = [
            [InlineKeyboardButton(f"🎯 Confidence ({min_conf}%)", callback_data="strat_conf_menu")],
            [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
            [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
            [InlineKeyboardButton("✅ Done", callback_data="strat_done")],
        ]
        await query.edit_message_text(
            f"⚙️ *Pick Settings*\n\n"
            f"Confidence: ≥{min_conf}%\n"
            f"Min Odds: ≥{min_odds}\n"
            f"Markets: {', '.join(enabled)}\n\n"
            "Tap to adjust:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return STRAT_CUSTOM_CONFIDENCE

    elif data == "strat_done":
        min_conf = config.get("min_confidence", 75)
        min_odds = config.get("min_odds", 1.05)
        enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
        await query.edit_message_text(
            f"✅ Settings saved!\n\n"
            f"Confidence: ≥{min_conf}%\n"
            f"Min Odds: ≥{min_odds}\n"
            f"Markets: {', '.join(enabled)}\n\n"
            "Run /pick to get selections."
        )
        return ConversationHandler.END

    # Legacy preset fallback
    preset_key = data.replace("strat_", "")
    if preset_key in STRATEGY_PRESETS:
        preset = STRATEGY_PRESETS[preset_key]
        config["min_confidence"] = preset["min_confidence"]
        config["min_odds"] = preset.get("min_odds", 1.10)
        config["enabled_markets"] = preset["preferred_markets"]
        save_user_config(config, chat_id=chat_id)
        await query.edit_message_text(
            f"Settings applied from {preset['label']} preset.\n\n"
            f"Confidence: ≥{preset['min_confidence']}%\n"
            f"Min Odds: ≥{preset.get('min_odds', 1.10)}\n"
            f"Markets: {', '.join(preset['preferred_markets'])}\n\n"
            "Run /pick to get selections."
        )
        return ConversationHandler.END

    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_custom_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confidence selection."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_conf_menu", "strat_mkt_menu", "strat_odds_menu", "strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    conf = int(data.replace("strat_conf_", ""))
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["min_confidence"] = conf
    save_user_config(config, chat_id=chat_id)

    # Show updated main menu
    min_odds = config.get("min_odds", 1.05)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
    buttons = [
        [InlineKeyboardButton(f"🎯 Confidence ({conf}%)", callback_data="strat_conf_menu")],
        [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
        [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
        [InlineKeyboardButton("✅ Done", callback_data="strat_done")],
    ]
    await query.edit_message_text(
        f"✅ Confidence set to {conf}%\n\n"
        f"⚙️ *Pick Settings*\n"
        f"Confidence: ≥{conf}%\n"
        f"Min Odds: ≥{min_odds}\n"
        f"Markets: {', '.join(enabled)}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_custom_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle market toggle in settings."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    enabled = config.get("enabled_markets", list(DEFAULT_ENABLED_MARKETS))

    # Parse toggle: strat_mkt_{on|off}_{market_name}
    parts = data.replace("strat_mkt_", "").split("_", 1)
    current_state = parts[0]
    market_name = parts[1]

    if current_state == "on" and market_name in enabled:
        enabled.remove(market_name)
    elif current_state == "off" and market_name not in enabled:
        enabled.append(market_name)

    # Ensure at least one market is enabled
    if not enabled:
        enabled = list(DEFAULT_ENABLED_MARKETS)

    config["enabled_markets"] = enabled
    save_user_config(config, chat_id=chat_id)

    # Rebuild buttons
    buttons = []
    active = set(enabled)
    for mkt_name, mkt_info in AVAILABLE_MARKETS.items():
        check = "✅" if mkt_name in active else "⬜"
        state = "on" if mkt_name in active else "off"
        buttons.append([InlineKeyboardButton(
            f"{check} {mkt_name} — {mkt_info['description']}",
            callback_data=f"strat_mkt_{state}_{mkt_name}",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="strat_back")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    return STRAT_CUSTOM_MARKETS


async def callback_strat_custom_over(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy — redirect to main settings."""
    return await callback_strategy_select(update, context)


async def callback_strat_custom_min_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle min odds selection."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    min_odds = float(data.replace("strat_minodds_", ""))
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["min_odds"] = min_odds
    save_user_config(config, chat_id=chat_id)

    # Show updated main menu
    min_conf = config.get("min_confidence", 75)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
    buttons = [
        [InlineKeyboardButton(f"🎯 Confidence ({min_conf}%)", callback_data="strat_conf_menu")],
        [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
        [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
        [InlineKeyboardButton("✅ Done", callback_data="strat_done")],
    ]
    await query.edit_message_text(
        f"✅ Min Odds set to {min_odds}\n\n"
        f"⚙️ *Pick Settings*\n"
        f"Confidence: ≥{min_conf}%\n"
        f"Min Odds: ≥{min_odds}\n"
        f"Markets: {', '.join(enabled)}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def strategy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the settings flow."""
    context.user_data.pop("custom_strategy", None)
    await update.message.reply_text("Settings cancelled.")
    return ConversationHandler.END


async def cmd_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the time window for /pick fixture scanning."""
    from config import TIMEFRAME_PRESETS

    config = load_user_config(chat_id=update.effective_chat.id)
    current = config.get("timeframe", "7days")
    current_label = TIMEFRAME_PRESETS.get(current, {}).get("label", current)

    buttons = [
        [
            InlineKeyboardButton("Today", callback_data="tf_today"),
            InlineKeyboardButton("Tomorrow", callback_data="tf_tomorrow"),
            InlineKeyboardButton("Weekend", callback_data="tf_weekend"),
        ],
        [
            InlineKeyboardButton("Next 7 Days", callback_data="tf_7days"),
            InlineKeyboardButton("Next 14 Days", callback_data="tf_14days"),
        ],
    ]
    await update.message.reply_text(
        f"Select time window for /pick:\nCurrent: {current_label}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe button press."""
    from config import TIMEFRAME_PRESETS

    query = update.callback_query
    await query.answer()
    preset_key = query.data.replace("tf_", "")
    preset = TIMEFRAME_PRESETS.get(preset_key)
    if not preset:
        await query.edit_message_text("Unknown timeframe. Try /timeframe again.")
        return

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["timeframe"] = preset_key
    save_user_config(config, chat_id=chat_id)
    await query.edit_message_text(
        f"✅ Timeframe set to: {preset['label']}\n\nRun /pick to get selections."
    )


# ── /check conversation flow ─────────────────────────────────────────────────

async def check_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the check flow. If codes given inline, skip to review."""
    context.user_data["chat_id"] = update.effective_chat.id
    args = context.args or []

    if args:
        # Codes provided inline: /check ABC123,DEF456
        raw = " ".join(args)
        codes = [c.strip().upper() for c in raw.replace(" ", ",").split(",") if c.strip()]
        return await _process_codes(update, context, codes)

    await update.message.reply_text(
        "📋 Send me one or more SportyBet booking codes.\n"
        "Separate multiple codes with commas.\n\n"
        "Example: G0S7HA, ABC123, XYZ789\n\n"
        "Send /cancel to exit."
    )
    return CHECK_CODES


async def check_receive_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive booking codes from user."""
    raw = update.message.text.strip()
    codes = [c.strip().upper() for c in raw.replace(" ", ",").split(",") if c.strip()]

    if not codes:
        await update.message.reply_text("No valid codes found. Try again or /cancel.")
        return CHECK_CODES

    return await _process_codes(update, context, codes)


async def _process_codes(update: Update, context: ContextTypes.DEFAULT_TYPE, codes: list[str]):
    """Fetch booking codes, score them using scorer.py + real SportyBet odds, then ask to expand."""
    await update.message.reply_text(f"🔍 Fetching {len(codes)} code(s): {', '.join(codes)}...")

    all_picks = []
    valid_codes = []
    failed_codes = []

    for code in codes:
        data = await asyncio.to_thread(fetch_booking_code, code)
        if data:
            picks = parse_outcomes(data)
            all_picks.extend(picks)
            valid_codes.append(code)
        else:
            failed_codes.append(code)

    if failed_codes:
        await update.message.reply_text(f"Could not fetch: {', '.join(failed_codes)}")

    if not all_picks:
        await update.message.reply_text("No valid picks found in any code.")
        return ConversationHandler.END

    # ── Score booking code picks using the SAME pipeline as /pick ──
    code_pick_count = len(all_picks)
    progress_msg = await update.message.reply_text(
        f"🔬 Scoring {code_pick_count} game(s) with full analysis..."
    )

    all_scored = []
    analyzed_count = 0

    for pick in all_picks:
        if pick["match_status"] == "Ended":
            # Already completed — keep as-is with original data
            pick["_source"] = "booking_code"
            pick["data_confidence"] = pick.get("confidence", 50)
            pick["verdict"] = "won" if pick.get("is_winning") == 1 else "lost" if pick.get("is_winning") == 0 else "ended"
            pick["analysis_reasons"] = [f"Match ended: {pick.get('score', '?')}"]
            pick["suggestion"] = None
            pick["data_quality"] = "ended"
            all_scored.append(pick)
            continue

        home_name = pick.get("home", "")
        away_name = pick.get("away", "")

        try:
            # 1. Look up real SportyBet event for real odds
            sporty_event = await asyncio.to_thread(find_event, home_name, away_name)

            # 2. Fetch form data for scoring
            home_results = await asyncio.to_thread(get_team_results, home_name, count=10)
            away_results = await asyncio.to_thread(get_team_results, away_name, count=10)

            home_form = _summarize_form(home_results) if home_results else None
            away_form = _summarize_form(away_results) if away_results else None

            event_id = pick.get("event_id", "")
            if sporty_event:
                event_id = sporty_event.get("eventId", event_id)
            markets = sporty_event.get("markets", {}) if sporty_event else {}

            base_pick = {
                "home": home_name,
                "away": away_name,
                "league": pick.get("tournament", ""),
                "date": "",
                "match_status": pick.get("match_status", "Upcoming"),
                "is_winning": pick.get("is_winning"),
                "score": pick.get("score", ""),
                "event_id": event_id,
                "selection": pick.get("selection", {}),
                "tournament": pick.get("tournament", ""),
                "_source": "booking_code",
                "_sporty_event": sporty_event,
                "_original_pick": pick.get("pick", ""),
                "_original_market": pick.get("market", ""),
                "_original_odds": pick.get("odds", 1.0),
            }

            if home_form and away_form:
                data_quality = "good"
                scores = score_match(home_form, away_form, home_results, away_results)

                def _get_odds_1x2(outcome_id: str) -> float:
                    o = markets.get("1", {}).get("outcomes", {}).get(outcome_id, {})
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
                    o = markets.get("29", {}).get("outcomes", {}).get("74", {})
                    try:
                        return float(o.get("odds", "0"))
                    except (ValueError, TypeError):
                        return 0.0

                def _verdict(conf: int) -> str:
                    if conf >= 75:
                        return "strong"
                    elif conf >= 60:
                        return "moderate"
                    return "weak"

                # Score ALL markets for this match (same as /pick)
                match_scored = []

                # Home Win
                hw = scores["home_win"]
                real_home_odds = _get_odds_1x2("1")
                if real_home_odds > 1.0:
                    checked = cross_check_with_odds(hw["confidence"], real_home_odds)
                    conf = checked["confidence"]
                    reasons = hw["reasons"][:]
                    if checked["warning"]:
                        reasons.append(checked["warning"])
                    if conf >= 45:
                        match_scored.append({
                            **base_pick,
                            "market": "1X2", "pick": "Home",
                            "odds": real_home_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
                            "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": data_quality,
                            "rating": "safe" if conf >= 70 else "moderate",
                        })

                # Away Win
                aw = scores["away_win"]
                real_away_odds = _get_odds_1x2("3")
                if real_away_odds > 1.0:
                    checked = cross_check_with_odds(aw["confidence"], real_away_odds)
                    conf = checked["confidence"]
                    reasons = aw["reasons"][:]
                    if checked["warning"]:
                        reasons.append(checked["warning"])
                    if conf >= 45:
                        match_scored.append({
                            **base_pick,
                            "market": "1X2", "pick": "Away",
                            "odds": real_away_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
                            "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": data_quality,
                            "rating": "safe" if conf >= 70 else "moderate",
                        })

                # Over 0.5 / 1.5 / 2.5
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
                            match_scored.append({
                                **base_pick,
                                "market": "Over/Under",
                                "pick": f"Over (total={threshold})",
                                "odds": real_odds,
                                "confidence": conf, "data_confidence": conf,
                                "verdict": _verdict(conf),
                                "analysis_reasons": reasons,
                                "suggestion": None, "data_quality": data_quality,
                                "rating": "safe" if threshold <= 1.5 else "moderate",
                            })

                # BTTS
                bt = scores["btts"]
                gg_odds = _get_gg_odds()
                if gg_odds > 1.0:
                    checked = cross_check_with_odds(bt["confidence"], gg_odds)
                    conf = checked["confidence"]
                    reasons = bt["reasons"][:]
                    if checked["warning"]:
                        reasons.append(checked["warning"])
                    if conf >= 45:
                        match_scored.append({
                            **base_pick,
                            "market": "GG/NG", "pick": "GG",
                            "odds": gg_odds,
                            "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf),
                            "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": data_quality,
                            "rating": "moderate",
                        })

                # Extended markets
                _add_extended_picks(
                    match_scored, scores, markets, base_pick,
                    data_quality, _verdict,
                )

                all_scored.extend(match_scored)
                analyzed_count += 1

            else:
                # No form data — keep original pick with odds-only confidence
                pick["_source"] = "booking_code"
                pick["data_confidence"] = pick.get("confidence", 50)
                pick["verdict"] = _confidence_verdict(pick.get("confidence", 50))
                pick["analysis_reasons"] = ["⚠ No form data — odds-only estimate"]
                pick["suggestion"] = None
                pick["data_quality"] = "limited"
                pick["event_id"] = event_id
                pick["_sporty_event"] = sporty_event
                all_scored.append(pick)
                analyzed_count += 1

        except Exception as e:
            logger.warning(f"Check analysis failed for {home_name} vs {away_name}: {e}")
            pick["_source"] = "booking_code"
            pick["data_confidence"] = pick.get("confidence", 50)
            pick["verdict"] = "error"
            pick["analysis_reasons"] = [f"Analysis error: {str(e)[:50]}"]
            pick["suggestion"] = None
            pick["data_quality"] = "limited"
            all_scored.append(pick)

    try:
        await progress_msg.edit_text(
            f"✅ Scored {analyzed_count} matches — {len(all_scored)} picks available."
        )
    except Exception:
        pass

    # Store scored picks (same format as /pick's all_scored)
    context.user_data["check_all_scored"] = all_scored
    context.user_data["check_codes"] = valid_codes
    context.user_data["check_excluded"] = set()

    # Show summary of code game verdicts
    pending = [p for p in all_scored if p.get("verdict") not in ("won", "lost", "ended")]
    ended = [p for p in all_scored if p.get("verdict") in ("won", "lost", "ended")]

    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠", "avoid": "🔴", "won": "✅", "lost": "❌"}
    lines = [f"📊 Analysis — {len(all_picks)} games from {len(valid_codes)} code(s)\n"]

    if ended:
        won = sum(1 for p in ended if p.get("is_winning") == 1)
        lost = sum(1 for p in ended if p.get("is_winning") == 0)
        lines.append(f"Completed: {won} won, {lost} lost\n")

    # Show the ORIGINAL picks from the booking code with their new scores
    original_matches = set()
    for pick in all_picks:
        if pick["match_status"] == "Ended":
            continue
        mk = f"{pick['home']}_{pick['away']}"
        if mk in original_matches:
            continue
        original_matches.add(mk)

        # Find the scored version of the original pick's market
        orig_market = pick.get("market", "").lower()
        orig_pick = pick.get("pick", "").lower()
        best_match = None
        for sp in all_scored:
            if sp.get("home") == pick["home"] and sp.get("away") == pick["away"]:
                sp_market = sp.get("market", "").lower()
                sp_pick = sp.get("pick", "").lower()
                # Try matching the original market
                if orig_market in sp_market or sp_market in orig_market:
                    if not best_match or sp.get("confidence", 0) > best_match.get("confidence", 0):
                        best_match = sp
        if not best_match:
            # Just take the highest confidence pick for this match
            for sp in all_scored:
                if sp.get("home") == pick["home"] and sp.get("away") == pick["away"]:
                    if not best_match or sp.get("confidence", 0) > best_match.get("confidence", 0):
                        best_match = sp
        if best_match:
            icon = verdict_icons.get(best_match.get("verdict", ""), "❓")
            conf = best_match.get("confidence", "?")
            lines.append(
                f"{icon} {pick['home']} vs {pick['away']}\n"
                f"   Code: {pick['market']}: {pick['pick']} @ {pick['odds']:.2f}\n"
                f"   Score: {best_match['market']}: {best_match['pick']} [{conf}%]"
            )
            reasons = best_match.get("analysis_reasons", [])
            if reasons:
                lines.append(f"   > {reasons[0]}")
            lines.append("")

    await send_long_message(update, "\n".join(lines))

    # Ask user: expand with league picks or proceed with code games only?
    expand_buttons = [
        [InlineKeyboardButton("✅ Add more picks from SportyBet", callback_data="check_expand_yes")],
        [InlineKeyboardButton("🎯 Code games only", callback_data="check_expand_no")],
    ]
    await update.message.reply_text(
        "Want to add extra picks from your SportyBet leagues, or proceed with just the code games?",
        reply_markup=InlineKeyboardMarkup(expand_buttons),
    )

    return CHECK_EXPAND


async def check_expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle expand choice: add SportyBet league picks (scored via /pick's pipeline) or code only."""
    query = update.callback_query
    await query.answer()

    data = query.data
    all_scored = context.user_data.get("check_all_scored", [])

    if data == "check_expand_no":
        await query.edit_message_text("🎯 Got it — working with your code games.")
        return await _show_check_target_odds(query.message, context, edit=False)

    # data == "check_expand_yes" — scan SportyBet events for user's leagues
    # Uses the SAME scoring as /pick (SportyBet events + scorer.py)
    config = load_user_config(chat_id=update.effective_chat.id)
    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")

    if not leagues:
        await query.edit_message_text("No leagues set up yet — use /leagues to add some.\nWorking with your code games for now.")
        return await _show_check_target_odds(query.message, context, edit=False)

    await query.edit_message_text(
        f"📊 Scanning {len(leagues)} league(s) on SportyBet + analyzing form..."
    )

    # Track existing matches to avoid duplicates
    code_matches = set()
    for p in all_scored:
        code_matches.add(f"{p.get('home', '').lower()}_{p.get('away', '').lower()}")

    # Fetch SportyBet events for user's leagues
    from config import LEAGUE_SPORTYBET_NAMES
    tournament_filters = []
    for lid in leagues:
        tournament_filters.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    events = await asyncio.to_thread(fetch_all_events, max_pages=5, allowed_tournaments=tournament_filters)
    events = filter_events(events, league_ids=leagues, timeframe=timeframe)

    league_scored = []
    scanned = 0
    for ev in events:
        home_name = ev.get("home", "")
        away_name = ev.get("away", "")
        match_key = f"{home_name.lower()}_{away_name.lower()}"
        if match_key in code_matches:
            continue

        try:
            home_results = await asyncio.to_thread(get_team_results, home_name, count=10)
            away_results = await asyncio.to_thread(get_team_results, away_name, count=10)
            if not home_results or not away_results:
                continue

            home_form = _summarize_form(home_results)
            away_form = _summarize_form(away_results)

            event_id = ev.get("eventId", "")
            tournament = ev.get("tournament", "")
            markets = ev.get("markets", {})
            kick_off = ev.get("estimateStartTime", 0)
            try:
                match_date = datetime.fromtimestamp(kick_off / 1000).strftime("%Y-%m-%d %H:%M") if kick_off else ""
            except Exception:
                match_date = ""

            base_pick = {
                "home": home_name, "away": away_name,
                "league": tournament, "date": match_date,
                "match_status": "Upcoming", "is_winning": None,
                "score": "", "event_id": event_id,
                "selection": {}, "tournament": tournament,
                "_source": "league_scan", "_sporty_event": ev,
            }

            scores = score_match(home_form, away_form, home_results, away_results)
            real_1x2 = markets.get("1", {})
            real_gg = markets.get("29", {})

            def _get_odds_1x2(oid):
                o = real_1x2.get("outcomes", {}).get(oid, {})
                try: return float(o.get("odds", "0"))
                except: return 0.0

            def _get_over_odds(threshold):
                mkt = markets.get(f"18|total={threshold}", {})
                o = mkt.get("outcomes", {}).get("12", {})
                try: return float(o.get("odds", "0"))
                except: return 0.0

            def _get_gg_odds():
                o = real_gg.get("outcomes", {}).get("74", {})
                try: return float(o.get("odds", "0"))
                except: return 0.0

            def _verdict(conf):
                return "strong" if conf >= 75 else "moderate" if conf >= 60 else "weak"

            # Score all markets (same as /pick)
            for mkt_key, scorer_key, market_label, pick_label, odds_fn in [
                ("hw", "home_win", "1X2", "Home", lambda: _get_odds_1x2("1")),
                ("aw", "away_win", "1X2", "Away", lambda: _get_odds_1x2("3")),
            ]:
                sc = scores[scorer_key]
                real_odds = odds_fn()
                if real_odds > 1.0:
                    checked = cross_check_with_odds(sc["confidence"], real_odds)
                    conf = checked["confidence"]
                    reasons = sc["reasons"][:]
                    if checked["warning"]:
                        reasons.append(checked["warning"])
                    if conf >= 45:
                        league_scored.append({
                            **base_pick, "market": market_label, "pick": pick_label,
                            "odds": real_odds, "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf), "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": "good",
                            "rating": "safe" if conf >= 70 else "moderate",
                        })

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
                        league_scored.append({
                            **base_pick, "market": "Over/Under",
                            "pick": f"Over (total={threshold})",
                            "odds": real_odds, "confidence": conf, "data_confidence": conf,
                            "verdict": _verdict(conf), "analysis_reasons": reasons,
                            "suggestion": None, "data_quality": "good",
                            "rating": "safe" if threshold <= 1.5 else "moderate",
                        })

            bt = scores["btts"]
            gg_odds = _get_gg_odds()
            if gg_odds > 1.0:
                checked = cross_check_with_odds(bt["confidence"], gg_odds)
                conf = checked["confidence"]
                reasons = bt["reasons"][:]
                if checked["warning"]:
                    reasons.append(checked["warning"])
                if conf >= 45:
                    league_scored.append({
                        **base_pick, "market": "GG/NG", "pick": "GG",
                        "odds": gg_odds, "confidence": conf, "data_confidence": conf,
                        "verdict": _verdict(conf), "analysis_reasons": reasons,
                        "suggestion": None, "data_quality": "good", "rating": "moderate",
                    })

            # Extended markets
            _add_extended_picks(
                league_scored, scores, markets, base_pick,
                "good", _verdict,
            )

            code_matches.add(match_key)
            scanned += 1

        except Exception as e:
            logger.warning(f"Check expand error for {home_name} vs {away_name}: {e}")

    if league_scored:
        all_scored.extend(league_scored)
        context.user_data["check_all_scored"] = all_scored
        code_count = len([p for p in all_scored if p.get("_source") == "booking_code"])
        league_count = len([p for p in all_scored if p.get("_source") == "league_scan"])
        await query.message.reply_text(
            f"📋 Pool expanded: {code_count} from codes + {league_count} from leagues ({scanned} matches scanned)"
        )
    else:
        await query.message.reply_text("Nothing strong enough from the league scan — sticking with your code games.")

    return await _show_check_target_odds(query.message, context, edit=False)


async def _show_check_target_odds(message, context, edit=False):
    """Display target odds selection buttons for /check."""
    all_scored = context.user_data.get("check_all_scored", [])
    has_league = any(p.get("_source") == "league_scan" for p in all_scored)
    source_note = "Best picks from BOTH sources will be combined:" if has_league else "Pick your target odds:"

    buttons = [
        [
            InlineKeyboardButton("3 odds", callback_data="check_odds_3"),
            InlineKeyboardButton("5 odds", callback_data="check_odds_5"),
            InlineKeyboardButton("10 odds", callback_data="check_odds_10"),
        ],
        [
            InlineKeyboardButton("15 odds", callback_data="check_odds_15"),
            InlineKeyboardButton("20 odds", callback_data="check_odds_20"),
            InlineKeyboardButton("All picks", callback_data="check_odds_all"),
        ],
    ]
    text = f"How many total odds do you want?\n{source_note}"

    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

    return CHECK_TARGET_ODDS


async def check_receive_odds_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds as text."""
    try:
        target = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 10) or /cancel.")
        return CHECK_TARGET_ODDS

    context.user_data["check_target"] = target
    return await _check_build_and_show(update.message, context)


async def check_receive_odds_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds from inline button."""
    query = update.callback_query
    await query.answer()

    data = query.data.replace("check_odds_", "")

    all_scored = context.user_data.get("check_all_scored", [])

    if data == "all":
        pending = [p for p in all_scored if p.get("match_status") != "Ended"]
        total = 1.0
        for p in pending:
            total *= p.get("odds", 1.0)
        target = total
    else:
        target = float(data)

    context.user_data["check_target"] = target
    return await _check_build_and_show(query.message, context)


async def _check_build_and_show(message, context):
    """Build combo using /pick's pipeline, then show with full review UI."""
    # Reuse _pick_build_combo by temporarily setting pick context
    all_scored = context.user_data.get("check_all_scored", [])
    target = context.user_data.get("check_target", 10)
    excluded = context.user_data.get("check_excluded", set())

    if not all_scored:
        await message.reply_text("No picks available. Start over with /check.")
        return ConversationHandler.END

    # Save to pick context keys so _pick_build_combo works
    context.user_data["pick_all_scored"] = all_scored
    context.user_data["pick_excluded"] = excluded
    context.user_data["pick_target"] = target
    context.user_data["pick_shuffle_seed"] = context.user_data.get("check_shuffle_seed", 0)
    context.user_data["_check_mode"] = True  # Flag to route back to check flow

    result = await _pick_build_combo(message, context)

    # Copy combo back to check context
    context.user_data["check_combo"] = context.user_data.get("pick_combo", [])

    return CHECK_REVIEW


async def check_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check review buttons — unified with /pick's UI."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "pick_explain_all":
        combo = context.user_data.get("pick_combo", [])
        if combo:
            await _explain_picks(query.message, context, combo, [])
        return CHECK_REVIEW

    # Route confirm to check's booking flow
    if data == "pick_confirm":
        combo = context.user_data.get("pick_combo", [])
        if not combo:
            await query.edit_message_text("No picks. Start over with /check.")
            return ConversationHandler.END

        # Book ALL picks (code + league) using /pick's booking logic
        total_odds = 1.0
        lines = ["✅ Final Selection:\n"]
        for i, p in enumerate(combo, 1):
            source_tag = " 📌" if p.get("_source") == "booking_code" else " 🔍"
            market_label = p["pick"].replace("(total=", "").replace(")", "") if "total=" in p.get("pick", "") else p.get("pick", "")
            lines.append(f"{i}. {p['home']} vs {p['away']}{source_tag}")
            lines.append(f"   {p['market']}: {market_label} @ {p['odds']:.2f}")
            total_odds *= p["odds"]
        lines.append(f"\nTotal Odds: {total_odds:.2f}")
        lines.append(f"Selections: {len(combo)}")

        await query.edit_message_text("\n".join(lines))

        # Use /pick's booking function — it handles ALL games with event_id
        result = await _pick_confirm_and_book(query.message, context)

        # Clean up check-specific context
        for key in ["check_all_scored", "check_codes", "check_combo", "check_target",
                     "check_excluded", "check_shuffle_seed", "_check_mode"]:
            context.user_data.pop(key, None)

        return result

    if data == "pick_reshuffle":
        seed = context.user_data.get("check_shuffle_seed", 0) + 1
        context.user_data["check_shuffle_seed"] = seed
        context.user_data["pick_shuffle_seed"] = seed
        await query.edit_message_text(f"🔄 Reshuffling (seed {seed})...")
        return await _check_build_and_show(query.message, context)

    if data.startswith("pick_exclude_"):
        idx = int(data.replace("pick_exclude_", ""))
        combo = context.user_data.get("pick_combo", [])
        if 0 <= idx < len(combo):
            removed = combo[idx]
            context.user_data.setdefault("check_excluded", set()).add(_pick_key(removed))
            context.user_data["pick_excluded"] = context.user_data["check_excluded"]
            await query.edit_message_text(f"❌ Excluded: {removed['home']} vs {removed['away']} ({removed.get('pick', '')})\nRebuilding...")
        return await _check_build_and_show(query.message, context)

    if data.startswith("pick_change_") or data.startswith("pick_mkt_") or data.startswith("pick_swap_"):
        # Delegate to pick's review handler, then route back to check
        result = await pick_review_callback(update, context)
        # The pick handler returns PICK_REVIEW, but we need CHECK_REVIEW
        return CHECK_REVIEW

    return CHECK_REVIEW


async def check_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle natural language during CHECK_REVIEW — uses same semantic layer as /pick."""
    text = update.message.text.strip()
    combo = context.user_data.get("pick_combo", [])
    if not combo:
        await update.message.reply_text("No active combo. Use /check to start.")
        return ConversationHandler.END

    # Route through semantic layer
    result = await asyncio.to_thread(
        gemini_chat.parse_review_message, text, len(combo)
    )

    intent = result.get("intent", "chat")
    params = result.get("params", {})
    reply = result.get("reply", "")

    # ── Book ──
    if intent == "book":
        if reply:
            await update.message.reply_text(reply)
        booking_result = await _pick_confirm_and_book(update.message, context)
        for key in ["check_all_scored", "check_codes", "check_combo", "check_target",
                     "check_excluded", "check_shuffle_seed", "_check_mode"]:
            context.user_data.pop(key, None)
        return booking_result

    # ── Reshuffle ──
    if intent == "reshuffle":
        seed = context.user_data.get("check_shuffle_seed", 0) + 1
        context.user_data["check_shuffle_seed"] = seed
        context.user_data["pick_shuffle_seed"] = seed
        await update.message.reply_text(reply or "🔄 Mixing it up...")
        return await _check_build_and_show(update.message, context)

    # ── Explain ──
    if intent == "explain":
        pick_nums = params.get("picks", [])
        return await _explain_picks(update.message, context, combo, pick_nums)

    # ── Edit ──
    if intent == "edit":
        actions = params.get("actions")
        if not actions:
            actions = await _parse_combo_edit(text, len(combo))

        if not actions:
            await update.message.reply_text(
                reply or "Not sure what to change. Try:\n"
                "• \"drop 1 and 3\"\n"
                "• \"make 2 an over 1.5\"\n"
                "• \"switch 4 to home win\""
            )
            return CHECK_REVIEW

        return await _apply_combo_edits(update.message, context, combo, actions)

    # ── Chat ──
    if reply:
        await update.message.reply_text(reply)
    else:
        await update.message.reply_text(
            "Still here! You can edit picks, ask me to explain them, "
            "reshuffle, or confirm to book."
        )
    return CHECK_REVIEW


async def check_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the check flow."""
    for key in ["check_all_scored", "check_codes", "check_combo", "check_target",
                 "check_excluded", "check_shuffle_seed", "_check_mode",
                 "check_picks"]:
        context.user_data.pop(key, None)
    await update.message.reply_text("Check cancelled.")
    return ConversationHandler.END


# ── Bot menu setup ───────────────────────────────────────────────────────────

async def post_init(application: Application):
    """Set the bot commands menu after startup."""
    commands = [
        BotCommand("start", "Welcome & help"),
        BotCommand("check", "Analyze SportyBet booking codes"),
        BotCommand("pick", "Get picks for target odds"),
        BotCommand("leagues", "Select leagues to analyze"),
        BotCommand("timeframe", "Set scan window (1-14 days)"),
        BotCommand("refresh", "Fetch fresh fixture data"),
        BotCommand("strategy", "Pick settings (confidence, markets, odds)"),
        BotCommand("settings", "Pick settings (alias for /strategy)"),
        BotCommand("budget", "Check API calls remaining"),
        BotCommand("status", "Current bot config"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot command menu registered.")

    # Pre-warm the SportyBet event index so cached picks can be booked immediately
    try:
        await asyncio.to_thread(build_event_index, force=False)
        logger.info("SportyBet event index pre-warmed on startup.")
    except Exception as e:
        logger.warning(f"Failed to pre-warm event index on startup: {e}")


# ── Gemini natural-language fallback ──────────────────────────────────────────

async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for free-text messages not caught by commands or conversations."""
    user_text = update.message.text
    if not user_text or not user_text.strip():
        return

    # Build context for Gemini
    chat_id = update.effective_chat.id if update.effective_chat else None
    config = load_user_config(chat_id=chat_id)
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config.get("leagues", [])]
    chat_context = {
        "active_leagues": league_names,
        "timeframe": config.get("timeframe", "7days"),
        "strategy": config.get("default_strategy", "balanced"),
    }

    try:
        result = await asyncio.to_thread(gemini_chat.chat, user_text, chat_context)
    except Exception as e:
        logger.warning(f"Gemini chat error: {e}")
        await update.message.reply_text(
            "Sorry, I couldn't process that. Try /pick, /check, or /leagues."
        )
        return ConversationHandler.END

    intent = result.get("intent", "chat")
    params = result.get("params", {})
    reply = result.get("reply", "")

    if intent == "pick":
        # Trigger the pick flow with extracted target odds
        target = params.get("target_odds")
        market_slots = params.get("market_slots")
        ticket_type = params.get("ticket_type")
        ticket_count = params.get("ticket_count")
        ticket_mode = params.get("ticket_mode")

        req = _ensure_pick_request(context)
        if ticket_type in ("single", "multiple"):
            req["ticket_type"] = ticket_type
        elif ticket_count or ticket_mode:
            req["ticket_type"] = "multiple"

        if ticket_count:
            try:
                req["ticket_count"] = max(2, min(5, int(ticket_count)))
            except (TypeError, ValueError):
                pass
        if ticket_mode in ("unique", "dynamic"):
            req["ticket_mode"] = ticket_mode
        if target and isinstance(target, (int, float)) and target > 1:
            req["target_odds"] = float(target)
            context.user_data["pick_target"] = float(target)
        context.user_data["pick_request"] = req

        if market_slots:
            context.user_data["pick_market_slots"] = market_slots
            logger.info(f"Market slots: {market_slots}")
        elif req.get("ticket_type") == "multiple":
            context.user_data.pop("pick_market_slots", None)

        await update.message.reply_text(reply)
        return await _continue_pick_request_flow(update.message, context)

    elif intent == "check":
        code = params.get("code")
        if code:
            await update.message.reply_text(reply)
            context.user_data["check_codes_raw"] = code
            await check_start(update, context)
        else:
            await update.message.reply_text(
                f"{reply}\n\nSend me a booking code or use /check."
            )
        return ConversationHandler.END

    elif intent == "leagues":
        await update.message.reply_text(reply)
        await cmd_leagues(update, context)
        return ConversationHandler.END

    elif intent == "timeframe":
        await update.message.reply_text(reply)
        await cmd_timeframe(update, context)
        return ConversationHandler.END

    elif intent == "stats":
        team = params.get("team", "").strip()
        if not team:
            await update.message.reply_text(
                "Which team? e.g. 'Arsenal form' or 'how is Chelsea doing?'"
            )
            return ConversationHandler.END

        await update.message.reply_text(f"Checking {team} form...")
        try:
            results = await asyncio.to_thread(get_team_results, team, count=10)
            if not results:
                await update.message.reply_text(
                    f"Could not find recent results for '{team}'. "
                    f"Try the full team name (e.g. 'Manchester United')."
                )
                return ConversationHandler.END

            form = _summarize_form(results)
            form_str = form.get("form_string", "")
            played = form.get("played", 0)
            wins = form.get("wins", 0)
            draws = form.get("draws", 0)
            losses = form.get("losses", 0)
            avg_scored = form.get("avg_scored", 0)
            avg_conceded = form.get("avg_conceded", 0)

            msg = (
                f"**{team}** — Last {played} matches\n\n"
                f"Form: {form_str} ← recent\n"
                f"W{wins} D{draws} L{losses}\n"
                f"Avg Goals Scored: {avg_scored:.1f}\n"
                f"Avg Goals Conceded: {avg_conceded:.1f}\n"
            )

            msg += "\nRecent:\n"
            for r in results[:5]:
                home = r.get("homeTeam", {}).get("name", "?")
                away = r.get("awayTeam", {}).get("name", "?")
                h_goals = r.get("score", {}).get("fullTime", {}).get("home", "?")
                a_goals = r.get("score", {}).get("fullTime", {}).get("away", "?")
                msg += f"  {home} {h_goals}-{a_goals} {away}\n"

            await send_long_message(update, msg)

        except Exception as e:
            logger.warning(f"Stats lookup error for {team}: {e}")
            await update.message.reply_text(
                f"Error looking up {team}: {e}\nTry the full team name."
            )
        return ConversationHandler.END

    else:
        # intent == "chat" or unknown
        await update.message.reply_text(reply)
        return ConversationHandler.END


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        print("1. Create a bot via @BotFather on Telegram")
        print("2. Add TELEGRAM_BOT_TOKEN=your_token to .env")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # /check conversation handler (must be added before plain CommandHandler)
    check_conv = ConversationHandler(
        entry_points=[CommandHandler("check", check_start)],
        states={
            CHECK_CODES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_receive_codes),
            ],
            CHECK_EXPAND: [
                CallbackQueryHandler(check_expand_callback, pattern=r"^check_expand_(yes|no)$"),
            ],
            CHECK_TARGET_ODDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_receive_odds_text),
                CallbackQueryHandler(check_receive_odds_button, pattern=r"^check_odds_"),
            ],
            CHECK_REVIEW: [
                CallbackQueryHandler(check_review_callback, pattern=r"^(pick_exclude_\d+|pick_swap_\d+|pick_change_\d+|pick_mkt_.+|pick_reshuffle|pick_confirm|pick_explain_all)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_edit_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", check_cancel)],
        allow_reentry=True,
    )
    app.add_handler(check_conv)

    # /pick conversation handler
    pick_conv = ConversationHandler(
        entry_points=[
            CommandHandler("pick", pick_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_language),
        ],
        states={
            PICK_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_receive_type_text),
                CallbackQueryHandler(pick_receive_type_button, pattern=r"^pick_type_"),
            ],
            PICK_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_receive_count_text),
                CallbackQueryHandler(pick_receive_count_button, pattern=r"^pick_count_"),
            ],
            PICK_ODDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_receive_odds_text),
                CallbackQueryHandler(pick_receive_odds_button, pattern=r"^pick_target_"),
            ],
            PICK_MODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_receive_mode_text),
                CallbackQueryHandler(pick_receive_mode_button, pattern=r"^pick_mode_"),
            ],
            PICK_REVIEW: [
                CallbackQueryHandler(pick_review_callback, pattern=r"^(pick_exclude_\d+|pick_swap_\d+|pick_change_\d+|pick_mkt_.+|pick_reshuffle|pick_confirm|pick_explain_all|pick_bundle_.+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_edit_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", pick_cancel)],
        allow_reentry=True,
    )
    app.add_handler(pick_conv)

    # Other commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("leagues", cmd_leagues))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("timeframe", cmd_timeframe))

    # Strategy/settings conversation handler
    strat_conv = ConversationHandler(
        entry_points=[
            CommandHandler("strategy", cmd_strategy),
            CommandHandler("settings", cmd_strategy),
        ],
        states={
            STRAT_CUSTOM_CONFIDENCE: [
                CallbackQueryHandler(callback_strat_custom_confidence, pattern=r"^strat_conf_"),
                CallbackQueryHandler(callback_strategy_select, pattern=r"^strat_(conf_menu|mkt_menu|odds_menu|back|done|conservative|balanced|aggressive|overs_only|btts_mix|favourites)$"),
            ],
            STRAT_CUSTOM_MARKETS: [
                CallbackQueryHandler(callback_strat_custom_markets, pattern=r"^strat_mkt_"),
                CallbackQueryHandler(callback_strategy_select, pattern=r"^strat_(back|done)$"),
            ],
            STRAT_CUSTOM_OVER: [
                CallbackQueryHandler(callback_strat_custom_over, pattern=r"^strat_"),
            ],
            STRAT_CUSTOM_MIN_ODDS: [
                CallbackQueryHandler(callback_strat_custom_min_odds, pattern=r"^strat_minodds_"),
                CallbackQueryHandler(callback_strategy_select, pattern=r"^strat_(back|done)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", strategy_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(strat_conv)

    # Callbacks (inline keyboard)
    app.add_handler(CallbackQueryHandler(
        callback_league_toggle, pattern=r"^league_"
    ))
    app.add_handler(CallbackQueryHandler(
        callback_strategy_select, pattern=r"^strat_(conservative|balanced|aggressive|overs_only|btts_mix|favourites|conf_menu|mkt_menu|odds_menu|back|done)$"
    ))
    app.add_handler(CallbackQueryHandler(
        callback_timeframe, pattern=r"^tf_"
    ))

    # Natural language is now handled as a pick_conv entry point
    # (it returns ConversationHandler.END for non-pick intents)

    runtime_mode = (os.getenv("BOT_MODE", "auto").strip().lower() or "auto")
    if runtime_mode not in {"auto", "polling", "webhook"}:
        runtime_mode = "auto"
    if runtime_mode == "auto":
        runtime_mode = "webhook" if os.getenv("WEBHOOK_URL", "").strip() else "polling"

    print("SportyBot Telegram bot starting...")
    print(f"Runtime mode: {runtime_mode}")
    print(f"API Budget: {get_api_budget()['remaining']} calls remaining")

    if runtime_mode == "webhook":
        raw_webhook_url = os.getenv("WEBHOOK_URL", "").strip()
        if not raw_webhook_url:
            print("Error: WEBHOOK_URL is required when BOT_MODE=webhook")
            sys.exit(1)

        parsed = urlparse(raw_webhook_url)
        if not parsed.scheme or not parsed.netloc:
            print("Error: WEBHOOK_URL must be a full URL like https://your-app.example.com/telegram")
            sys.exit(1)

        try:
            port = int(os.getenv("PORT", "8080") or 8080)
        except ValueError:
            print("Error: PORT must be an integer")
            sys.exit(1)

        listen = os.getenv("LISTEN", "0.0.0.0").strip() or "0.0.0.0"
        url_path = parsed.path.lstrip("/") or "telegram"
        webhook_url = raw_webhook_url.rstrip("/")
        if parsed.path in ("", "/"):
            webhook_url = f"{webhook_url}/{url_path}"
        secret_token = os.getenv("WEBHOOK_SECRET", "").strip() or None

        print(f"Webhook listen: {listen}:{port}")
        print(f"Webhook URL: {webhook_url}")
        app.run_webhook(
            listen=listen,
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            allowed_updates=Update.ALL_TYPES,
        )
        return

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
