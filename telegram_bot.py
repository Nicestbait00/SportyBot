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
import chat_agent
import ticket_engine
from booking_service import fetch_booking_code, parse_outcomes
from analysis_service import (
    add_extended_picks as _add_extended_picks,
    add_thin_data_safe_picks as _add_thin_data_safe_picks,
    score_fixture as _score_fixture,
    verdict as _verdict,
)
from ticket_engine import (
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
from sportybet_events import (
    build_event_index, clear_cache as clear_sportybet_cache,
    fetch_all_events, filter_events, find_event,
    build_booking_selection, create_booking_code,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CONCURRENT_UPDATES = max(1, int(os.getenv("CONCURRENT_UPDATES", "8") or 8))
REFRESH_CONCURRENCY = max(1, int(os.getenv("REFRESH_CONCURRENCY", "4") or 4))
BOOKING_FETCH_CONCURRENCY = max(1, int(os.getenv("BOOKING_FETCH_CONCURRENCY", "4") or 4))
TEAM_RESULTS_FETCH_CONCURRENCY = max(1, int(os.getenv("TEAM_RESULTS_FETCH_CONCURRENCY", "2") or 2))

# Conversation states for /check flow
CHECK_CODES, CHECK_TARGET_ODDS, CHECK_EXCLUDE, CHECK_CONFIRM, CHECK_EXPAND, CHECK_REVIEW = range(6)
# Conversation states for /pick flow
PICK_TYPE, PICK_COUNT, PICK_ODDS, PICK_MODE, PICK_REVIEW, GAME_DIALOGUE = range(10, 16)
# Conversation states for /strategy custom flow
STRAT_CUSTOM_CONFIDENCE, STRAT_CUSTOM_MARKETS, STRAT_CUSTOM_OVER, STRAT_CUSTOM_MIN_ODDS, STRAT_LEAGUES, STRAT_TIMEFRAME = range(20, 26)


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Scoring/analysis functions are in analysis_service.py ────────────────────
# Imported as _add_extended_picks, _add_thin_data_safe_picks, _score_fixture, _verdict


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


def _build_league_keyboard(active: set[int], in_settings: bool = False) -> InlineKeyboardMarkup:
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
        if len(category_row) == 2:
            buttons.append(category_row)
            category_row = []
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

    done_btn = InlineKeyboardButton("⬅️ Back", callback_data="league_done") if in_settings else InlineKeyboardButton("✅ Done", callback_data="league_done")
    buttons.append(
        [
            InlineKeyboardButton("🧹 Clear All", callback_data="league_clear_all"),
            done_btn,
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
        "_team_results_cache",
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
    """Receive target odds as text."""
    try:
        target = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Send a number (e.g. 10) or /cancel.")
        return PICK_ODDS
    if target < 1.5 or target > 500:
        await update.message.reply_text("Target odds must be between 1.5 and 500. Try again or /cancel.")
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

        # Send typing indicator while fetching
        try:
            await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")
        except Exception:
            pass

        # Step 1: Get fixtures directly from SportyBet (has event IDs + real odds)
        # Build tournament name filter from user's league config
        from config import LEAGUE_SPORTYBET_NAMES
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
    if ticket_mode == "unique":
        bundle, profile = _generate_unique_bundle(all_scored, ticket_count, target, pick_cfg, excluded, shuffle_seed)
        return bundle, profile, False

    bundle, profile, reused = _generate_dynamic_bundle(all_scored, ticket_count, target, pick_cfg, excluded, None, shuffle_seed)
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

    lines = [
        f"🎫 Bundle ready: {len(bundle)} ticket(s)",
        f"Mode: {req.get('ticket_mode', 'dynamic').title()}",
        f"Target per ticket: ~{float(req.get('target_odds') or 0):.0f} odds",
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
    from config import LEAGUE_NAMES as _LN, TIMEFRAME_PRESETS as _TP

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
    """Cancel the pick flow."""
    _clear_pick_runtime(context)
    await update.message.reply_text("Pick cancelled.")
    return ConversationHandler.END


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
    cache_dir = Path(__file__).resolve().parent / ".cache" / "analysis"
    cleared = 0
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink()
            cleared += 1

    # 3. Re-fetch from SportyBet with user's league filter
    from config import LEAGUE_SPORTYBET_NAMES
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


def _settings_hub_content(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the settings hub text and keyboard. Reused from multiple places."""
    min_conf = config.get("min_confidence", 75)
    min_odds = config.get("min_odds", 1.05)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
    active_leagues = config.get("leagues", [])
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in active_leagues[:5]]
    league_summary = ", ".join(league_names) or "None"
    if len(active_leagues) > 5:
        league_summary += f" +{len(active_leagues) - 5} more"
    from config import TIMEFRAME_PRESETS
    tf_key = config.get("timeframe", "7days")
    tf_label = TIMEFRAME_PRESETS.get(tf_key, {}).get("label", tf_key)

    text = (
        "⚙️ *Settings*\n\n"
        f"📋 Leagues: {league_summary}\n"
        f"⏰ Timeframe: {tf_label}\n"
        f"🎯 Confidence: ≥{min_conf}%\n"
        f"📊 Markets: {len(enabled)} enabled\n"
        f"💰 Min Odds: ≥{min_odds}\n\n"
        "Tap to adjust:"
    )
    buttons = [
        [
            InlineKeyboardButton("📋 Leagues", callback_data="strat_leagues_menu"),
            InlineKeyboardButton("⏰ Timeframe", callback_data="strat_timeframe_menu"),
        ],
        [InlineKeyboardButton(f"🎯 Confidence ({min_conf}%)", callback_data="strat_conf_menu")],
        [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
        [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
        [InlineKeyboardButton("✅ Done", callback_data="strat_done")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show unified settings panel — leagues, timeframe, confidence, markets, min odds."""
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    config = load_user_config(chat_id=chat_id)
    text, markup = _settings_hub_content(config)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
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

    elif data == "strat_leagues_menu":
        active = set(config.get("leagues", []))
        await query.edit_message_text(
            _format_league_selection_text(active),
            reply_markup=_build_league_keyboard(active, in_settings=True),
        )
        return STRAT_LEAGUES

    elif data == "strat_timeframe_menu":
        from config import TIMEFRAME_PRESETS
        current = config.get("timeframe", "7days")
        buttons = [
            [
                InlineKeyboardButton(f"{'✅ ' if current == 'today' else ''}Today", callback_data="strat_tf_today"),
                InlineKeyboardButton(f"{'✅ ' if current == 'tomorrow' else ''}Tomorrow", callback_data="strat_tf_tomorrow"),
                InlineKeyboardButton(f"{'✅ ' if current == 'weekend' else ''}Weekend", callback_data="strat_tf_weekend"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == '7days' else ''}Next 7 Days", callback_data="strat_tf_7days"),
                InlineKeyboardButton(f"{'✅ ' if current == '14days' else ''}Next 14 Days", callback_data="strat_tf_14days"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")],
        ]
        current_label = TIMEFRAME_PRESETS.get(current, {}).get("label", current)
        await query.edit_message_text(
            f"⏰ Timeframe\nCurrent: {current_label}\n\nHow far ahead should /pick scan?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_TIMEFRAME

    elif data == "strat_back":
        # Back to main settings hub
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    elif data == "strat_done":
        min_conf = config.get("min_confidence", 75)
        min_odds = config.get("min_odds", 1.05)
        enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
        active_leagues = config.get("leagues", [])
        league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in active_leagues]
        from config import TIMEFRAME_PRESETS
        tf_label = TIMEFRAME_PRESETS.get(config.get("timeframe", "7days"), {}).get("label", "7 days")
        await query.edit_message_text(
            f"✅ Settings saved!\n\n"
            f"Leagues: {', '.join(league_names) or 'None'}\n"
            f"Timeframe: {tf_label}\n"
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

    # Show updated settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Confidence set to {conf}%\n\n" + text,
        reply_markup=markup,
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

    # Show updated settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Min Odds set to {min_odds}\n\n" + text,
        reply_markup=markup,
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league toggles within the settings ConversationHandler."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data == "league_done":
        # Back to settings hub
        config = load_user_config(chat_id=chat_id)
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    # Reuse the existing toggle logic
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
    elif data.startswith("league_toggle_"):
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
        reply_markup=_build_league_keyboard(active, in_settings=True),
    )
    return STRAT_LEAGUES


async def callback_strat_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe selection within settings ConversationHandler."""
    from config import TIMEFRAME_PRESETS

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "strat_back":
        config = load_user_config(chat_id=update.effective_chat.id)
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    preset_key = data.replace("strat_tf_", "")
    preset = TIMEFRAME_PRESETS.get(preset_key)
    if not preset:
        return STRAT_TIMEFRAME

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["timeframe"] = preset_key
    save_user_config(config, chat_id=chat_id)

    # Return to settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Timeframe set to {preset['label']}\n\n" + text,
        reply_markup=markup,
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def strategy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the settings flow."""
    context.user_data.pop("custom_strategy", None)
    await update.message.reply_text("Settings cancelled.")
    return ConversationHandler.END


async def cmd_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect to unified settings."""
    return await cmd_strategy(update, context)


async def cmd_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect to unified settings."""
    return await cmd_strategy(update, context)


async def callback_league_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league toggle from standalone /leagues (outside settings)."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data
    if data == "league_done":
        config = load_user_config(chat_id=chat_id)
        names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config["leagues"]]
        await query.edit_message_text(
            f"Leagues set: {', '.join(names) or 'None'}\n\n"
            "Now run /pick to get selections.",
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


async def callback_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe from standalone /timeframe (outside settings)."""
    from config import TIMEFRAME_PRESETS

    query = update.callback_query
    await query.answer()
    preset_key = query.data.replace("tf_", "")
    preset = TIMEFRAME_PRESETS.get(preset_key)
    if not preset:
        await query.edit_message_text("Unknown timeframe. Try /settings.")
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
    rl_msg = _check_rate_limit(context, "check")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return ConversationHandler.END
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
    job_token = _mark_job_start(context, "check_codes")
    job_success = False
    job_error = None
    await update.message.reply_text(f"🔍 Fetching {len(codes)} code(s): {', '.join(codes)}...")

    all_picks = []
    valid_codes = []
    failed_codes = []

    sem = asyncio.Semaphore(min(BOOKING_FETCH_CONCURRENCY, max(len(codes), 1)))

    async def _fetch_code(code: str) -> tuple[str, dict | None]:
        async with sem:
            return code, await asyncio.to_thread(fetch_booking_code, code)

    try:
        fetched_codes = await asyncio.gather(*[_fetch_code(code) for code in codes])
    except Exception as e:
        job_error = e
        logger.exception("Booking code fetch failed")
        _mark_job_finish(context, job_token, success=False, error=job_error)
        await update.message.reply_text("I couldn't fetch those booking codes right now. Please try again shortly.")
        return ConversationHandler.END

    for code, data in fetched_codes:
        if data:
            picks = parse_outcomes(data)
            all_picks.extend(picks)
            valid_codes.append(code)
        else:
            failed_codes.append(code)

    if failed_codes:
        await update.message.reply_text(
            f"Could not fetch: {', '.join(failed_codes)}\n"
            "These codes may be expired, invalid, or the SportyBet API may be down."
        )

    if not all_picks:
        job_success = True
        _mark_job_finish(context, job_token, success=True)
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
            home_results, away_results = await asyncio.gather(
                _get_team_results_cached(context, home_name, count=10),
                _get_team_results_cached(context, away_name, count=10),
            )

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
            logger.exception("Check analysis failed for %s vs %s", home_name, away_name)
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
    job_success = True

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
                f"   Score: {best_match['market']}: {best_match['pick']} @ {best_match.get('odds', 0):.2f} [{conf}%]"
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

    _mark_job_finish(context, job_token, success=job_success, error=job_error)
    return CHECK_EXPAND


async def check_expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle expand choice: add SportyBet league picks (scored via /pick's pipeline) or code only."""
    job_token = _mark_job_start(context, "check_expand")
    job_success = False
    job_error = None
    query = update.callback_query
    await query.answer()

    data = query.data
    all_scored = context.user_data.get("check_all_scored", [])

    if data == "check_expand_no":
        await query.edit_message_text("🎯 Got it — working with your code games.")
        job_success = True
        _mark_job_finish(context, job_token, success=True)
        return await _show_check_target_odds(query.message, context, edit=False)

    # data == "check_expand_yes" — scan SportyBet events for user's leagues
    # Uses the SAME scoring as /pick (SportyBet events + scorer.py)
    config = load_user_config(chat_id=update.effective_chat.id)
    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")

    if not leagues:
        await query.edit_message_text("No leagues set up yet — use /leagues to add some.\nWorking with your code games for now.")
        job_success = True
        _mark_job_finish(context, job_token, success=True)
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

    events = await asyncio.to_thread(fetch_all_events, max_pages=15, allowed_tournaments=tournament_filters)
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
            home_results, away_results = await asyncio.gather(
                _get_team_results_cached(context, home_name, count=10),
                _get_team_results_cached(context, away_name, count=10),
            )
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
            logger.exception("Check expand error for %s vs %s", home_name, away_name)

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

    job_success = True
    _mark_job_finish(context, job_token, success=job_success, error=job_error)
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
    if target < 1.5 or target > 500:
        await update.message.reply_text("Target odds must be between 1.5 and 500. Try again or /cancel.")
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

    if data.startswith("pick_dialogue_"):
        idx = int(data.replace("pick_dialogue_", ""))
        combo = context.user_data.get("pick_combo", [])
        if idx < 0 or idx >= len(combo):
            return CHECK_REVIEW
        context.user_data["dialogue_pick_idx"] = idx
        pick = combo[idx]
        await _explain_picks(query.message, context, combo, [idx + 1])
        await query.message.reply_text(
            f"💬 Let's talk about: {pick['home']} vs {pick['away']}\n"
            f"Current: {pick['market']}: {pick.get('pick', '')} @ {pick.get('odds', 0):.2f}\n\n"
            "Suggest a different market (e.g. 'what about over 1.5?') or type 'done' to go back."
        )
        return GAME_DIALOGUE

    if data.startswith("pick_change_") or data.startswith("pick_mkt_") or data.startswith("pick_swap_"):
        # Delegate to pick's review handler, then route back to check
        result = await pick_review_callback(update, context)
        # Sync pick_combo back to check_combo after market change
        context.user_data["check_combo"] = context.user_data.get("pick_combo", [])
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
        BotCommand("pick", "Get picks for target odds"),
        BotCommand("check", "Analyze SportyBet booking codes"),
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
        # intent == "chat" or unknown — route to agent for richer response
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            response = await chat_agent.agent_respond(user_text, context.user_data, chat_id)
            await send_long_message(update, response)
        except Exception as e:
            logger.warning(f"Agent error: {e}")
            await update.message.reply_text(reply)  # fall back to Gemini's short reply
        return ConversationHandler.END


async def cmd_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chat command — explicit agent mode."""
    rl_msg = _check_rate_limit(context, "pick")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return
    user_text = update.message.text
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
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        response = await chat_agent.agent_respond(user_text, context.user_data, chat_id)
        await send_long_message(update, response)
    except Exception as e:
        logger.warning(f"Chat agent error: {e}")
        await update.message.reply_text("I hit a snag processing that. Try again in a moment.")


# ── Main ─────────────────────────────────────────────────────────────────────

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
