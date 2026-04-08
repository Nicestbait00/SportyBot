"""SportyBot /split conversation handler.

Handles ticket splitting: paste code -> set count -> set targets -> confirm -> book.
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
from services import ticket_splitter
from bot.formatters import (
    format_picks_review,
    send_long_message,
    _sort_booking_picks,
    _kickoff_ms_from_pick,
    _format_pick_kickoff,
)

logger = logging.getLogger(__name__)

# Conversation states
SPLIT_CODE, SPLIT_COUNT, SPLIT_TARGETS, SPLIT_CONFIRM = range(30, 34)

BOOKING_FETCH_CONCURRENCY = 4

from bot.handlers._shared import _check_rate_limit


async def split_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the split flow. Accepts inline code or prompts for one."""
    rl_msg = _check_rate_limit(context, "check")
    if rl_msg:
        await update.message.reply_text(rl_msg)
        return ConversationHandler.END
    context.user_data["chat_id"] = update.effective_chat.id
    args = context.args or []

    if args:
        raw = " ".join(args)
        code = raw.strip().split(",")[0].strip().upper()
        return await _split_fetch_code(update, context, code)

    await update.message.reply_text(
        "🔀 *Ticket Splitter*\n\n"
        "Send me a SportyBet booking code to split.\n\n"
        "Example: `G0S7HA`\n\n"
        "Send /cancel to exit.",
        parse_mode="Markdown",
    )
    return SPLIT_CODE


async def split_receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the booking code to split."""
    raw = update.message.text.strip().upper()
    code = raw.split(",")[0].strip()
    if not code:
        await update.message.reply_text("No valid code found. Try again or /cancel.")
        return SPLIT_CODE
    return await _split_fetch_code(update, context, code)


async def _split_fetch_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    """Fetch and parse a booking code for splitting."""
    await update.message.reply_text(f"🔍 Fetching code `{code}`...", parse_mode="Markdown")

    try:
        data = await asyncio.to_thread(fetch_booking_code, code)
    except Exception:
        data = None

    if not data:
        await update.message.reply_text(
            f"Could not fetch code `{code}`. It may be expired or invalid.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    picks = parse_outcomes(data)
    # Only keep pending/upcoming picks — ended games can't be re-booked
    active_picks = [p for p in picks if p.get("match_status") != "Ended"]

    if len(active_picks) < 2:
        await update.message.reply_text(
            f"This ticket only has {len(active_picks)} active pick(s). "
            "Need at least 2 to split."
        )
        return ConversationHandler.END

    # Store picks for later steps
    context.user_data["split_picks"] = active_picks
    context.user_data["split_code"] = code

    # Calculate total odds
    total_odds = 1.0
    for p in active_picks:
        total_odds *= float(p.get("odds", 1.0))

    # Show summary
    lines = [f"📋 *Code {code}* — {len(active_picks)} active picks"]
    lines.append(f"💰 Total odds: *{total_odds:.2f}*\n")

    for i, p in enumerate(active_picks, 1):
        odds = float(p.get("odds", 1.0))
        lines.append(f"{i}. {p['home']} vs {p['away']}")
        lines.append(f"   {p['market']}: {p['pick']} @ {odds:.2f}")

    if len(picks) > len(active_picks):
        ended = len(picks) - len(active_picks)
        lines.append(f"\n⚠️ {ended} ended game(s) excluded from split.")

    lines.append(f"\n🔢 *Split into how many tickets?* (2-{min(5, len(active_picks))})")

    await send_long_message(update, "\n".join(lines))
    return SPLIT_COUNT


async def split_receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the number of tickets to split into."""
    raw = update.message.text.strip()
    active_picks = context.user_data.get("split_picks", [])
    max_splits = min(ticket_splitter.MAX_SPLITS, len(active_picks))

    try:
        count = int(raw)
    except ValueError:
        await update.message.reply_text(
            f"Please enter a number between 2 and {max_splits}."
        )
        return SPLIT_COUNT

    if count < 2 or count > max_splits:
        await update.message.reply_text(
            f"Please enter a number between 2 and {max_splits}."
        )
        return SPLIT_COUNT

    context.user_data["split_count"] = count

    total_odds = 1.0
    for p in active_picks:
        total_odds *= float(p.get("odds", 1.0))

    await update.message.reply_text(
        f"Got it — *{count} tickets*.\n\n"
        f"Now send me the *target odds* for each ticket, separated by commas.\n"
        f"Your total is *{total_odds:.2f}* odds across {len(active_picks)} picks.\n\n"
        f"Example: `3,8,5` or `2,20,50`\n\n"
        f"The targets don't need to multiply to the total — "
        f"I'll distribute proportionally and get as close as possible.",
        parse_mode="Markdown",
    )
    return SPLIT_TARGETS


async def split_receive_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the target odds per ticket and perform the split."""
    raw = update.message.text.strip()
    count = context.user_data.get("split_count", 2)
    picks = context.user_data.get("split_picks", [])

    # Parse comma-separated target odds
    parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
    targets = []
    for part in parts:
        try:
            t = float(part)
            targets.append(t)
        except ValueError:
            await update.message.reply_text(
                f"Couldn't parse `{part}` as a number. "
                f"Send {count} odds values separated by commas.\n"
                f"Example: `3,8,5`",
                parse_mode="Markdown",
            )
            return SPLIT_TARGETS

    if len(targets) != count:
        await update.message.reply_text(
            f"You said {count} tickets but gave {len(targets)} target(s). "
            f"Send exactly {count} values separated by commas.",
        )
        return SPLIT_TARGETS

    # Validate
    err = ticket_splitter.validate_split_request(picks, targets)
    if err:
        await update.message.reply_text(f"⚠️ {err}\nTry different targets.")
        return SPLIT_TARGETS

    # Perform the split
    await update.message.reply_text("🔀 Splitting ticket...")
    result = ticket_splitter.split_ticket(picks, targets)
    context.user_data["split_result"] = result

    # Show summary
    summary = ticket_splitter.format_split_summary(result)
    await send_long_message(update, summary)

    # Build action buttons
    buttons = []
    for i, t in enumerate(result["tickets"]):
        buttons.append([
            InlineKeyboardButton(
                f"📋 Book Ticket {t['ticket_num']} ({t['actual_odds']:.2f} odds)",
                callback_data=f"split_book_{i}",
            )
        ])

    buttons.append([InlineKeyboardButton("📋 Book All Tickets", callback_data="split_book_all")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="split_cancel")])

    await update.message.reply_text(
        "What would you like to do?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SPLIT_CONFIRM


async def split_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle split confirmation buttons: book individual, book all, cancel."""
    query = update.callback_query
    await query.answer()
    data = query.data
    result = context.user_data.get("split_result")

    if not result:
        await query.edit_message_text("Session expired. Use /split to start again.")
        return ConversationHandler.END

    # ── Cancel ──
    if data == "split_cancel":
        context.user_data.pop("split_result", None)
        context.user_data.pop("split_picks", None)
        await query.edit_message_text("Split cancelled.")
        return ConversationHandler.END

    # ── Book a single ticket ──
    if data.startswith("split_book_"):
        idx_str = data.replace("split_book_", "")

        if idx_str == "all":
            await query.edit_message_text("📋 Booking all split tickets...")
            booked = []
            failed = []
            for i, t in enumerate(result["tickets"]):
                code = await _book_split_ticket(t)
                if code:
                    booked.append((i, t, code))
                else:
                    failed.append((i, t))

            lines = ["🎉 *Booking Results*\n"]
            for i, t, code in booked:
                lines.append(
                    f"✅ Ticket {t['ticket_num']}: `{code}` "
                    f"({t['actual_odds']:.2f} odds, {t['pick_count']} picks)"
                )
            for i, t in failed:
                lines.append(
                    f"❌ Ticket {t['ticket_num']}: booking failed "
                    f"({t['actual_odds']:.2f} odds, {t['pick_count']} picks)"
                )

            await send_long_message(update.callback_query, "\n".join(lines))
            context.user_data.pop("split_result", None)
            context.user_data.pop("split_picks", None)
            return ConversationHandler.END

        else:
            idx = int(idx_str)
            tickets = result["tickets"]
            if 0 <= idx < len(tickets):
                t = tickets[idx]
                await query.edit_message_text(
                    f"📋 Booking Ticket {t['ticket_num']}..."
                )
                code = await _book_split_ticket(t)
                if code:
                    await query.message.reply_text(
                        f"✅ *Ticket {t['ticket_num']}* booked!\n"
                        f"Code: `{code}`\n"
                        f"Odds: {t['actual_odds']:.2f} | Picks: {t['pick_count']}",
                        parse_mode="Markdown",
                    )
                else:
                    await query.message.reply_text(
                        f"❌ Booking failed for Ticket {t['ticket_num']}. "
                        f"Some picks may have expired or the SportyBet API is down."
                    )

                # Re-show remaining buttons
                remaining = [
                    (i, tk) for i, tk in enumerate(tickets)
                    if i != idx
                ]
                if remaining:
                    buttons = []
                    for i, tk in remaining:
                        buttons.append([
                            InlineKeyboardButton(
                                f"📋 Book Ticket {tk['ticket_num']} ({tk['actual_odds']:.2f} odds)",
                                callback_data=f"split_book_{i}",
                            )
                        ])
                    buttons.append([InlineKeyboardButton("❌ Done", callback_data="split_cancel")])
                    await query.message.reply_text(
                        "Book another?",
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                    return SPLIT_CONFIRM
                else:
                    context.user_data.pop("split_result", None)
                    context.user_data.pop("split_picks", None)
                    return ConversationHandler.END

    return SPLIT_CONFIRM


async def _book_split_ticket(ticket: dict) -> str | None:
    """Book a single split ticket via SportyBet API. Returns booking code or None."""
    selections = []
    for pick in ticket["picks"]:
        sel = pick.get("selection", {})
        if not sel or not sel.get("eventId"):
            continue
        selections.append({
            "eventId": sel["eventId"],
            "marketId": str(sel.get("marketId", "")),
            "outcomeId": str(sel.get("outcomeId", "")),
            "specifier": sel.get("specifier", ""),
        })

    if not selections:
        return None

    try:
        code = await asyncio.to_thread(create_booking_code, selections)
        return code
    except Exception as e:
        logger.warning(f"Split ticket booking failed: {e}")
        return None


async def split_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
