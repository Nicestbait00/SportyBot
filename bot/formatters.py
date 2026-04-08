"""Telegram message formatters for SportyBot."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import LEAGUE_NAMES, load_user_config
from data.sportybet_events import fetch_all_events, filter_events, find_event, build_booking_selection, create_booking_code
from data.booking_service import fetch_booking_code as _fetch_booking_code, parse_outcomes as _parse_outcomes


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


def _kickoff_ms_from_pick(pick: dict) -> int:
    """Return the best-known kickoff timestamp in milliseconds for a pick."""
    cached = pick.get("_sort_kickoff_ms")
    if cached:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass

    sporty_event = pick.get("_sporty_event") or {}
    kickoff = sporty_event.get("estimateStartTime")
    if kickoff:
        try:
            return int(kickoff)
        except (TypeError, ValueError):
            pass

    date_str = pick.get("date", "")
    if date_str:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_str, fmt)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    return 0


def _format_pick_kickoff(pick: dict) -> str:
    """Format a pick kickoff for display."""
    kickoff_ms = _kickoff_ms_from_pick(pick)
    if kickoff_ms <= 0:
        return "Unknown kickoff"
    return datetime.fromtimestamp(kickoff_ms / 1000).strftime("%Y-%m-%d %H:%M")


def _sort_booking_picks(picks: list[dict], mode: str) -> list[dict]:
    """Sort already-selected booking picks without changing the selection itself."""

    def _sort_key(pick: dict):
        kickoff_ms = _kickoff_ms_from_pick(pick)
        has_kickoff = 0 if kickoff_ms > 0 else 1
        if kickoff_ms > 0:
            dt = datetime.fromtimestamp(kickoff_ms / 1000)
            if mode == "time":
                primary = (dt.hour, dt.minute, dt.date().isoformat())
            else:
                primary = (dt.date().isoformat(), dt.hour, dt.minute)
        else:
            primary = ("9999-12-31", 99, 99)
        return (
            has_kickoff,
            primary,
            pick.get("tournament", pick.get("league", "")),
            pick.get("home", ""),
            pick.get("away", ""),
            pick.get("market", ""),
        )

    return sorted([dict(p) for p in picks], key=_sort_key)
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

