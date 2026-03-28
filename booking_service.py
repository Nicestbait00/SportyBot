"""
Booking Service — SportyBet booking code fetch and parse logic.

No Telegram imports. Can be called from bot handlers, OpenClaw tools, or web API.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def fetch_booking_code(code: str) -> dict | None:
    """Fetch and parse a single SportyBet booking code. Returns parsed data or None."""
    try:
        url = f"https://www.sportybet.com/api/ng/orders/share/{code}"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
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

        sel = sel_map.get(event_id, {})
        specifier = sel.get("specifier", "")
        if specifier:
            pick_desc = f"{pick_desc} ({specifier})"

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
