"""SportyBot /sort conversation handler.

Handles booking code re-sorting: paste code -> choose sort mode -> rebook.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from data.sportybet_events import (
    fetch_all_events, filter_events, find_event,
    build_booking_selection, create_booking_code,
)
from data.booking_service import fetch_booking_code, parse_outcomes
from bot.formatters import (
    format_picks_review,
    send_long_message,
    _sort_booking_picks,
    _kickoff_ms_from_pick,
    _format_pick_kickoff,
)
from bot.handlers._shared import _mark_job_start, _mark_job_finish, _send_or_edit
from bot.handlers.pick import _book_ticket_picks, _clear_sort_runtime

logger = logging.getLogger(__name__)

# Conversation states
SORT_CODE, SORT_MODE = range(40, 42)

BOOKING_FETCH_CONCURRENCY = 4


async def cmd_sort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the standalone ticket sorting flow for an existing booking code."""
    _clear_sort_runtime(context)
    context.user_data["chat_id"] = update.effective_chat.id
    args = context.args or []
    if args:
        code = "".join(args).replace(",", "").strip().upper()
        if code:
            context.user_data["sort_code"] = code
            return await _prompt_sort_mode(update.message)

    await update.message.reply_text(
        "🗂 Send me the SportyBet booking code you want to reorganize.\n"
        "I’ll keep the same picks, sort them, and generate a fresh code."
    )
    return SORT_CODE


async def sort_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a booking code for the /sort flow."""
    code = update.message.text.strip().upper().replace(" ", "")
    if not code:
        await update.message.reply_text("Send a valid booking code or /cancel.")
        return SORT_CODE
    context.user_data["sort_code"] = code
    return await _prompt_sort_mode(update.message)


async def _prompt_sort_mode(message, edit: bool = False):
    """Ask how the ticket should be reordered."""
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("By Date", callback_data="sort_mode_date"),
            InlineKeyboardButton("By Time", callback_data="sort_mode_time"),
        ]
    ])
    await _send_or_edit(
        message,
        "How should I reorganize this ticket?\n"
        "Date = calendar date first, then kickoff.\n"
        "Time = kickoff time-of-day first, then date.",
        reply_markup=buttons,
        edit=edit,
    )
    return SORT_MODE


async def sort_receive_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the requested sort mode for /sort."""
    query = update.callback_query
    await query.answer()
    mode = query.data.replace("sort_mode_", "")
    context.user_data["sort_mode"] = mode
    return await _sort_and_rebook_code(query.message, context, mode, edit=True)


async def _sort_and_rebook_code(message, context, mode: str, edit: bool = False):
    """Fetch a booking code, reorder the same upcoming picks, and rebook them."""
    code = context.user_data.get("sort_code")
    if not code:
        await _send_or_edit(message, "No booking code loaded. Start again with /sort.", edit=edit)
        return ConversationHandler.END

    job_token = _mark_job_start(context, "sort_ticket")
    job_success = False
    job_error = None
    mode_label = "date" if mode == "date" else "time"

    try:
        await _send_or_edit(
            message,
            f"🗂 Fetching `{code}` and reorganizing it by {mode_label}...",
            edit=edit,
            parse_mode="Markdown",
        )

        data = await asyncio.to_thread(fetch_booking_code, code)
        if not data:
            job_success = True
            await message.reply_text("I couldn't fetch that booking code. Double-check it and try again.")
            return ConversationHandler.END

        raw_picks = parse_outcomes(data)
        pending_picks = [dict(p) for p in raw_picks if p.get("match_status") != "Ended"]
        ended_count = len(raw_picks) - len(pending_picks)

        if not pending_picks:
            job_success = True
            await message.reply_text("That ticket has no upcoming picks left to reorganize.")
            return ConversationHandler.END

        async def _enrich_pick(pick: dict) -> dict:
            sporty_event = await asyncio.to_thread(find_event, pick.get("home", ""), pick.get("away", ""))
            if sporty_event:
                pick["_sporty_event"] = sporty_event
                pick["_sort_kickoff_ms"] = int(sporty_event.get("estimateStartTime", 0) or 0)
                pick["event_id"] = sporty_event.get("eventId", pick.get("event_id", ""))
                kick_off = pick.get("_sort_kickoff_ms", 0)
                if kick_off > 0:
                    pick["date"] = datetime.fromtimestamp(kick_off / 1000).strftime("%Y-%m-%d %H:%M")
            else:
                pick["_sort_kickoff_ms"] = 0
            return pick

        enriched_picks = await asyncio.gather(*[_enrich_pick(pick) for pick in pending_picks])
        sorted_picks = _sort_booking_picks(enriched_picks, mode)
        booking_result = await _book_ticket_picks(sorted_picks)

        lines = [
            f"🗂 Sorted ticket from `{code}`",
            f"Mode: {'Date first' if mode == 'date' else 'Time of day first'}",
            "",
        ]
        if ended_count:
            lines.append(f"Skipped ended picks: {ended_count}")
            lines.append("")

        for idx, pick in enumerate(sorted_picks, 1):
            lines.append(f"{idx}. {_format_pick_kickoff(pick)} — {pick.get('home')} vs {pick.get('away')}")
            lines.append(f"   {pick.get('market')}: {pick.get('pick')} @ {pick.get('odds', 0):.2f}")

        lines.append("")
        lines.append(f"Selections rebooked: {booking_result['selection_count']}/{len(sorted_picks)}")
        await send_long_message(message, "\n".join(lines))

        if booking_result["code"]:
            await message.reply_text(
                f"🎫 *Sorted Booking Code:* `{booking_result['code']}`\n\n"
                f"Same picks, reorganized by {mode_label}.",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "⚠️ I reordered the ticket, but SportyBet did not return a new booking code."
            )

        if booking_result["failed"]:
            await message.reply_text(
                "⚠️ Some picks could not be rebooked:\n" + "\n".join(booking_result["failed"])
            )

        job_success = True
        return ConversationHandler.END
    except Exception as e:
        job_error = e
        logger.exception("Sort flow failed")
        await message.reply_text(
            "I hit an error while sorting that ticket. Please try again in a moment."
        )
        return ConversationHandler.END
    finally:
        _mark_job_finish(context, job_token, success=job_success, error=job_error)
        _clear_sort_runtime(context)


async def sort_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
