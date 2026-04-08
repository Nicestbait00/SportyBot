"""SportyBot /check conversation handler.

Handles booking code analysis: paste codes -> review picks -> target odds -> build combo.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from core.config import load_user_config, save_user_config
from data.data_collector import get_api_budget
from data.web_analyzer import get_team_results, _summarize_form
from core.scorer import score_match, cross_check_with_odds
from services.analysis_service import (
    add_extended_picks as _add_extended_picks,
    score_fixture as _score_fixture,
    verdict as _verdict,
)
from services.ticket_engine import (
    pick_key as _pick_key,
    ticket_totals as _ticket_totals,
    pick_selection_score as _pick_selection_score,
    pick_qualifies_for_combo as _pick_qualifies_for_combo,
    make_ticket_entry as _make_ticket_entry,
    build_qualified_pool as _build_qualified_pool,
    select_ticket_from_pool as _select_ticket_from_pool,
)
from data.sportybet_events import (
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
from bot.handlers._shared import _mark_job_start, _mark_job_finish, _send_or_edit
from bot.handlers.pick import (
    GAME_DIALOGUE, pick_review_callback, _explain_picks,
    _show_pick_change_options, _pick_build_combo, _pick_confirm_and_book,
)

logger = logging.getLogger(__name__)

# Rate limiting
_RATE_LIMITS = {"check": 10}


# ── Shared helpers ──

def _check_rate_limit(context, command: str) -> str | None:
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

_TEAM_RESULTS_CACHE = {}

async def _get_team_results_cached(context, team_name: str, *, count: int = 10) -> list:
    """Reuse team results within one bot flow."""
    key = f"{team_name.strip().lower()}::{count}"
    if key in _TEAM_RESULTS_CACHE:
        return _TEAM_RESULTS_CACHE[key]
    results = await asyncio.to_thread(get_team_results, team_name, count=count)
    _TEAM_RESULTS_CACHE[key] = results
    return results

# Conversation states
CHECK_CODES, CHECK_TARGET_ODDS, CHECK_EXCLUDE, CHECK_CONFIRM, CHECK_EXPAND, CHECK_REVIEW = range(6)


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

            # Use the same scoring pipeline as /pick
            ev = sporty_event or {
                "home": home_name,
                "away": away_name,
                "eventId": pick.get("event_id", ""),
                "markets": {},
            }

            # Tag booking code metadata onto the event for downstream use
            ev.setdefault("_booking_meta", {
                "selection": pick.get("selection", {}),
                "_original_pick": pick.get("pick", ""),
                "_original_market": pick.get("market", ""),
                "_original_odds": pick.get("odds", 1.0),
            })

            match_scored = _score_fixture(ev, home_results, away_results)

            # Enrich each scored pick with booking code metadata
            for sp in match_scored:
                sp["_source"] = "booking_code"
                sp["_sporty_event"] = sporty_event
                sp["selection"] = pick.get("selection", {})
                sp["_original_pick"] = pick.get("pick", "")
                sp["_original_market"] = pick.get("market", "")
                sp["_original_odds"] = pick.get("odds", 1.0)
                sp["match_status"] = pick.get("match_status", "Upcoming")
                sp["is_winning"] = pick.get("is_winning")
                sp["score"] = pick.get("score", "")

            all_scored.extend(match_scored)
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
    from core.config import LEAGUE_SPORTYBET_NAMES
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
                "good",
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
