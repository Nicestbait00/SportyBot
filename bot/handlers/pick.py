"""SportyBot /pick conversation handler.

Handles the guided pick flow: ticket type -> count -> odds -> mode -> analyze -> review -> book.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from core.config import (
    DEFAULT_ENABLED_MARKETS,
    LEAGUE_CATEGORIES,
    LEAGUE_NAMES,
    STRATEGY_PRESETS,
    TIMEFRAME_PRESETS,
    load_user_config,
    save_user_config,
)
from data.data_collector import get_api_budget, get_fixtures, get_fixtures_lookahead, refresh_all
from data.web_analyzer import analyze_pick as web_analyze_pick, get_team_results, _summarize_form
from core.scorer import score_match, cross_check_with_odds
from services.analysis_service import (
    add_extended_picks as _add_extended_picks,
    add_thin_data_safe_picks as _add_thin_data_safe_picks,
    score_fixture as _score_fixture,
)
from services.ticket_engine import (
    confidence_verdict as _confidence_verdict,
    pick_key as _pick_key,
    pick_threshold as _pick_threshold,
    match_key as _match_key,
    ticket_totals as _ticket_totals,
    is_safe_team_total_pick as _is_safe_team_total_pick,
    is_thin_data_safe_pick as _is_thin_data_safe_pick,
    pick_selection_score as _pick_selection_score,
    pick_qualifies_for_combo as _pick_qualifies_for_combo,
    market_cap as _market_cap,
    make_ticket_entry as _make_ticket_entry,
    build_fallback_profiles as _build_fallback_profiles,
    build_qualified_pool as _build_qualified_pool,
    select_ticket_from_pool as _select_ticket_from_pool,
    select_unique_ticket_from_pool as _select_unique_ticket_from_pool,
    generate_dynamic_bundle as _generate_dynamic_bundle,
    generate_unique_bundle as _generate_unique_bundle,
)
from services import ticket_engine
from services import ticket_splitter
from data.sportybet_events import (
    build_event_index, clear_cache as clear_sportybet_cache,
    fetch_all_events, filter_events, find_event,
    build_booking_selection, create_booking_code,
)
from data.booking_service import fetch_booking_code, parse_outcomes
from bot.formatters import (
    format_pick,
    format_analysis,
    format_combo_result,
    send_long_message,
    build_best_combo,
    format_deep_review,
    format_picks_review,
    _sort_booking_picks,
    _kickoff_ms_from_pick,
    _format_pick_kickoff,
)
from bot.handlers._shared import _mark_job_start, _mark_job_finish, _send_or_edit, _check_rate_limit

logger = logging.getLogger(__name__)

TEAM_RESULTS_FETCH_CONCURRENCY = 2
BOOKING_FETCH_CONCURRENCY = 4


# Conversation states for /pick flow
PICK_TYPE, PICK_COUNT, PICK_ODDS, PICK_MODE, PICK_REVIEW, GAME_DIALOGUE = range(10, 16)





def _team_results_cache_key(team_name: str, count: int) -> str:
    """Stable key for per-run team results caching."""
    return f"{team_name.strip().lower()}::{count}"


async def _get_team_results_cached(
    context: ContextTypes.DEFAULT_TYPE,
    team_name: str,
    *,
    count: int = 10,
) -> list[dict]:
    """Reuse team results within one bot flow and cap concurrent fetches."""
    cache = context.user_data.setdefault("_team_results_cache", {})
    key = _team_results_cache_key(team_name, count)
    if key in cache:
        return cache[key]

    semaphore = context.application.bot_data.get("team_results_semaphore")
    if semaphore is None:
        semaphore = asyncio.Semaphore(TEAM_RESULTS_FETCH_CONCURRENCY)
        context.application.bot_data["team_results_semaphore"] = semaphore

    async with semaphore:
        if key in cache:
            return cache[key]
        results = await asyncio.to_thread(get_team_results, team_name, count=count)
        cache[key] = results
        return results



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
        "_team_results_cache",
    ]:
        context.user_data.pop(key, None)


def _clear_sort_runtime(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear transient /sort state."""
    for key in [
        "sort_code",
        "sort_mode",
        "sort_pending_picks",
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


async def _prompt_pick_target_odds(message, context=None, edit: bool = False):
    """Ask for target odds per ticket."""
    req = _ensure_pick_request(context) if context else {}
    is_multi = req.get("ticket_type") == "multiple"
    count = int(req.get("ticket_count", 2)) if is_multi else 0

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

    if is_multi and count >= 2:
        text = (
            f"🎯 What odds are you targeting for your {count} tickets?\n\n"
            f"• Tap a button for the *same* odds on all tickets\n"
            f"• Or type individual odds separated by commas\n"
            f"  e.g. `5,10,25` for 3 different targets"
        )
    else:
        text = "🎯 What odds are you targeting per ticket?\nPick below or type a custom number."

    await _send_or_edit(message, text, reply_markup=buttons, edit=edit, parse_mode="Markdown" if is_multi else None)
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
        return await _prompt_pick_target_odds(message, context=context, edit=edit)
    if req["ticket_type"] == "multiple" and req.get("ticket_mode") not in ("unique", "dynamic"):
        return await _prompt_pick_ticket_mode(message, edit=edit)

    context.user_data["pick_target"] = float(req["target_odds"])
    context.user_data["pick_bundle_mode"] = req.get("ticket_mode", "single")
    return await _pick_analyze_from_message(message, context, float(req["target_odds"]))



async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message and quick setup."""
    from core.config import TIMEFRAME_PRESETS
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



async def pick_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the guided /pick flow."""
    rl_msg = _check_rate_limit(context, "pick")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return ConversationHandler.END
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
    """Receive target odds as text. Supports comma-separated for multi-ticket."""
    raw = update.message.text.strip()
    req = _ensure_pick_request(context)
    is_multi = req.get("ticket_type") == "multiple"
    count = int(req.get("ticket_count", 2)) if is_multi else 0

    # Try parsing as comma-separated list first (e.g. "5,10,25")
    if "," in raw and is_multi:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        targets = []
        for p in parts:
            try:
                t = float(p)
                if t < 1.5 or t > 500:
                    await update.message.reply_text(f"Each target must be between 1.5 and 500. `{p}` is out of range.", parse_mode="Markdown")
                    return PICK_ODDS
                targets.append(t)
            except ValueError:
                await update.message.reply_text(f"Couldn't parse `{p}` as a number. Try again.", parse_mode="Markdown")
                return PICK_ODDS

        if len(targets) != count:
            await update.message.reply_text(
                f"You have {count} tickets but gave {len(targets)} target(s). "
                f"Send exactly {count} values (e.g. `5,10,25`).",
                parse_mode="Markdown",
            )
            return PICK_ODDS

        req["target_odds"] = targets[0]  # for display/compat
        req["target_odds_list"] = targets
        context.user_data["pick_request"] = req
        context.user_data["pick_target"] = targets[0]
        return await _continue_pick_request_flow(update.message, context)

    # Single number
    try:
        target = float(raw)
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 10) or comma-separated (e.g. 5,10,25). /cancel to exit.")
        return PICK_ODDS
    if target < 1.5 or target > 500:
        await update.message.reply_text("Target odds must be between 1.5 and 500. Try again or /cancel.")
        return PICK_ODDS
    req["target_odds"] = target
    req.pop("target_odds_list", None)
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

    job_token = _mark_job_start(context, "pick_analysis")
    job_success = False
    job_error = None

    try:
        chat_id = context.user_data.get("chat_id")
        config = load_user_config(chat_id=chat_id)
        leagues = config.get("leagues", [])
        timeframe = config.get("timeframe", "7days")
        today = datetime.now()
        date_str = today.strftime("%Y-%m-%d")

        # Check analysis cache first (valid for 3 hours)
        cache_key_raw = f"pick_sportybet|{date_str}|{sorted(leagues)}|{timeframe}"
        cache_key = hashlib.sha256(cache_key_raw.encode()).hexdigest()[:16]
        cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "analysis"
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
            import time as _time2
            cache_age_min = int((_time2.time() - cdata.get("_ts", 0)) / 60)
            stale_note = ""
            if cache_age_min > 60:
                stale_note = f"\n⏳ Cached {cache_age_min}m ago — odds may have shifted. Use /refresh to re-analyze."
            await message.reply_text(
                f"⚡ Using cached analysis ({len(cached_scored)} scored picks).\n"
                f"Target: ~{target:.0f} odds{stale_note}",
            )
            context.user_data["pick_all_scored"] = cached_scored
            context.user_data["pick_excluded"] = set()
            job_success = True
            return await _pick_build_combo(message, context)

        # Resolve friendly names for display
        from core.config import LEAGUE_NAMES, TIMEFRAME_PRESETS
        league_display = ", ".join(LEAGUE_NAMES.get(lid, str(lid)) for lid in leagues) if leagues else "All leagues"
        timeframe_display = TIMEFRAME_PRESETS.get(timeframe, {}).get("label", timeframe)

        await message.reply_text(
            f"🔍 Fetching upcoming matches from SportyBet...\n"
            f"Leagues: {league_display}\n"
            f"Window: {timeframe_display}\n"
            f"Target: ~{target:.0f} odds\n"
            f"First run — caching results for instant reshuffles.",
        )

        # Send typing indicator while fetching
        try:
            await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
        except Exception:
            pass

        # Step 1: Get fixtures directly from SportyBet (has event IDs + real odds)
        # Build tournament name filter from user's league config
        from core.config import LEAGUE_SPORTYBET_NAMES
        allowed_tournaments = None
        if leagues:
            allowed_tournaments = []
            for lid in leagues:
                allowed_tournaments.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

        try:
            all_sporty_events = await asyncio.to_thread(fetch_all_events, max_pages=15, allowed_tournaments=allowed_tournaments or None)
        except Exception as e:
            logger.warning(f"SportyBet fetch failed: {e}")
            all_sporty_events = []

        if not all_sporty_events:
            await message.reply_text(
                "Could not fetch fixtures from SportyBet.\n"
                "This usually means the SportyBet API is temporarily down.\n\n"
                f"Leagues tried: {league_display}\n"
                "Try /refresh in a few minutes, or check if sportybet.com is accessible.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Retry", callback_data=f"pick_target_{target}")],
                ]),
            )
            job_success = True
            return PICK_ODDS

        # Filter by league + timeframe
        sporty_events = filter_events(all_sporty_events, league_ids=leagues, timeframe=timeframe)

        if not sporty_events:
            await message.reply_text(
                f"No matches found for {league_display} in {timeframe_display}.\n"
                f"Try /timeframe to change window or /leagues to add leagues."
            )
            job_success = True
            return ConversationHandler.END

        total_matches = len(sporty_events)
        progress_msg = await message.reply_text(
            f"Found {total_matches} matches ({len(all_sporty_events)} total on SportyBet).\n"
            f"Analyzing form data... 0/{total_matches}"
        )

        # Step 2: Score every fixture using analysis_service.score_fixture
        all_scored = []
        analyzed_count = 0
        _last_progress = 0

        for ev_idx, ev in enumerate(sporty_events):
            if ev_idx % 10 == 0:
                try:
                    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
                except Exception:
                    pass

            home_name = ev["home"]
            away_name = ev["away"]

            try:
                home_results, away_results = await asyncio.gather(
                    _get_team_results_cached(context, home_name, count=10),
                    _get_team_results_cached(context, away_name, count=10),
                )

                # Skip if no form data and no 1X2 market for odds-only fallback
                if not home_results and not away_results:
                    real_1x2 = ev.get("markets", {}).get("1", {})
                    if not real_1x2.get("outcomes"):
                        continue

                analyzed_count += 1

                if analyzed_count - _last_progress >= 5:
                    _last_progress = analyzed_count
                    try:
                        await progress_msg.edit_text(
                            f"Found {total_matches} matches.\n"
                            f"Analyzing form data... {analyzed_count}/{total_matches}"
                        )
                    except Exception:
                        pass

                fixture_picks = _score_fixture(ev, home_results, away_results)
                all_scored.extend(fixture_picks)

            except Exception as e:
                logger.warning(f"Error analyzing {home_name} vs {away_name}: {e}")

        if not all_scored:
            pick_cfg = _get_pick_config(config)
            await message.reply_text(
                f"No picks passed the filters after analyzing {analyzed_count} matches.\n\n"
                f"Current filters:\n"
                f"• Min confidence: {pick_cfg.get('min_confidence', 75)}%\n"
                f"• Min odds: {pick_cfg.get('min_odds', 1.05)}\n"
                f"• Markets: {len(pick_cfg.get('preferred_markets', []))} enabled\n\n"
                "Try /strategy to loosen filters, or /leagues to add more leagues.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Retry", callback_data=f"pick_target_{target}")],
                ]),
            )
            job_success = True
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

        job_success = True
        return await _pick_build_combo(message, context)
    except Exception as e:
        job_error = e
        logger.exception("Pick analysis failed")
        await message.reply_text(
            "I hit an unexpected error while building those picks. Please try again in a moment."
        )
        return ConversationHandler.END
    finally:
        _mark_job_finish(context, job_token, success=job_success, error=job_error)



# ── Scoring/pool/ticket functions are in ticket_engine.py ──────────────────
# Imported as _confidence_verdict, _pick_key, _pick_threshold, _match_key,
# _ticket_totals, _is_safe_team_total_pick, _is_thin_data_safe_pick,
# _pick_selection_score, _pick_qualifies_for_combo, _market_cap,
# _make_ticket_entry, _build_fallback_profiles, _build_qualified_pool,
# _select_ticket_from_pool, _select_unique_ticket_from_pool,
# _generate_dynamic_bundle, _generate_unique_bundle


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
    targets = req.get("target_odds_list") or target

    if ticket_mode == "unique":
        # Unique mode uses a single target (same fixtures, different markets)
        single_target = targets[0] if isinstance(targets, list) else targets
        bundle, profile = _generate_unique_bundle(all_scored, ticket_count, single_target, pick_cfg, excluded, shuffle_seed)
        return bundle, profile, False

    bundle, profile, reused = _generate_dynamic_bundle(all_scored, ticket_count, targets, pick_cfg, excluded, None, shuffle_seed)
    return bundle, profile, reused


def _build_ticket_review_text(ticket: dict, heading: str | None = None) -> str:
    """Format a single ticket for Telegram review."""
    picks = ticket.get("picks", [])
    is_reused = ticket.get("reused_fixtures", False)
    lines = [heading or f"🎫 Ticket {ticket.get('id', 1)}"]
    lines.append("")

    has_limited = False
    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠"}
    for idx, pick in enumerate(picks, 1):
        icon = verdict_icons.get(pick.get("verdict", ""), "❓")
        if pick.get("data_quality") == "limited":
            icon = "⚠️"
            has_limited = True
        market_label = pick.get("pick", "").replace("(total=", "").replace(")", "")
        conf = pick.get("data_confidence", pick.get("confidence", "?"))
        suffix = " ♻️" if is_reused else ""
        lines.append(f"{icon} {idx}. {pick.get('home')} vs {pick.get('away')}{suffix}")
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
    legend_parts = []
    if has_limited:
        legend_parts.append("⚠️ = limited data")
    if is_reused:
        legend_parts.append("♻️ = reused fixture (different market)")
    if legend_parts:
        lines.append(" | ".join(legend_parts))
    return "\n".join(lines)


async def _show_pick_bundle_summary(message, context, edit: bool = False):
    """Show the multi-ticket bundle summary with high-level actions."""
    bundle = context.user_data.get("pick_bundle", [])
    req = _ensure_pick_request(context)
    if not bundle:
        await _send_or_edit(message, "No tickets available right now.", edit=edit)
        return ConversationHandler.END

    targets_list = req.get("target_odds_list")
    if targets_list and len(set(targets_list)) > 1:
        targets_str = ", ".join(f"{t:.0f}" for t in targets_list)
        target_line = f"Targets: {targets_str} odds"
    else:
        target_line = f"Target per ticket: ~{float(req.get('target_odds') or 0):.0f} odds"

    lines = [
        f"🎫 Bundle ready: {len(bundle)} ticket(s)",
        f"Mode: {req.get('ticket_mode', 'dynamic').title()}",
        target_line,
        "",
    ]

    for ticket in bundle:
        status_bits = [f"~{ticket['total_odds']:.2f} odds", f"{ticket['avg_confidence']}% avg"]
        if any(p.get("data_quality") == "limited" for p in ticket.get("picks", [])):
            status_bits.append("⚠️ thin-data picks")
        if ticket.get("reused_fixtures"):
            status_bits.append("♻️ reused fixtures")
        lines.append(f"Ticket {ticket['id']}: " + " | ".join(status_bits))

    buttons = [
        [
            InlineKeyboardButton(f"✏️ Edit Ticket {ticket['id']}", callback_data=f"pick_bundle_edit_{idx}"),
            InlineKeyboardButton(f"🗑️ Remove", callback_data=f"pick_bundle_delete_{idx}"),
        ]
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
    from core.config import LEAGUE_NAMES as _LN, TIMEFRAME_PRESETS as _TP

    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")
    league_str = ", ".join(_LN.get(lid, str(lid)) for lid in leagues) if leagues else "All"
    timeframe_str = _TP.get(timeframe, {}).get("label", timeframe)

    is_reused = ticket.get("reused_fixtures", False)
    verdict_icons = {"strong": "🟢", "moderate": "🟡", "weak": "🟠"}
    lines = [f"🎯 Picks for ~{target:.0f} odds | {league_str} | {timeframe_str}", ""]

    has_limited = False
    for idx, pick in enumerate(selected, 1):
        icon = verdict_icons.get(pick.get("verdict", ""), "❓")
        # Flag limited-data picks
        if pick.get("data_quality") == "limited":
            icon = "⚠️"
            has_limited = True
        market_label = pick.get("pick", "").replace("(total=", "").replace(")", "")
        conf = pick.get("data_confidence", pick.get("confidence", 0))
        suffix = " ♻️" if is_reused else ""
        lines.append(f"{icon} {idx}. {pick.get('home')} vs {pick.get('away')}{suffix}")
        lines.append(f"   [{pick.get('league', '')}] {pick.get('market')}: {market_label} @ ~{pick.get('odds', 0):.2f} [{conf}%]")
        reasons = pick.get("analysis_reasons", [])
        if reasons:
            lines.append(f"   > {reasons[0]}")
        lines.append("")

    lines.append(f"Total Odds: ~{ticket.get('total_odds', 1.0):.2f}")
    lines.append(f"Selections: {len(selected)}")
    lines.append(f"Avg Confidence: {ticket.get('avg_confidence', 0)}%")

    # Legend for special indicators
    legend_parts = []
    if has_limited:
        legend_parts.append("⚠️ = limited data")
    if is_reused:
        legend_parts.append("♻️ = reused fixture")
    if legend_parts:
        lines.append(" | ".join(legend_parts))

    buttons = []
    for idx, _pick in enumerate(selected):
        buttons.append([
            InlineKeyboardButton(f"❌ {idx + 1}", callback_data=f"pick_exclude_{idx}"),
            InlineKeyboardButton(f"🔄 {idx + 1}", callback_data=f"pick_change_{idx}"),
            InlineKeyboardButton(f"💬 {idx + 1}", callback_data=f"pick_dialogue_{idx}"),
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
            await _send_or_edit(
                message,
                "Couldn't build a combo from these picks.\n"
                "The code picks may not meet the current confidence/odds filters.\n"
                "Try /strategy to loosen filters or lower the target odds.",
                edit=False,
            )
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
                failed_bookings.append(f"{p['home']} vs {p['away']}: event not found (may have started or been removed)")
                continue

        logger.info(f"  selection: {sel}")
        if sel:
            booking_selections.append(sel)
        else:
            failed_bookings.append(f"{p['home']} vs {p['away']}: market '{p['market']}' unavailable (odds may have changed)")

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
        elif market.endswith("Or Over") or market.endswith("Or Under") or market.endswith("Or GG"):
            label = f"{market} ({pick_str})"
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

    if data.startswith("pick_bundle_delete_"):
        idx = int(data.replace("pick_bundle_delete_", ""))
        bundle = context.user_data.get("pick_bundle", [])
        if 0 <= idx < len(bundle):
            removed = bundle.pop(idx)
            # Re-number remaining tickets
            for i, t in enumerate(bundle):
                t["id"] = i + 1
            if not bundle:
                await query.edit_message_text("All tickets removed. Use /pick to generate new ones.")
                return ConversationHandler.END
            await query.edit_message_text(f"🗑️ Ticket removed. {len(bundle)} ticket(s) remaining.")
            return await _show_pick_bundle_summary(query.message, context, edit=False)
        return PICK_REVIEW

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

    elif data.startswith("pick_dialogue_"):
        idx = int(data.replace("pick_dialogue_", ""))
        combo = context.user_data.get("pick_combo", [])
        if idx < 0 or idx >= len(combo):
            return PICK_REVIEW
        context.user_data["dialogue_pick_idx"] = idx
        pick = combo[idx]
        # Show full analysis for this game
        await _explain_picks(query.message, context, combo, [idx + 1])
        await query.message.reply_text(
            f"💬 Let's talk about: {pick['home']} vs {pick['away']}\n"
            f"Current: {pick['market']}: {pick.get('pick', '')} @ {pick.get('odds', 0):.2f}\n\n"
            "Suggest a different market (e.g. 'what about over 1.5?') or type 'done' to go back."
        )
        return GAME_DIALOGUE

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


async def game_dialogue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle per-game dialogue during combo review."""
    text = update.message.text.strip()
    idx = context.user_data.get("dialogue_pick_idx", -1)
    combo = context.user_data.get("pick_combo", [])

    if idx < 0 or idx >= len(combo):
        await update.message.reply_text("Lost track of the game. Returning to review.")
        context.user_data.pop("dialogue_pick_idx", None)
        if context.user_data.get("_check_mode"):
            return await _check_build_and_show(update.message, context)
        return await _pick_build_combo(update.message, context)

    # Exit dialogue
    if text.lower() in ("done", "back", "exit", "return", "go back"):
        context.user_data.pop("dialogue_pick_idx", None)
        if context.user_data.get("_check_mode"):
            return await _check_build_and_show(update.message, context)
        return await _pick_build_combo(update.message, context)

    pick = combo[idx]
    all_scored = context.user_data.get("pick_all_scored", [])
    match_key = _match_key(pick)

    # Get all scored alternatives for this match
    match_alts = [p for p in all_scored if _match_key(p) == match_key and _pick_key(p) != _pick_key(pick)]

    # Use Gemini to understand the user's intent
    result = await asyncio.to_thread(
        gemini_chat.parse_game_dialogue, text, pick, match_alts
    )

    intent = result.get("intent", "chat")
    params = result.get("params", {})
    reply = result.get("reply", "")

    if intent == "switch":
        # User wants to switch to a specific market
        target_market = params.get("market", "").lower()
        target_pick = params.get("pick", "").lower()

        # Find the best matching alternative
        best_alt = None
        best_score = -1
        for alt in match_alts:
            alt_market = alt.get("market", "").lower()
            alt_pick = alt.get("pick", "").lower()
            score = 0
            if target_market and target_market in alt_market:
                score += 2
            if target_pick and target_pick in alt_pick:
                score += 2
            if target_market and alt_market in target_market:
                score += 1
            if score > best_score:
                best_score = score
                best_alt = alt

        if best_alt and best_score > 0:
            old_desc = f"{pick['market']}: {pick.get('pick', '')} @ {pick['odds']:.2f}"
            # Apply the switch
            for field in ["market", "pick", "odds", "confidence", "data_confidence",
                          "verdict", "analysis_reasons", "data_quality", "rating"]:
                if field in best_alt:
                    pick[field] = best_alt[field]
            new_desc = f"{pick['market']}: {pick.get('pick', '')} @ {pick['odds']:.2f}"
            await update.message.reply_text(
                f"✅ Switched!\n"
                f"Was: {old_desc}\n"
                f"Now: {new_desc} [{pick.get('confidence', '?')}%]\n\n"
                "Type 'done' to return to your combo, or suggest another market."
            )
        else:
            await update.message.reply_text(
                reply or "That market isn't available for this match. "
                "Try one of these:\n" + "\n".join(
                    f"  • {a['market']}: {a.get('pick', '')} @ {a.get('odds', 0):.2f} [{a.get('confidence', '?')}%]"
                    for a in sorted(match_alts, key=lambda x: x.get("confidence", 0), reverse=True)[:5]
                )
            )
        return GAME_DIALOGUE

    elif intent == "compare":
        # Show comparison between current and suggested
        if reply:
            await update.message.reply_text(reply)
        else:
            # Build a comparison ourselves
            target_market = params.get("market", "").lower()
            relevant = [a for a in match_alts if target_market in a.get("market", "").lower()]
            if not relevant:
                relevant = match_alts[:4]
            lines = [f"📊 Comparison for {pick['home']} vs {pick['away']}:\n"]
            lines.append(f"Current: {pick['market']}: {pick.get('pick', '')} @ {pick['odds']:.2f} [{pick.get('confidence', '?')}%]")
            for r in pick.get("analysis_reasons", [])[:2]:
                lines.append(f"  > {r}")
            lines.append("")
            for alt in sorted(relevant, key=lambda x: x.get("confidence", 0), reverse=True)[:4]:
                lines.append(f"Alternative: {alt['market']}: {alt.get('pick', '')} @ {alt.get('odds', 0):.2f} [{alt.get('confidence', '?')}%]")
                for r in alt.get("analysis_reasons", [])[:1]:
                    lines.append(f"  > {r}")
            lines.append("\nSay 'switch to [market]' to change, or 'done' to go back.")
            await update.message.reply_text("\n".join(lines))
        return GAME_DIALOGUE

    elif intent == "keep":
        context.user_data.pop("dialogue_pick_idx", None)
        await update.message.reply_text("👍 Keeping the original. Heading back to your combo.")
        if context.user_data.get("_check_mode"):
            return await _check_build_and_show(update.message, context)
        return await _pick_build_combo(update.message, context)

    else:
        # General chat — relay Gemini's response
        if reply:
            await update.message.reply_text(reply)
        else:
            await update.message.reply_text(
                "I'm not sure what you mean. You can:\n"
                "• Suggest a market: 'what about over 1.5?'\n"
                "• Compare: 'compare GG vs over 2.5'\n"
                "• Switch: 'switch to double chance'\n"
                "• Go back: 'done'"
            )
        return GAME_DIALOGUE


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
