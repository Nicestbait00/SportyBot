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
import time
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
    PicklePersistence,
    filters,
)

# Add project root to path so package imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from core.config import (
    AVAILABLE_MARKETS,
    DEFAULT_ENABLED_MARKETS,
    LEAGUE_CATEGORIES,
    LEAGUE_NAMES,
    LEAGUES,
    STRATEGY_PRESETS,
    load_user_config,
    save_user_config,
)
from data.data_collector import get_api_budget, get_fixtures, get_fixtures_lookahead, refresh_all
from services.analyzer import find_best_combo
from data.web_analyzer import analyze_pick as web_analyze_pick, get_team_results, _summarize_form
from core.scorer import score_match, cross_check_with_odds
from services import gemini_chat
from services import chat_agent
from services import ticket_engine
from services import ticket_splitter
from data.booking_service import fetch_booking_code, parse_outcomes
from services.analysis_service import (
    add_extended_picks as _add_extended_picks,
    add_thin_data_safe_picks as _add_thin_data_safe_picks,
    score_fixture as _score_fixture,
)
from services.ticket_engine import (
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
from data.sportybet_events import (
    build_event_index, clear_cache as clear_sportybet_cache,
    fetch_all_events, filter_events, find_event,
    build_booking_selection, create_booking_code,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



# ── Handler module imports ──────────────────────────────────────────────────
from bot.formatters import send_long_message
from bot.handlers.pick import (
    PICK_TYPE, PICK_COUNT, PICK_ODDS, PICK_MODE, PICK_REVIEW, GAME_DIALOGUE,
    cmd_start,
    pick_start, pick_receive_type_text, pick_receive_type_button,
    pick_receive_count_text, pick_receive_count_button,
    pick_receive_odds_text, pick_receive_odds_button,
    pick_receive_mode_text, pick_receive_mode_button,
    pick_review_callback, pick_edit_text, pick_cancel,
    game_dialogue_handler,
    _ensure_pick_request, _continue_pick_request_flow,
    _get_pick_config,
)
from bot.handlers.check import (
    CHECK_CODES, CHECK_EXPAND, CHECK_TARGET_ODDS, CHECK_REVIEW,
    check_start, check_receive_codes, check_expand_callback,
    check_receive_odds_text, check_receive_odds_button,
    check_review_callback, check_edit_text, check_cancel,
)
from bot.handlers.strategy import (
    STRAT_CUSTOM_CONFIDENCE, STRAT_CUSTOM_MARKETS, STRAT_CUSTOM_OVER,
    STRAT_CUSTOM_MIN_ODDS, STRAT_LEAGUES, STRAT_TIMEFRAME,
    cmd_strategy, callback_strategy_select, callback_strat_custom_confidence,
    callback_strat_custom_markets, callback_strat_custom_over,
    callback_strat_custom_min_odds, callback_strat_leagues,
    callback_strat_timeframe, strategy_cancel,
    cmd_leagues, cmd_timeframe, callback_league_toggle, callback_timeframe,
)
from bot.handlers.split import (
    SPLIT_CODE, SPLIT_COUNT, SPLIT_TARGETS, SPLIT_CONFIRM,
    split_start, split_receive_code, split_receive_count,
    split_receive_targets, split_confirm_callback, split_cancel,
)
from bot.handlers.sort import (
    SORT_CODE, SORT_MODE,
    cmd_sort, sort_receive_code, sort_receive_mode, sort_cancel,
)

# ── Constants ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CONCURRENT_UPDATES = max(1, int(os.getenv("CONCURRENT_UPDATES", "8") or 8))
REFRESH_CONCURRENCY = max(1, int(os.getenv("REFRESH_CONCURRENCY", "4") or 4))
BOOKING_FETCH_CONCURRENCY = max(1, int(os.getenv("BOOKING_FETCH_CONCURRENCY", "4") or 4))
TEAM_RESULTS_FETCH_CONCURRENCY = max(1, int(os.getenv("TEAM_RESULTS_FETCH_CONCURRENCY", "2") or 2))

def _runtime_mode_from_env() -> str:
    """Resolve the configured runtime mode."""
    runtime_mode = (os.getenv("BOT_MODE", "auto").strip().lower() or "auto")
    if runtime_mode not in {"auto", "polling", "webhook"}:
        runtime_mode = "auto"
    if runtime_mode == "auto":
        return "webhook" if os.getenv("WEBHOOK_URL", "").strip() else "polling"
    return runtime_mode


def _utc_now_iso() -> str:
    """Return an ISO UTC timestamp without microseconds."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _format_duration(seconds: float) -> str:
    """Format a small uptime/duration value for human display."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _health_state(application: Application) -> dict:
    """Get or initialize in-memory runtime health state."""
    state = application.bot_data.get("health")
    if not isinstance(state, dict):
        now = time.time()
        state = {
            "started_at": _utc_now_iso(),
            "started_at_epoch": now,
            "active_jobs": 0,
            "last_job_name": None,
            "last_job_started_at": None,
            "last_job_finished_at": None,
            "last_job_duration_ms": None,
            "last_error": None,
            "last_error_at": None,
            "last_success_at": None,
        }
        application.bot_data["health"] = state
    return state


def _mark_job_start(context: ContextTypes.DEFAULT_TYPE, job_name: str) -> tuple[str, float]:
    """Track the beginning of a long-running bot job."""
    state = _health_state(context.application)
    state["active_jobs"] = int(state.get("active_jobs", 0)) + 1
    state["last_job_name"] = job_name
    state["last_job_started_at"] = _utc_now_iso()
    return job_name, time.monotonic()


def _mark_job_finish(
    context: ContextTypes.DEFAULT_TYPE,
    token: tuple[str, float],
    *,
    success: bool = True,
    error: Exception | None = None,
) -> None:
    """Track the end of a long-running bot job."""
    state = _health_state(context.application)
    state["active_jobs"] = max(0, int(state.get("active_jobs", 0)) - 1)
    state["last_job_name"] = token[0]
    state["last_job_finished_at"] = _utc_now_iso()
    state["last_job_duration_ms"] = int((time.monotonic() - token[1]) * 1000)
    if success:
        state["last_success_at"] = state["last_job_finished_at"]
    elif error is not None:
        state["last_error_at"] = _utc_now_iso()
        state["last_error"] = f"{type(error).__name__}: {error}"


# ── Rate limiting ─────────────────────────────────────────────────────────────

_RATE_LIMITS = {
    "pick": 10,     # seconds
    "check": 10,
    "refresh": 60,
}


def _check_rate_limit(context: ContextTypes.DEFAULT_TYPE, command: str) -> str | None:
    """Return a user-facing message if rate-limited, else None (allowed)."""
    cooldown = _RATE_LIMITS.get(command, 10)
    ts_key = f"_rl_{command}"
    now = time.time()
    last = context.user_data.get(ts_key, 0)
    remaining = cooldown - (now - last)
    if remaining > 0:
        return f"Please wait {int(remaining)}s before using /{command} again."
    context.user_data[ts_key] = now
    return None



async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show runtime health for the currently running bot instance."""
    state = _health_state(context.application)
    budget = await asyncio.to_thread(get_api_budget)
    uptime = _format_duration(time.time() - float(state.get("started_at_epoch", time.time())))
    msg = (
        "SportyBot Health\n\n"
        f"Runtime Mode: {_runtime_mode_from_env()}\n"
        f"Uptime: {uptime}\n"
        f"Concurrent Updates: {CONCURRENT_UPDATES}\n"
        f"Active Jobs: {state.get('active_jobs', 0)}\n"
        f"Last Job: {state.get('last_job_name') or 'None'}\n"
        f"Last Success: {state.get('last_success_at') or 'None'}\n"
        f"Last Error: {state.get('last_error') or 'None'}\n"
        f"Last Error At: {state.get('last_error_at') or 'None'}\n"
        f"API Budget: {budget['remaining']}/{budget['limit']} remaining"
    )
    await update.message.reply_text(msg)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions and retain a lightweight health snapshot."""
    err = context.error or RuntimeError("Unknown bot error")
    state = _health_state(context.application)
    state["last_error_at"] = _utc_now_iso()
    state["last_error"] = f"{type(err).__name__}: {err}"
    logger.exception("Unhandled exception while processing update", exc_info=err)

    try:
        if isinstance(update, Update):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Something went wrong on my side. Please try that again in a moment."
                )
            elif update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Something went wrong on my side. Please try that again in a moment.",
                )
    except Exception:
        logger.exception("Failed to send fallback error message")
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Refresh cached data — clears SportyBet event index + analysis cache, re-fetches."""
    rl_msg = _check_rate_limit(context, "refresh")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return
    job_token = _mark_job_start(context, "refresh")
    config = load_user_config(chat_id=update.effective_chat.id)
    leagues = config.get("leagues", [])

    if not leagues:
        _mark_job_finish(context, job_token)
        await update.message.reply_text("No leagues configured. Run /leagues first.")
        return

    await update.message.reply_text(
        f"🔄 Clearing caches and re-fetching from SportyBet for {len(leagues)} leagues..."
    )

    # 1. Clear SportyBet event index
    await asyncio.to_thread(clear_sportybet_cache)

    # 2. Clear analysis cache
    from pathlib import Path
    cache_dir = Path(__file__).resolve().parent.parent / ".cache" / "analysis"
    cleared = 0
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink()
            cleared += 1

    # 3. Re-fetch from SportyBet with user's league filter
    from core.config import LEAGUE_SPORTYBET_NAMES
    allowed_tournaments = []
    for lid in leagues:
        allowed_tournaments.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    try:
        events = await asyncio.to_thread(
            fetch_all_events,
            max_pages=15,
            allowed_tournaments=allowed_tournaments or None,
        )
    except Exception as e:
        _mark_job_finish(context, job_token, success=False, error=e)
        logger.exception("Refresh fetch failed")
        await update.message.reply_text("Refresh failed — could not reach SportyBet. Try again shortly.")
        return

    # 4. Rebuild event index
    await asyncio.to_thread(build_event_index, True)

    league_display = ", ".join(LEAGUE_NAMES.get(lid, str(lid)) for lid in leagues)
    msg = (
        f"✅ Refresh complete\n\n"
        f"Leagues: {league_display}\n"
        f"Events fetched: {len(events)}\n"
        f"Analysis cache cleared ({cleared} files)\n\n"
        f"Run /pick to get fresh selections."
    )
    _mark_job_finish(context, job_token)
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
    from core.config import TIMEFRAME_PRESETS

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


async def post_init(application: Application):
    """Set the bot commands menu after startup."""
    commands = [
        BotCommand("start", "Welcome & help"),
        BotCommand("pick", "Get picks for target odds"),
        BotCommand("check", "Analyze SportyBet booking codes"),
        BotCommand("split", "Split a large ticket into smaller ones"),
        BotCommand("sort", "Reorder an existing booking code"),
        BotCommand("chat", "Chat with the AI analyst"),
        BotCommand("settings", "Leagues, timeframe & pick settings"),
        BotCommand("budget", "Check API calls remaining"),
        BotCommand("status", "Current bot config"),
        BotCommand("health", "Runtime health & recent errors"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot command menu registered.")

    # Pre-warm the SportyBet event index so cached picks can be booked immediately
    try:
        await asyncio.to_thread(build_event_index, force=False)
        logger.info("SportyBet event index pre-warmed on startup.")
    except Exception as e:
        logger.warning(f"Failed to pre-warm event index on startup: {e}")
async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback handler for free-text messages not caught by commands or conversations."""
    user_text = update.message.text
    if not user_text or not user_text.strip():
        return ConversationHandler.END

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
            "Sorry, I couldn't process that. Try /pick, /check, or /chat."
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
        await update.message.reply_text(
            f"{reply}\n\nUse /settings to configure your leagues."
        )
        return ConversationHandler.END

    elif intent == "timeframe":
        await update.message.reply_text(
            f"{reply}\n\nUse /settings to configure your timeframe."
        )
        return ConversationHandler.END

    elif intent == "split":
        # Route to chat agent — it has the split_ticket tool
        chat_id = update.effective_chat.id
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            response = await chat_agent.agent_respond(user_text, context.user_data, chat_id)
            if response and response.strip():
                await send_long_message(update, response)
            else:
                await update.message.reply_text(
                    "I couldn't process that. Try /split with a booking code."
                )
        except Exception as e:
            logger.warning(f"Split agent error: {e}")
            await update.message.reply_text(
                "Something went wrong. Try /split with a booking code instead."
            )
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
        # intent == "chat" or unknown — route to agent for richer response
        chat_id = update.effective_chat.id
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            response = await chat_agent.agent_respond(user_text, context.user_data, chat_id)
            if response and response.strip():
                await send_long_message(update, response)
            elif reply and reply.strip():
                await update.message.reply_text(reply)
            else:
                await update.message.reply_text(
                    "I'm here! Try /pick for picks, /check to analyze a code, or /chat to ask me anything."
                )
        except Exception as e:
            logger.warning(f"Agent error: {e}")
            fallback = reply if reply and reply.strip() else "Something went wrong. Try /pick or /check."
            await update.message.reply_text(fallback)
        return ConversationHandler.END
async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chat command — explicit agent mode."""
    rl_msg = _check_rate_limit(context, "pick")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return
    user_text = update.message.text or ""
    if user_text.startswith("/chat"):
        user_text = user_text[5:].strip()
    if not user_text:
        await update.message.reply_text(
            "Hey! Ask me anything about football or betting.\n"
            "I can analyze matches, build tickets, check codes, and more.\n\n"
            "Try: /chat what matches are on today?"
        )
        return
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass
    try:
        response = await chat_agent.agent_respond(user_text, context.user_data, chat_id)
        if response and response.strip():
            await send_long_message(update, response)
        else:
            await update.message.reply_text("I couldn't generate a response. Try rephrasing or use /pick.")
    except Exception as e:
        logger.warning(f"Chat agent error: {e}")
        await update.message.reply_text("I hit a snag processing that. Try again in a moment.")

def main():
    if not TELEGRAM_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        print("1. Create a bot via @BotFather on Telegram")
        print("2. Add TELEGRAM_BOT_TOKEN=your_token to .env")
        sys.exit(1)

    runtime_mode = _runtime_mode_from_env()
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .persistence(persistence)
        .concurrent_updates(CONCURRENT_UPDATES)
        .post_init(post_init)
        .build()
    )
    _health_state(app)
    app.bot_data["runtime_mode"] = runtime_mode

    # /split conversation handler
    split_conv = ConversationHandler(
        entry_points=[CommandHandler("split", split_start)],
        states={
            SPLIT_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, split_receive_code),
            ],
            SPLIT_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, split_receive_count),
            ],
            SPLIT_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, split_receive_targets),
            ],
            SPLIT_CONFIRM: [
                CallbackQueryHandler(split_confirm_callback, pattern=r"^split_(book_\d+|book_all|cancel)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", split_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )
    app.add_handler(split_conv)

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
                CallbackQueryHandler(check_review_callback, pattern=r"^(pick_exclude_\d+|pick_swap_\d+|pick_change_\d+|pick_dialogue_\d+|pick_mkt_.+|pick_reshuffle|pick_confirm|pick_explain_all)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, check_edit_text),
            ],
            GAME_DIALOGUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, game_dialogue_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", check_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )
    app.add_handler(check_conv)

    sort_conv = ConversationHandler(
        entry_points=[CommandHandler("sort", cmd_sort)],
        states={
            SORT_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sort_receive_code),
            ],
            SORT_MODE: [
                CallbackQueryHandler(sort_receive_mode, pattern=r"^sort_mode_"),
            ],
        },
        fallbacks=[CommandHandler("cancel", sort_cancel)],
        allow_reentry=True,
    )
    app.add_handler(sort_conv)

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
                CallbackQueryHandler(pick_review_callback, pattern=r"^(pick_exclude_\d+|pick_swap_\d+|pick_change_\d+|pick_dialogue_\d+|pick_mkt_.+|pick_reshuffle|pick_confirm|pick_explain_all|pick_bundle_.+)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, pick_edit_text),
            ],
            GAME_DIALOGUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, game_dialogue_handler),
            ],
        },
        fallbacks=[CommandHandler("cancel", pick_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )
    app.add_handler(pick_conv)

    # Other commands (leagues + timeframe are now entry points of strat_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("budget", cmd_budget))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_error_handler(error_handler)

    # Strategy/settings conversation handler (leagues + timeframe + strategy all in one)
    strat_conv = ConversationHandler(
        entry_points=[
            CommandHandler("strategy", cmd_strategy),
            CommandHandler("settings", cmd_strategy),
            CommandHandler("leagues", cmd_leagues),
            CommandHandler("timeframe", cmd_timeframe),
        ],
        states={
            STRAT_CUSTOM_CONFIDENCE: [
                CallbackQueryHandler(callback_strat_custom_confidence, pattern=r"^strat_conf_"),
                CallbackQueryHandler(callback_strategy_select, pattern=r"^strat_(conf_menu|mkt_menu|odds_menu|leagues_menu|timeframe_menu|back|done|conservative|balanced|aggressive|overs_only|btts_mix|favourites)$"),
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
            STRAT_LEAGUES: [
                CallbackQueryHandler(callback_strat_leagues, pattern=r"^league_"),
            ],
            STRAT_TIMEFRAME: [
                CallbackQueryHandler(callback_strat_timeframe, pattern=r"^strat_(tf_|back)"),
            ],
        },
        fallbacks=[CommandHandler("cancel", strategy_cancel)],
        allow_reentry=True,
        per_message=False,
        conversation_timeout=300,
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
