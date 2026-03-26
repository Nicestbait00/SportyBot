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
CHECK_CODES, CHECK_TARGET_ODDS, CHECK_EXCLUDE, CHECK_CONFIRM, CHECK_EXPAND = range(5)
# Conversation states for /pick flow
PICK_ODDS, PICK_REVIEW = range(10, 12)
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


# ── Strategy helpers ──────────────────────────────────────────────────────────

def _get_strategy_config(user_config: dict) -> dict:
    """Return the effective strategy dict for a user config.

    If the user chose a preset (string), look it up in STRATEGY_PRESETS.
    If the user built a custom strategy (dict), return it directly.
    Falls back to the 'balanced' preset.
    """
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return strat
    return STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])


def _get_strategy_label(user_config: dict) -> str:
    """Human-readable label for the user's current strategy."""
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return strat.get("label", "Custom")
    preset = STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])
    return preset["label"]


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and quick setup."""
    from config import TIMEFRAME_PRESETS
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config["leagues"]]
    tf_label = TIMEFRAME_PRESETS.get(config.get("timeframe", "7days"), {}).get("label", "7 days")

    msg = (
        "Welcome to SportyBot\n\n"
        "I analyze football matches and build betting combos with auto-booking on SportyBet.\n\n"
        "Just chat naturally:\n"
        "  \"Give me 20 odds\"\n"
        "  \"3 wins, 5 over 1.5 for 30 odds\"\n"
        "  \"Arsenal form\"\n\n"
        "After picks show, you can say:\n"
        "  \"remove 1, 3\" — drop games\n"
        "  \"change 5 to over 2.5\" — swap markets\n"
        "  \"book it\" — generate SportyBet code\n\n"
        "Commands:\n"
        "/pick — Get picks\n"
        "/check — Analyze a booking code\n"
        "/leagues — Toggle leagues\n"
        "/timeframe — Set time window\n"
        "/strategy — Set betting strategy\n"
        "/status — Current config\n\n"
        f"Leagues: {', '.join(league_names) if league_names else 'All'}\n"
        f"Timeframe: {tf_label}\n"
        f"Strategy: {_get_strategy_label(config)}"
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

    buttons = []
    row = []
    for lid, name in sorted(LEAGUE_NAMES.items(), key=lambda x: x[1]):
        check = "✅" if lid in active else "⬜"
        row.append(InlineKeyboardButton(
            f"{check} {name}", callback_data=f"league_toggle_{lid}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("✅ Done", callback_data="league_done")])

    await update.message.reply_text(
        "Select leagues to analyze:\n(Tap to toggle on/off)",
        reply_markup=InlineKeyboardMarkup(buttons),
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
        await query.edit_message_text(
            f"Leagues set: {', '.join(names) or 'None'}\n\n"
            "Now run /refresh to cache data, then /pick to get selections.",
        )
        return

    lid = int(data.replace("league_toggle_", ""))
    config = load_user_config(chat_id=chat_id)
    leagues = config.get("leagues", [])

    if lid in leagues:
        leagues.remove(lid)
    else:
        leagues.append(lid)

    config["leagues"] = leagues
    save_user_config(config, chat_id=chat_id)

    active = set(leagues)
    buttons = []
    row = []
    for league_id, name in sorted(LEAGUE_NAMES.items(), key=lambda x: x[1]):
        check = "✅" if league_id in active else "⬜"
        row.append(InlineKeyboardButton(
            f"{check} {name}", callback_data=f"league_toggle_{league_id}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("✅ Done", callback_data="league_done")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))


async def pick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start interactive pick flow. If odds given inline, skip to analysis."""
    context.user_data["chat_id"] = update.effective_chat.id
    args = context.args or []

    if args:
        try:
            target = float(args[0])
            context.user_data["pick_target"] = target
            return await _pick_analyze(update, context, target)
        except ValueError:
            pass

    # Ask for target odds
    buttons = [
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
    ]
    await update.message.reply_text(
        "🎯 What total odds are you targeting?\n"
        "Pick below or type a custom number:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return PICK_ODDS


async def pick_receive_odds_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds as text."""
    try:
        target = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 10) or /cancel.")
        return PICK_ODDS
    context.user_data["pick_target"] = target
    return await _pick_analyze(update, context, target)


async def pick_receive_odds_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds from button."""
    query = update.callback_query
    await query.answer()
    target = float(query.data.replace("pick_target_", ""))
    context.user_data["pick_target"] = target
    await query.edit_message_text(f"🎯 Target: {target:.0f} odds. Scanning...")
    # We need to use query.message for replies from here
    context.user_data["_pick_msg"] = query.message
    return await _pick_analyze_from_message(query.message, context, target)


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


async def _pick_build_combo(message, context):
    """Build best combo from scored picks, respecting exclusions and strategy."""
    import random

    all_scored = context.user_data.get("pick_all_scored", [])
    excluded = context.user_data.get("pick_excluded", set())
    target = context.user_data.get("pick_target", 10)
    shuffle_seed = context.user_data.get("pick_shuffle_seed", 0)

    # Load user strategy
    chat_id = context.user_data.get("chat_id")
    config = load_user_config(chat_id=chat_id)
    strat = _get_strategy_config(config)
    min_confidence = strat.get("min_confidence", 55)
    preferred_markets = strat.get("preferred_markets", ["1X2", "Over/Under", "GG/NG"])
    over_threshold = strat.get("over_threshold", 1.5)

    # Filter out excluded and odds-only picks (no real form data)
    available = [
        p for p in all_scored
        if _pick_key(p) not in excluded and p.get("data_quality") != "limited"
    ]

    # Check for market_slots (natural language structured request)
    market_slots = context.user_data.get("pick_market_slots")

    if market_slots:
        # Natural language request — skip strategy filters entirely
        # The user explicitly told us what they want
        pass
    else:
        # Apply strategy market filter
        def _market_allowed(p):
            market = p.get("market", "")
            if market not in preferred_markets and market not in ("1X2", "Over/Under", "GG/NG"):
                return False
            return True
        available = [p for p in available if _market_allowed(p)]

    # Sort by confidence, with slight randomness on reshuffle
    if shuffle_seed > 0:
        rng = random.Random(shuffle_seed)
        available.sort(key=lambda p: p["confidence"] + rng.randint(-8, 8), reverse=True)
    else:
        available.sort(key=lambda p: p["confidence"], reverse=True)

    # Build combo: filter by strategy thresholds only when no market_slots
    if market_slots:
        # Trust the user's explicit request — only filter out bad data
        qualified = [
            p for p in available
            if p.get("data_quality") != "limited"
            and p.get("odds", 0) > 1.0
        ]
    else:
        min_pick_odds = strat.get("min_odds", 1.10)
        qualified = [
            p for p in available
            if p["confidence"] >= min_confidence
            and p.get("data_quality") != "limited"
            and p.get("odds", 0) >= min_pick_odds
        ]

    selected = []
    used_matches = set()
    current_odds = 1.0

    # Check for market_slots (structured distribution request)
    market_slots = context.user_data.get("pick_market_slots")

    if market_slots:
        # Phase 1: Fill each fixed slot (those with "count")
        for slot in market_slots:
            if slot.get("fill"):
                continue  # Handle fill slots last
            slot_market = slot["market"]
            slot_count = slot.get("count", 1)
            slot_threshold = slot.get("threshold")

            # Filter qualified picks for this slot
            slot_picks = [
                p for p in qualified
                if p["market"] == slot_market
                and f"{p['home']}_{p['away']}" not in used_matches
            ]

            # For Over/Under, filter by threshold
            if slot_market == "Over/Under" and slot_threshold is not None:
                slot_picks = [
                    p for p in slot_picks
                    if _pick_threshold(p) == slot_threshold
                ]

            # Sort by confidence
            slot_picks.sort(key=lambda p: p["confidence"], reverse=True)

            for p in slot_picks[:slot_count]:
                match_key = f"{p['home']}_{p['away']}"
                selected.append(p)
                used_matches.add(match_key)
                current_odds *= p["odds"]

        # Phase 2: Fill remaining with "fill" slot picks until target reached
        fill_slots = [s for s in market_slots if s.get("fill")]
        if fill_slots and current_odds < target:
            fs = fill_slots[0]
            fill_market = fs["market"]
            fill_threshold = fs.get("threshold")

            fill_picks = [
                p for p in qualified
                if p["market"] == fill_market
                and f"{p['home']}_{p['away']}" not in used_matches
            ]

            if fill_market == "Over/Under" and fill_threshold is not None:
                fill_picks = [
                    p for p in fill_picks
                    if _pick_threshold(p) == fill_threshold
                ]

            fill_picks.sort(key=lambda p: p["confidence"], reverse=True)

            for p in fill_picks:
                if current_odds >= target:
                    break
                match_key = f"{p['home']}_{p['away']}"
                if match_key in used_matches:
                    continue
                selected.append(p)
                used_matches.add(match_key)
                current_odds *= p["odds"]

        # Phase 3: If still under target, add any high-confidence pick
        if current_odds < target:
            remaining = [
                p for p in qualified
                if f"{p['home']}_{p['away']}" not in used_matches
            ]
            remaining.sort(key=lambda p: p["confidence"], reverse=True)
            for p in remaining:
                if current_odds >= target:
                    break
                match_key = f"{p['home']}_{p['away']}"
                selected.append(p)
                used_matches.add(match_key)
                current_odds *= p["odds"]
    else:
        # Default: greedy combo with league diversity check
        league_counts = {}
        max_per_league = max(3, len(qualified) // 5)  # At most ~1/5 of picks from same league

        for p in qualified:
            if current_odds >= target:
                break
            match_key = f"{p['home']}_{p['away']}"
            if match_key in used_matches:
                continue

            # Limit concentration from any single league
            p_league = p.get("league", "")
            if league_counts.get(p_league, 0) >= max_per_league:
                continue

            selected.append(p)
            used_matches.add(match_key)
            current_odds *= p["odds"]
            league_counts[p_league] = league_counts.get(p_league, 0) + 1

    if not selected:
        await message.reply_text(
            "Not enough confident picks for this target.\n"
            "Try /timeframe to widen the window or /leagues to add more leagues."
        )
        return ConversationHandler.END

    # Store combo
    context.user_data["pick_combo"] = selected

    # Format header with active leagues + timeframe
    from config import LEAGUE_NAMES as _LN, TIMEFRAME_PRESETS as _TP
    config = load_user_config(chat_id=chat_id)
    _leagues = config.get("leagues", [])
    _tf = config.get("timeframe", "7days")
    _league_str = ", ".join(_LN.get(lid, str(lid)) for lid in _leagues) if _leagues else "All"
    _tf_str = _TP.get(_tf, {}).get("label", _tf)

    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠"}
    lines = [f"🎯 Picks for ~{target:.0f} odds | {_league_str} | {_tf_str}\n"]

    # Sort selected by kickoff date for grouped display
    from datetime import datetime as _dt

    def _parse_date(d):
        try:
            return _dt.strptime(d, "%Y-%m-%d %H:%M")
        except Exception:
            return _dt.max

    selected.sort(key=lambda p: _parse_date(p.get("date", "")))

    # Group by date and show headers
    current_day = None
    for i, p in enumerate(selected, 1):
        pick_date = p.get("date", "")
        try:
            dt = _dt.strptime(pick_date, "%Y-%m-%d %H:%M")
            day_str = dt.strftime("%a %d %b")
            time_str = dt.strftime("%H:%M")
        except Exception:
            day_str = ""
            time_str = pick_date

        # Insert day header when date changes
        if day_str and day_str != current_day:
            current_day = day_str
            lines.append(f"── {day_str} ──")

        icon = verdict_icons.get(p.get("verdict", ""), "❓")
        market_label = p["pick"].replace("(total=", "").replace(")", "") if "total=" in p["pick"] else p["pick"]
        conf = p.get('data_confidence', p['confidence'])
        # Margin: ±5 for good data, ±10 for limited, ±15 for odds-only
        dq = p.get("data_quality", "good")
        margin = 5 if dq == "good" else 10 if dq == "fair" else 15
        conf_low = max(0, conf - margin)
        conf_high = min(95, conf + margin)
        lines.append(
            f"{icon} {i}. {p['home']} vs {p['away']}\n"
            f"   [{p['league']}] {time_str}\n"
            f"   {p['market']}: {market_label} @ ~{p['odds']:.2f} [{conf_low}-{conf_high}%]"
        )
        for r in p.get("analysis_reasons", [])[:1]:
            lines.append(f"   > {r}")
        if p.get("suggestion"):
            lines.append(f"   💡 Swap to: {p['suggestion']['market']} ({p['suggestion']['confidence']}%)")
        lines.append("")

    avg_conf = int(sum(p.get("data_confidence", p["confidence"]) for p in selected) / len(selected))
    lines.append(f"Total Odds: ~{current_odds:.2f}")
    lines.append(f"Selections: {len(selected)}")
    lines.append(f"Avg Confidence: {avg_conf}%")

    # Show kickoff spread
    dates = [_parse_date(p.get("date", "")) for p in selected if _parse_date(p.get("date", "")) != _dt.max]
    if len(dates) >= 2:
        span = (max(dates) - min(dates)).days
        if span == 0:
            lines.append(f"All games kick off the same day")
        else:
            lines.append(f"Spread across {span + 1} days ({min(dates).strftime('%d %b')} → {max(dates).strftime('%d %b')})")

    if current_odds < target * 0.7:
        lines.append(f"\n⚠️ Under target ({current_odds:.1f} vs {target:.0f})")

    # Odds freshness timestamp
    lines.append(f"\nOdds fetched: {_dt.now().strftime('%H:%M %d %b')} — book soon, odds move")

    msg = "\n".join(lines)

    # Exclude + change market + reshuffle + confirm buttons
    buttons = []
    for i, p in enumerate(selected):
        row = [
            InlineKeyboardButton(f"❌ {i+1}", callback_data=f"pick_exclude_{i}"),
            InlineKeyboardButton(f"🔄 {i+1}", callback_data=f"pick_change_{i}"),
        ]
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("🔄 Reshuffle", callback_data="pick_reshuffle"),
        InlineKeyboardButton("✅ Keep these", callback_data="pick_confirm"),
    ])

    msg += "\n\nExclude, reshuffle, or confirm:"
    await message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

    return PICK_REVIEW


async def _pick_confirm_and_book(message, context):
    """Shared booking logic — called from button confirm or NL 'book it'."""
    combo = context.user_data.get("pick_combo", [])
    if not combo:
        await message.reply_text("No picks to book. Use /pick to start.")
        return ConversationHandler.END

    await message.reply_text("📲 Booking on SportyBet...")

    total_odds = 1.0
    for p in combo:
        total_odds *= p["odds"]

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

    if booking_selections:
        code = await asyncio.to_thread(create_booking_code, booking_selections)
        if code:
            await message.reply_text(
                f"🎫 *SportyBet Booking Code:* `{code}`\n\n"
                f"Tap the code to copy, then:\n"
                f"SportyBet App → Betslip → Load Booking Code → Paste\n\n"
                f"Selections: {len(booking_selections)} | Total Odds: ~{total_odds:.2f}",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "⚠️ Booking code generation failed.\n"
                "SportyBet API might be temporarily unavailable.\n"
                "You can manually select the games above."
            )
    else:
        await message.reply_text(
            "⚠️ Could not book — no valid selections.\n"
            + ("\n".join(failed_bookings) if failed_bookings else "")
        )

    if failed_bookings and booking_selections:
        await message.reply_text(
            "⚠️ Some picks couldn't be booked:\n" + "\n".join(failed_bookings)
        )

    # Clean up
    for key in ["pick_all_scored", "pick_combo", "pick_target", "pick_excluded", "pick_shuffle_seed", "_pick_msg", "pick_market_slots"]:
        context.user_data.pop(key, None)
    return ConversationHandler.END


async def pick_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle exclude/reshuffle/confirm buttons in pick flow."""
    query = update.callback_query
    await query.answer()
    data = query.data

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
        combo = context.user_data.get("pick_combo", [])
        if 0 <= idx < len(combo):
            pick = combo[idx]
            suggestion = pick.get("suggestion")
            if suggestion:
                old_desc = f"{pick['market']}: {pick['pick']}"
                # Apply the suggestion — update market and pick, keep the same match
                pick["market"] = suggestion["market"]
                pick["pick"] = suggestion["market"]  # suggestion market IS the pick name
                pick["confidence"] = suggestion["confidence"]
                pick["data_confidence"] = suggestion["confidence"]
                pick["verdict"] = _confidence_verdict(suggestion["confidence"])
                pick["suggestion"] = None  # clear after swap
                # Odds aren't provided by suggestion; estimate lower for safer picks
                if pick["odds"] > 1.5:
                    pick["odds"] = max(1.10, pick["odds"] * 0.75)
                await query.edit_message_text(
                    f"💡 Swapped #{idx+1}: {old_desc}\n"
                    f"   → {pick['market']}: {pick['pick']} @ ~{pick['odds']:.2f}\nRebuilding..."
                )
            else:
                await query.edit_message_text("No suggestion available for this pick.\nRebuilding...")
        return await _pick_build_combo(query.message, context)

    elif data.startswith("pick_change_"):
        idx = int(data.replace("pick_change_", ""))
        combo = context.user_data.get("pick_combo", [])
        if 0 <= idx < len(combo):
            pick = combo[idx]
            context.user_data["pick_change_idx"] = idx

            # Find ALL scored alternatives for this same match from the analysis
            all_scored = context.user_data.get("pick_all_scored", [])
            match_key = f"{pick['home']}_{pick['away']}"
            current_pick_key = _pick_key(pick)

            match_alts = [
                p for p in all_scored
                if f"{p['home']}_{p['away']}" == match_key
                and _pick_key(p) != current_pick_key
                and p.get("odds", 0) > 1.0
            ]

            # Sort by real scorer confidence (not implied odds), take top 4
            match_alts.sort(key=lambda p: p.get("confidence", 0), reverse=True)
            match_alts = match_alts[:4]

            lines = [f"🔄 *{pick['home']} vs {pick['away']}*\n"]
            lines.append(f"Current: {pick['market']}: {pick['pick']} @ {pick['odds']:.2f} [{pick.get('confidence', '?')}%]\n")
            lines.append("Best alternatives by analysis:")

            # Get SportyBet event for callback data mapping
            sporty_event = pick.get("_sporty_event")
            if not sporty_event:
                sporty_event = await asyncio.to_thread(find_event, pick["home"], pick["away"])

            buttons = []
            for alt in match_alts:
                conf = alt.get("confidence", 0)
                odds = alt.get("odds", 0)
                market = alt.get("market", "")
                pick_str = alt.get("pick", "")

                # Build a readable label
                if market == "Over/Under":
                    t = _pick_threshold(alt)
                    label = f"Over {t}" if t else pick_str
                elif market == "1X2":
                    label = f"{pick_str} Win" if pick_str in ("Home", "Away") else pick_str
                elif market == "GG/NG":
                    label = "BTTS Yes" if pick_str == "GG" else pick_str
                else:
                    label = f"{market}: {pick_str}"

                display = f"{label} @ {odds:.2f} [{conf}%]"
                lines.append(f"  {display}")

                # Map to callback data format
                if market == "1X2":
                    oid = "1" if pick_str == "Home" else "3"
                    cb = f"pick_mkt_{idx}_1x2_{oid}"
                elif market == "Over/Under":
                    t = _pick_threshold(alt) or "1.5"
                    cb = f"pick_mkt_{idx}_ou_{t}"
                elif market == "GG/NG":
                    cb = f"pick_mkt_{idx}_gg_"
                else:
                    continue

                # Top reason from analysis
                reasons = alt.get("analysis_reasons", [])
                if reasons:
                    lines.append(f"    ↳ {reasons[0]}")

                buttons.append([InlineKeyboardButton(display, callback_data=cb)])

            if not buttons:
                await query.edit_message_text("No alternative markets found for this match.\nRebuilding...")
                return await _pick_build_combo(query.message, context)

            buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="pick_mkt_back")])

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        return PICK_REVIEW

    elif data.startswith("pick_mkt_"):
        # Handle market selection for a specific pick
        if data == "pick_mkt_back":
            return await _pick_build_combo(query.message, context)

        combo = context.user_data.get("pick_combo", [])
        idx = context.user_data.get("pick_change_idx", -1)
        if idx < 0 or idx >= len(combo):
            return await _pick_build_combo(query.message, context)

        pick = combo[idx]
        sporty_event = pick.get("_sporty_event")
        if not sporty_event:
            sporty_event = await asyncio.to_thread(find_event, pick["home"], pick["away"])

        markets = sporty_event.get("markets", {}) if sporty_event else {}

        # Parse the callback: pick_mkt_{idx}_{type}_{extra}
        parts = data.split("_")
        # parts: ['pick', 'mkt', idx, type, ...]
        mkt_type = parts[3] if len(parts) > 3 else ""

        new_market = pick["market"]
        new_pick = pick["pick"]
        new_odds = pick["odds"]

        if mkt_type == "1x2":
            oid = parts[4] if len(parts) > 4 else ""
            m = markets.get("1", {})
            o = m.get("outcomes", {}).get(oid, {})
            name_map = {"1": "Home", "2": "Draw", "3": "Away"}
            new_market = "1X2"
            new_pick = name_map.get(oid, o.get("name", "Home"))
            new_odds = float(o.get("odds", pick["odds"]))

        elif mkt_type == "ou":
            threshold = parts[4] if len(parts) > 4 else "2.5"
            m = markets.get(f"18|total={threshold}", {})
            o = m.get("outcomes", {}).get("12", {})
            new_market = "Over/Under"
            new_pick = f"Over (total={threshold})"
            new_odds = float(o.get("odds", pick["odds"]))

        elif mkt_type == "gg":
            m = markets.get("29", {})
            o = m.get("outcomes", {}).get("74", {})
            new_market = "GG/NG"
            new_pick = "GG"
            new_odds = float(o.get("odds", pick["odds"]))

        elif mkt_type == "dc":
            oid = parts[4] if len(parts) > 4 else ""
            m = markets.get("10", {})
            o = m.get("outcomes", {}).get(oid, {})
            new_market = "Double Chance"
            new_pick = o.get("name", "")
            new_odds = float(o.get("odds", pick["odds"]))

        # Update the pick in-place
        old_desc = f"{pick['market']}: {pick['pick']} @ {pick['odds']:.2f}"
        pick["market"] = new_market
        pick["pick"] = new_pick
        pick["odds"] = new_odds

        await query.edit_message_text(
            f"🔄 Changed #{idx+1}: {old_desc}\n"
            f"   → {new_market}: {new_pick} @ {new_odds:.2f}\nRebuilding..."
        )
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
    """Handle natural language edits to the current combo during PICK_REVIEW.

    Examples:
      "remove games 1, 2 and 5"
      "change game 7 to over 1.5"
      "swap 3 to home win and 9 to btts"
    """
    text = update.message.text.strip()
    text_lower = text.lower()
    combo = context.user_data.get("pick_combo", [])
    if not combo:
        await update.message.reply_text("No active combo. Use /pick to start.")
        return ConversationHandler.END

    # Check for confirm/book intent
    book_phrases = ("book it", "book", "confirm", "yes", "keep", "lock it", "lock", "go ahead", "place it", "bet", "lets go", "let's go", "do it")
    if text_lower in book_phrases or any(text_lower.startswith(p) for p in ("book ", "confirm ", "yes ")):
        # Simulate the confirm button press
        return await _pick_confirm_and_book(update.message, context)

    # Parse edit instructions — try Gemini first, fallback to regex
    actions = await _parse_combo_edit(text, len(combo))

    if not actions:
        await update.message.reply_text(
            "Couldn't understand that edit. Try:\n"
            "• \"remove 1, 3, 5\"\n"
            "• \"change 2 to over 1.5\"\n"
            "• \"swap 4 to home win\""
        )
        return PICK_REVIEW

    all_scored = context.user_data.get("pick_all_scored", [])
    excluded = context.user_data.get("pick_excluded", set())
    changes_made = []

    for action in actions:
        idx = action.get("index", 0) - 1  # User uses 1-based
        if idx < 0 or idx >= len(combo):
            continue

        if action["type"] == "remove":
            excluded.add(_pick_key(combo[idx]))
            changes_made.append(f"❌ Removed #{idx+1}: {combo[idx]['home']} vs {combo[idx]['away']}")

        elif action["type"] == "change":
            target_market = action.get("market", "")
            target_threshold = action.get("threshold")
            match_key = f"{combo[idx]['home']}_{combo[idx]['away']}"

            # Find the best alternative matching the requested market
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

            if replacement:
                old = f"{combo[idx]['market']}: {combo[idx]['pick']}"
                combo[idx] = replacement
                changes_made.append(
                    f"🔄 #{idx+1}: {old} → {replacement['market']}: {replacement['pick']} "
                    f"@ {replacement['odds']:.2f} [{replacement['confidence']}%]"
                )
            else:
                changes_made.append(f"⚠️ #{idx+1}: couldn't find {target_market} for that match")

    context.user_data["pick_excluded"] = excluded

    if changes_made:
        await update.message.reply_text("\n".join(changes_made) + "\n\nRebuilding...")

    return await _pick_build_combo(update.message, context)


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
    for key in ["pick_all_scored", "pick_combo", "pick_target", "pick_excluded", "pick_shuffle_seed", "_pick_msg"]:
        context.user_data.pop(key, None)
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
    config = load_user_config(chat_id=update.effective_chat.id)
    budget = await asyncio.to_thread(get_api_budget)
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config.get("leagues", [])]

    strat_cfg = _get_strategy_config(config)
    strat_label = _get_strategy_label(config)
    strat_detail = (
        f"  Min confidence: {strat_cfg['min_confidence']}%\n"
        f"  Markets: {', '.join(strat_cfg['preferred_markets'])}\n"
        f"  Over threshold: {strat_cfg['over_threshold']}"
    )

    msg = (
        f"SportyBot Status\n\n"
        f"Leagues: {', '.join(league_names) or 'None'}\n"
        f"Strategy: {strat_label}\n{strat_detail}\n"
        f"Timeframe: {config.get('days_ahead', 7)} days\n"
        f"API Budget: {budget['remaining']}/{budget['limit']} remaining\n"
        f"Date: {budget['date']}"
    )
    await update.message.reply_text(msg)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show strategy selection with inline buttons."""
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    config = load_user_config(chat_id=chat_id)
    current_label = _get_strategy_label(config)
    strat_cfg = _get_strategy_config(config)

    lines = [
        f"Current strategy: *{current_label}*",
        f"  Min confidence: {strat_cfg['min_confidence']}%",
        f"  Min odds/pick: {strat_cfg.get('min_odds', 1.10)}",
        f"  Markets: {', '.join(strat_cfg['preferred_markets'])}",
        f"  Over threshold: {strat_cfg['over_threshold']}",
        "",
    ]

    for key, preset in STRATEGY_PRESETS.items():
        lines.append(f"*{preset['label']}* — {preset['description']}")

    lines.append("\nSelect a preset or build your own:")

    buttons = [
        [
            InlineKeyboardButton("Conservative", callback_data="strat_conservative"),
            InlineKeyboardButton("Balanced", callback_data="strat_balanced"),
        ],
        [
            InlineKeyboardButton("Aggressive", callback_data="strat_aggressive"),
            InlineKeyboardButton("Overs Only", callback_data="strat_overs_only"),
        ],
        [
            InlineKeyboardButton("BTTS Mix", callback_data="strat_btts_mix"),
            InlineKeyboardButton("Favourites", callback_data="strat_favourites"),
        ],
        [
            InlineKeyboardButton("Custom...", callback_data="strat_custom"),
            InlineKeyboardButton("Edit current", callback_data="strat_edit"),
        ],
    ]
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE  # Reuse this state for initial selection too


async def callback_strategy_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle strategy preset button press."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_custom", "strat_edit"):
        # Start custom strategy conversation
        # If editing, pre-load current strategy values
        if data == "strat_edit":
            chat_id = update.effective_chat.id
            config = load_user_config(chat_id=chat_id)
            current = _get_strategy_config(config)
            context.user_data["custom_strategy"] = {
                "min_confidence": current.get("min_confidence", 55),
                "preferred_markets": list(current.get("preferred_markets", ["1X2", "Over/Under", "GG/NG"])),
                "over_threshold": current.get("over_threshold", 1.5),
                "min_odds": current.get("min_odds", 1.20),
            }
            label = f"Editing: {_get_strategy_label(config)}\n\n"
        else:
            context.user_data["custom_strategy"] = {}
            label = ""

        buttons = [
            [
                InlineKeyboardButton("45%", callback_data="strat_conf_45"),
                InlineKeyboardButton("55%", callback_data="strat_conf_55"),
            ],
            [
                InlineKeyboardButton("65%", callback_data="strat_conf_65"),
                InlineKeyboardButton("75%", callback_data="strat_conf_75"),
            ],
        ]
        await query.edit_message_text(
            f"{label}Custom Strategy — Step 1/4\n\n"
            "Minimum confidence for picks:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_CONFIDENCE

    # Preset selected
    preset_key = data.replace("strat_", "")
    preset = STRATEGY_PRESETS.get(preset_key)
    if not preset:
        await query.edit_message_text("Unknown strategy. Try /strategy again.")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["strategy"] = preset_key
    save_user_config(config, chat_id=chat_id)

    await query.edit_message_text(
        f"Strategy set to: {preset['label']}\n"
        f"{preset['description']}\n\n"
        f"Min confidence: {preset['min_confidence']}%\n"
        f"Markets: {', '.join(preset['preferred_markets'])}\n"
        f"Over threshold: {preset['over_threshold']}\n\n"
        f"Run /pick to get selections."
    )
    return ConversationHandler.END


async def callback_strat_custom_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom strategy confidence selection."""
    query = update.callback_query
    await query.answer()

    conf = int(query.data.replace("strat_conf_", ""))
    context.user_data["custom_strategy"]["min_confidence"] = conf

    # Step 2: Market selection (toggleable)
    context.user_data["custom_strategy"]["preferred_markets"] = ["1X2", "Over/Under", "GG/NG"]

    all_markets = ["1X2", "Over/Under", "GG/NG"]
    active = set(context.user_data["custom_strategy"]["preferred_markets"])

    buttons = []
    for m in all_markets:
        check = "on" if m in active else "off"
        buttons.append([InlineKeyboardButton(
            f"{'✅' if m in active else '⬜'} {m}",
            callback_data=f"strat_mkt_{check}_{m}",
        )])
    buttons.append([InlineKeyboardButton("Done", callback_data="strat_mkt_done")])

    await query.edit_message_text(
        f"Custom Strategy — Step 2/3\n"
        f"Min confidence: {conf}%\n\n"
        "Toggle markets to include:\n(Tap to toggle on/off)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return STRAT_CUSTOM_MARKETS


async def callback_strat_custom_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom strategy market toggle."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "strat_mkt_done":
        # Proceed to step 3: over threshold
        custom = context.user_data.get("custom_strategy", {})
        markets = custom.get("preferred_markets", [])
        if not markets:
            markets = ["1X2", "Over/Under", "GG/NG"]
            custom["preferred_markets"] = markets

        buttons = [
            [
                InlineKeyboardButton("Over 0.5", callback_data="strat_over_0.5"),
                InlineKeyboardButton("Over 1.5", callback_data="strat_over_1.5"),
                InlineKeyboardButton("Over 2.5", callback_data="strat_over_2.5"),
            ],
        ]
        await query.edit_message_text(
            f"Custom Strategy — Step 3/3\n"
            f"Min confidence: {custom['min_confidence']}%\n"
            f"Markets: {', '.join(markets)}\n\n"
            "Over threshold (minimum goal line for Over picks):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_OVER

    # Toggle a market
    # data format: strat_mkt_{on|off}_{market_name}
    parts = data.replace("strat_mkt_", "").split("_", 1)
    current_state = parts[0]  # "on" or "off"
    market_name = parts[1]

    custom = context.user_data.get("custom_strategy", {})
    markets = custom.get("preferred_markets", ["1X2", "Over/Under", "GG/NG"])

    if current_state == "on" and market_name in markets:
        markets.remove(market_name)
    elif current_state == "off" and market_name not in markets:
        markets.append(market_name)

    custom["preferred_markets"] = markets
    context.user_data["custom_strategy"] = custom

    # Rebuild buttons
    all_markets = ["1X2", "Over/Under", "GG/NG"]
    active = set(markets)
    buttons = []
    for m in all_markets:
        check = "on" if m in active else "off"
        buttons.append([InlineKeyboardButton(
            f"{'✅' if m in active else '⬜'} {m}",
            callback_data=f"strat_mkt_{check}_{m}",
        )])
    buttons.append([InlineKeyboardButton("Done", callback_data="strat_mkt_done")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    return STRAT_CUSTOM_MARKETS


async def callback_strat_custom_over(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom strategy over threshold selection — proceed to min odds."""
    query = update.callback_query
    await query.answer()

    threshold = float(query.data.replace("strat_over_", ""))
    custom = context.user_data.get("custom_strategy", {})
    custom["over_threshold"] = threshold

    buttons = [
        [
            InlineKeyboardButton("1.10", callback_data="strat_minodds_1.10"),
            InlineKeyboardButton("1.20", callback_data="strat_minodds_1.20"),
            InlineKeyboardButton("1.30", callback_data="strat_minodds_1.30"),
        ],
        [
            InlineKeyboardButton("1.40", callback_data="strat_minodds_1.40"),
            InlineKeyboardButton("1.60", callback_data="strat_minodds_1.60"),
            InlineKeyboardButton("1.80", callback_data="strat_minodds_1.80"),
        ],
    ]
    await query.edit_message_text(
        f"Custom Strategy — Step 4/4\n"
        f"Min confidence: {custom['min_confidence']}%\n"
        f"Markets: {', '.join(custom['preferred_markets'])}\n"
        f"Over threshold: {threshold}\n\n"
        "Minimum odds per pick (filters out low-value picks):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return STRAT_CUSTOM_MIN_ODDS


async def callback_strat_custom_min_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom strategy min odds selection and save."""
    query = update.callback_query
    await query.answer()

    min_odds = float(query.data.replace("strat_minodds_", ""))
    custom = context.user_data.get("custom_strategy", {})
    custom["min_odds"] = min_odds
    custom["label"] = "Custom"

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["strategy"] = custom
    save_user_config(config, chat_id=chat_id)

    await query.edit_message_text(
        f"Custom strategy saved!\n\n"
        f"Min confidence: {custom['min_confidence']}%\n"
        f"Min odds/pick: {custom['min_odds']}\n"
        f"Markets: {', '.join(custom['preferred_markets'])}\n"
        f"Over threshold: {custom['over_threshold']}\n\n"
        f"Run /pick to get selections."
    )

    context.user_data.pop("custom_strategy", None)
    return ConversationHandler.END


async def strategy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the strategy flow."""
    context.user_data.pop("custom_strategy", None)
    await update.message.reply_text("Strategy selection cancelled.")
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
    """Fetch booking codes, analyze them, then ask user whether to expand with league picks."""
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

    # Analyze booking code picks (deep analysis)
    code_pick_count = len(all_picks)
    await update.message.reply_text(f"🔬 Deep-analyzing {code_pick_count} booking code game(s)...")

    analyzed_picks = []
    for pick in all_picks:
        try:
            analyzed = await asyncio.to_thread(web_analyze_pick, pick)
            analyzed["_source"] = "booking_code"
            analyzed_picks.append(analyzed)
        except Exception as e:
            logger.warning(f"Analysis failed for {pick.get('home')} vs {pick.get('away')}: {e}")
            pick["data_confidence"] = pick.get("confidence", 50)
            pick["verdict"] = "error"
            pick["analysis_reasons"] = [f"Analysis error: {str(e)[:50]}"]
            pick["suggestion"] = None
            pick["_source"] = "booking_code"
            analyzed_picks.append(pick)

    # Store analyzed code picks and codes in context
    context.user_data["check_picks"] = analyzed_picks
    context.user_data["check_codes"] = valid_codes

    # Send deep review of booking code games
    review = format_deep_review(analyzed_picks, valid_codes)
    await send_long_message(update, review)

    # Ask user: expand with league picks or proceed with code games only?
    expand_buttons = [
        [
            InlineKeyboardButton("✅ Add more picks from SportyBet", callback_data="check_expand_yes"),
        ],
        [
            InlineKeyboardButton("🎯 Code games only", callback_data="check_expand_no"),
        ],
    ]
    await update.message.reply_text(
        "Want to add extra picks from your SportyBet leagues, or proceed with just the code games?",
        reply_markup=InlineKeyboardMarkup(expand_buttons),
    )

    return CHECK_EXPAND


async def check_expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle expand choice: add SportyBet league picks or proceed with code games only."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "check_expand_no":
        # Code games only — go straight to target odds
        await query.edit_message_text("🎯 Proceeding with code games only.")
        return await _show_target_odds_buttons(query.message, context, edit=False)

    # data == "check_expand_yes" — scan leagues and merge
    analyzed_picks = context.user_data.get("check_picks", [])

    # Track matches already in booking codes to avoid duplicates
    code_matches = set()
    for p in analyzed_picks:
        code_matches.add(f"{p['home'].lower()}_{p['away'].lower()}")

    config = load_user_config(chat_id=update.effective_chat.id)
    leagues = config.get("leagues", [])
    days_ahead = config.get("days_ahead", 14)
    today = datetime.now()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    league_picks = []
    if leagues:
        await query.edit_message_text(
            f"📊 Scanning {len(leagues)} league(s) for extra picks to strengthen the pool..."
        )
        for lid in leagues:
            try:
                fixtures = await asyncio.to_thread(get_fixtures_lookahead, lid, date_from=date_from, date_to=date_to)
                for f in fixtures:
                    home_name = f["home"]["name"]
                    away_name = f["away"]["name"]
                    match_key = f"{home_name.lower()}_{away_name.lower()}"

                    if match_key in code_matches:
                        continue  # already in booking code

                    # Score this fixture quickly
                    try:
                        home_results = await asyncio.to_thread(get_team_results, home_name, count=10)
                        away_results = await asyncio.to_thread(get_team_results, away_name, count=10)

                        if not home_results or not away_results:
                            continue

                        home_form = _summarize_form(home_results)
                        away_form = _summarize_form(away_results)

                        # Home Win option
                        home_wr = home_form["wins"] / max(home_form["played"], 1)
                        away_lr = away_form["losses"] / max(away_form["played"], 1)
                        win_conf = int((home_wr * 45 + away_lr * 25 + 30) * 1.0)
                        win_conf = max(0, min(100, win_conf))

                        if win_conf >= 65:
                            win_odds = 1.20 if win_conf >= 85 else 1.35 if win_conf >= 75 else 1.55
                            league_picks.append({
                                "home": home_name,
                                "away": away_name,
                                "tournament": f["league"]["name"],
                                "market": "1X2",
                                "pick": "Home",
                                "odds": win_odds,
                                "confidence": win_conf,
                                "match_status": "Upcoming",
                                "is_winning": None,
                                "score": "",
                                "rating": "moderate" if win_conf < 75 else "safe",
                                "event_id": "",
                                "selection": {},
                                "_source": "league_scan",
                            })

                        # Over 1.5 option
                        expected_goals = (
                            (home_form["avg_scored"] + away_form["avg_conceded"]) / 2 +
                            (away_form["avg_scored"] + home_form["avg_conceded"]) / 2
                        )
                        over_prob = _poisson_over_prob(expected_goals, 1.5)
                        over_conf = max(0, min(100, int(over_prob * 100)))

                        if over_conf >= 70:
                            league_picks.append({
                                "home": home_name,
                                "away": away_name,
                                "tournament": f["league"]["name"],
                                "market": "Over/Under",
                                "pick": f"Over (total=1.5)",
                                "odds": 1.28,
                                "confidence": over_conf,
                                "match_status": "Upcoming",
                                "is_winning": None,
                                "score": "",
                                "rating": "safe",
                                "event_id": "",
                                "selection": {},
                                "_source": "league_scan",
                            })

                        code_matches.add(match_key)  # prevent duplicate per match

                    except Exception as e:
                        logger.warning(f"Error scoring {home_name} vs {away_name}: {e}")
            except Exception as e:
                logger.warning(f"Error fetching league {lid}: {e}")
    else:
        await query.edit_message_text("No leagues configured. Use /leagues to add some.\nProceeding with code games only.")

    # Analyze league picks and merge into the pool
    if league_picks:
        for pick in league_picks:
            try:
                analyzed = await asyncio.to_thread(web_analyze_pick, pick)
                analyzed["_source"] = "league_scan"
                analyzed_picks.append(analyzed)
            except Exception as e:
                pick["data_confidence"] = pick.get("confidence", 50)
                pick["verdict"] = _confidence_verdict(pick.get("confidence", 50))
                pick["analysis_reasons"] = ["From league scan"]
                pick["suggestion"] = None
                pick["data_quality"] = "fair"
                pick["_source"] = "league_scan"
                analyzed_picks.append(pick)

        context.user_data["check_picks"] = analyzed_picks

        code_count = len([p for p in analyzed_picks if p.get("_source") == "booking_code"])
        league_count = len([p for p in analyzed_picks if p.get("_source") == "league_scan"])
        await query.message.reply_text(
            f"📋 Pool expanded: {code_count} from codes + {league_count} from league scan"
        )
    else:
        if leagues:
            await query.message.reply_text("No qualifying league picks found. Proceeding with code games only.")

    return await _show_target_odds_buttons(query.message, context, edit=False)


async def _show_target_odds_buttons(message, context, edit=False):
    """Display target odds selection buttons."""
    picks = context.user_data.get("check_picks", [])
    has_league = any(p.get("_source") == "league_scan" for p in picks)
    source_note = "The best picks from BOTH sources will be combined:" if has_league else "Pick your target odds:"

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

    return await _build_and_send_combo(update, context, target)


async def check_receive_odds_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive target odds from inline button."""
    query = update.callback_query
    await query.answer()

    data = query.data.replace("check_odds_", "")

    picks = context.user_data.get("check_picks", [])

    if data == "all":
        pending = [p for p in picks if p["match_status"] != "Ended"]
        total = 1.0
        for p in pending:
            total *= p["odds"]
        target = total
    else:
        target = float(data)

    context.user_data["check_target"] = target
    return await _build_and_show_combo(query.message, context, target, edit=True)


async def _build_and_send_combo(update: Update, context: ContextTypes.DEFAULT_TYPE, target: float):
    """Build combo from stored picks and send result."""
    picks = context.user_data.get("check_picks", [])

    if not picks:
        await update.message.reply_text("No picks stored. Start over with /check.")
        return ConversationHandler.END

    context.user_data["check_target"] = target
    return await _build_and_show_combo(update.message, context, target, edit=False)


async def _build_and_show_combo(message, context, target, edit=False):
    """Build combo, show it, then ask to exclude or confirm."""
    picks = context.user_data.get("check_picks", [])

    combo = build_best_combo(picks, target)
    context.user_data["check_combo"] = combo

    msg = format_combo_result(combo, target)

    selected = combo.get("selected", [])
    if not selected:
        if edit:
            await message.edit_text(msg)
        else:
            await message.reply_text(msg)
        return ConversationHandler.END

    # Add exclude + swap buttons — one row per selected pick
    buttons = []
    row = []
    for i, p in enumerate(selected):
        short = f"{p['home'][:8]} vs {p['away'][:8]}"
        row.append(InlineKeyboardButton(f"❌ {i+1}. {short}", callback_data=f"exclude_{i}"))
        if p.get("suggestion"):
            row.append(InlineKeyboardButton(f"💡 Swap", callback_data=f"check_swap_{i}"))
        if len(row) >= 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("✅ Looks good — keep all", callback_data="check_confirm"),
    ])

    msg += "\n\nExclude any games? Tap to remove, or confirm:"

    if edit:
        await message.edit_text(msg, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await message.reply_text(msg, reply_markup=InlineKeyboardMarkup(buttons))

    return CHECK_EXCLUDE


async def check_exclude_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle exclude button — remove a pick and rebuild combo."""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "check_confirm":
        # User is happy — try to book on SportyBet
        combo = context.user_data.get("check_combo", {})
        selected = combo.get("selected", [])
        target = context.user_data.get("check_target", 10)

        if not selected:
            await query.edit_message_text("No picks left. Start over with /check.")
            return ConversationHandler.END

        # Final summary
        total_odds = 1.0
        lines = ["✅ Final Selection:\n"]
        for i, p in enumerate(selected, 1):
            source_tag = " 📌" if p.get("_source") == "booking_code" else " 🔍"
            lines.append(f"{i}. {p['home']} vs {p['away']}{source_tag}")
            lines.append(f"   {p['market']}: {p['pick']} @ {p['odds']:.2f}")
            total_odds *= p["odds"]

        lines.append(f"\nTotal Odds: {total_odds:.2f}")
        lines.append(f"Selections: {len(selected)}")

        # Try to auto-book picks that have SportyBet event IDs
        bookable = [p for p in selected if p.get("event_id") and p.get("selection")]
        non_bookable = [p for p in selected if not p.get("event_id") or not p.get("selection")]

        if bookable:
            import requests as req
            try:
                selections = []
                for p in bookable:
                    sel = p.get("selection", {})
                    if isinstance(sel, dict) and sel.get("eventId"):
                        selections.append({
                            "eventId": sel.get("eventId", p.get("event_id", "")),
                            "marketId": str(sel.get("marketId", "1")),
                            "outcomeId": str(sel.get("outcomeId", "1")),
                            "specifier": sel.get("specifier", ""),
                        })
                    elif p.get("event_id"):
                        # Build from pick data
                        market_id = "1"  # 1X2
                        outcome_id = "1"  # Home
                        specifier = ""

                        pick_lower = p.get("pick", "").lower()
                        market_lower = p.get("market", "").lower()

                        if "over" in pick_lower or "under" in pick_lower:
                            market_id = "18"
                            outcome_id = "12" if "over" in pick_lower else "13"
                            if "total=" in pick_lower:
                                specifier = pick_lower.split("(")[1].rstrip(")") if "(" in pick_lower else ""
                        elif "gg" in pick_lower or "gg" in market_lower:
                            market_id = "29"
                            outcome_id = "74"  # GG
                        elif "away" in pick_lower or pick_lower == "2":
                            outcome_id = "3"
                        elif "draw" in pick_lower or pick_lower == "x":
                            outcome_id = "2"

                        selections.append({
                            "eventId": p["event_id"],
                            "marketId": market_id,
                            "outcomeId": outcome_id,
                            "specifier": specifier,
                        })

                if selections:
                    def _post_booking_code():
                        return req.post(
                            "https://www.sportybet.com/api/ng/orders/share",
                            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
                            json={"selections": selections},
                            timeout=15,
                        )
                    r = await asyncio.to_thread(_post_booking_code)
                    resp = r.json()
                    if resp.get("bizCode") == 10000 and resp.get("data", {}).get("shareCode"):
                        code = resp["data"]["shareCode"]
                        lines.append(f"\n🎫 Booking Code: {code}")
                        lines.append(f"Tap to copy → SportyBet App → Betslip → Load Code")
                    else:
                        lines.append(f"\n⚠️ Auto-booking failed: {resp.get('message', 'unknown error')}")
                        lines.append("You can manually add these on SportyBet.")
            except Exception as e:
                lines.append(f"\n⚠️ Booking error: {str(e)[:50]}")
                lines.append("You can manually add these on SportyBet.")

        if non_bookable:
            lines.append(f"\n📋 {len(non_bookable)} pick(s) from league scan — find these manually on SportyBet.")

        await query.edit_message_text("\n".join(lines))

        # Clean up
        context.user_data.pop("check_picks", None)
        context.user_data.pop("check_codes", None)
        context.user_data.pop("check_combo", None)
        context.user_data.pop("check_target", None)
        return ConversationHandler.END

    # Swap a pick's market to its suggestion
    if data.startswith("check_swap_"):
        idx = int(data.replace("check_swap_", ""))
        combo = context.user_data.get("check_combo", {})
        selected = combo.get("selected", [])
        if 0 <= idx < len(selected):
            pick = selected[idx]
            suggestion = pick.get("suggestion")
            if suggestion:
                old_desc = f"{pick['market']}: {pick['pick']}"
                pick["market"] = suggestion["market"]
                pick["pick"] = suggestion["market"]
                pick["confidence"] = suggestion["confidence"]
                if "data_confidence" in pick:
                    pick["data_confidence"] = suggestion["confidence"]
                pick["verdict"] = _confidence_verdict(suggestion["confidence"])
                pick["suggestion"] = None
                if pick["odds"] > 1.5:
                    pick["odds"] = max(1.10, pick["odds"] * 0.75)
                # Also update in the main picks pool
                picks = context.user_data.get("check_picks", [])
                for pp in picks:
                    if pp["home"] == pick["home"] and pp["away"] == pick["away"]:
                        pp["market"] = pick["market"]
                        pp["pick"] = pick["pick"]
                        pp["confidence"] = pick["confidence"]
                        pp["odds"] = pick["odds"]
                        pp["suggestion"] = None
                        break
                await query.edit_message_text(
                    f"💡 Swapped #{idx+1}: {old_desc}\n"
                    f"   → {pick['market']} @ ~{pick['odds']:.2f}\nRebuilding..."
                )
            else:
                await query.edit_message_text("No suggestion available.\nRebuilding...")
        target = context.user_data.get("check_target", 10)
        return await _build_and_show_combo(query.message, context, target, edit=True)

    # Exclude a pick
    idx = int(data.replace("exclude_", ""))
    combo = context.user_data.get("check_combo", {})
    selected = combo.get("selected", [])

    removed = None
    if 0 <= idx < len(selected):
        removed = selected.pop(idx)
        logger.info(f"Excluded: {removed['home']} vs {removed['away']}")
    else:
        logger.warning(f"Exclude index {idx} out of range (selected has {len(selected)} items)")

    # Also remove from the main picks pool so it doesn't get re-added
    if removed is not None:
        picks = context.user_data.get("check_picks", [])
        picks = [p for p in picks if not (p["home"] == removed["home"] and p["away"] == removed["away"])]
        context.user_data["check_picks"] = picks

    # Rebuild combo with remaining picks
    target = context.user_data.get("check_target", 10)
    return await _build_and_show_combo(query.message, context, target, edit=True)


async def check_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the check flow."""
    context.user_data.pop("check_picks", None)
    context.user_data.pop("check_codes", None)
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
        BotCommand("strategy", "Set betting strategy (conservative/balanced/aggressive/custom)"),
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
    config = load_user_config()
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

        if market_slots:
            context.user_data["pick_market_slots"] = market_slots
            logger.info(f"Market slots: {market_slots}")

        if target and isinstance(target, (int, float)) and target > 1:
            context.user_data["pick_target"] = float(target)
            await update.message.reply_text(reply)
            return await _pick_analyze(update, context, float(target))
        else:
            # No target extracted — show the pick menu
            await update.message.reply_text(reply)
            return await pick_start(update, context)

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
            CHECK_EXCLUDE: [
                CallbackQueryHandler(check_exclude_callback, pattern=r"^(exclude_\d+|check_swap_\d+|check_confirm)$"),
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
            PICK_ODDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_receive_odds_text),
                CallbackQueryHandler(pick_receive_odds_button, pattern=r"^pick_target_"),
            ],
            PICK_REVIEW: [
                CallbackQueryHandler(pick_review_callback, pattern=r"^(pick_exclude_\d+|pick_swap_\d+|pick_change_\d+|pick_mkt_.+|pick_reshuffle|pick_confirm)$"),
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

    # Strategy conversation handler
    strat_conv = ConversationHandler(
        entry_points=[CommandHandler("strategy", cmd_strategy)],
        states={
            STRAT_CUSTOM_CONFIDENCE: [
                CallbackQueryHandler(callback_strat_custom_confidence, pattern=r"^strat_conf_"),
                CallbackQueryHandler(callback_strategy_select, pattern=r"^strat_(conservative|balanced|aggressive|overs_only|btts_mix|favourites|custom|edit)$"),
            ],
            STRAT_CUSTOM_MARKETS: [
                CallbackQueryHandler(callback_strat_custom_markets, pattern=r"^strat_mkt_"),
            ],
            STRAT_CUSTOM_OVER: [
                CallbackQueryHandler(callback_strat_custom_over, pattern=r"^strat_over_"),
            ],
            STRAT_CUSTOM_MIN_ODDS: [
                CallbackQueryHandler(callback_strat_custom_min_odds, pattern=r"^strat_minodds_"),
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
        callback_strategy_select, pattern=r"^strat_(conservative|balanced|aggressive|overs_only|btts_mix|favourites)$"
    ))
    app.add_handler(CallbackQueryHandler(
        callback_timeframe, pattern=r"^tf_"
    ))

    # Natural language is now handled as a pick_conv entry point
    # (it returns ConversationHandler.END for non-pick intents)

    print("SportyBot Telegram bot starting...")
    print(f"API Budget: {get_api_budget()['remaining']} calls remaining")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
