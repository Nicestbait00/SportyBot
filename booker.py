"""
SportyBot Booker
Maps analyzed selections to SportyBet and generates booking codes.

NOTE: SportyBet does not publish a public API. The functions below define the
interface we need; implementations will be filled in once we reverse-engineer
or document the required endpoints.
"""

from __future__ import annotations

from typing import Any, Optional

from config import SPORTYBET_BASE_URL


# ── Match search ─────────────────────────────────────────────────────────────

def search_sportybet_match(
    home_team: str,
    away_team: str,
    date: str,
) -> Optional[dict]:
    """
    Find a match on SportyBet by team names and date.

    Args:
        home_team: Home team name (e.g. "Arsenal").
        away_team: Away team name (e.g. "Southampton").
        date:      Date string "YYYY-MM-DD".

    Returns:
        Dict with SportyBet event details (eventId, markets, etc.)
        or None if not found.

    TODO: Reverse-engineer the SportyBet match listing endpoint.
          Likely something like:
          GET {SPORTYBET_BASE_URL}/factsCenter/pcEvents
          with params: sportId=sr:sport:1, marketId=1, ...
          The response contains events with homeTeamName/awayTeamName
          which we can fuzzy-match against our input.
    """
    # TODO: Implement once SportyBet API endpoints are mapped.
    #
    # Pseudocode:
    # resp = requests.get(f"{SPORTYBET_BASE_URL}/factsCenter/pcEvents", params={
    #     "sportId": "sr:sport:1",
    #     "_t": int(time.time() * 1000),
    # })
    # events = resp.json()["data"]["events"]
    # for event in events:
    #     if fuzzy_match(event["homeTeamName"], home_team) and \
    #        fuzzy_match(event["awayTeamName"], away_team):
    #         return event
    # return None

    print(f"[booker] search_sportybet_match not yet implemented")
    print(f"         Looking for: {home_team} vs {away_team} on {date}")
    return None


# ── Slip builder ─────────────────────────────────────────────────────────────

def build_slip(selections: list[dict]) -> dict:
    """
    Build a SportyBet bet slip from analyzer output.

    Args:
        selections: List of pick dicts from analyzer, each with:
            - fixture (with home/away team info)
            - selection ("Home Win", "Away Win", "Over 1.5", etc.)
            - odds

    Returns:
        A slip dict ready for generate_booking_code().

    TODO: Map our generic selections to SportyBet market/outcome IDs.
          SportyBet uses:
            - marketId: "1" for 1x2, "18" for Over/Under, etc.
            - outcomeId: "1" (home), "2" (draw), "3" (away) for 1x2
            - specifier: "total=1.5" for over/under lines
    """
    # TODO: Implement once SportyBet API market IDs are documented.
    #
    # Pseudocode:
    # slip_items = []
    # for sel in selections:
    #     match = search_sportybet_match(
    #         sel["fixture"]["home"]["name"],
    #         sel["fixture"]["away"]["name"],
    #         sel["fixture"]["date"][:10],
    #     )
    #     if match is None:
    #         continue
    #     market_id, outcome_id, specifier = _map_selection(sel["selection"])
    #     slip_items.append({
    #         "eventId": match["eventId"],
    #         "marketId": market_id,
    #         "outcomeId": outcome_id,
    #         "specifier": specifier,
    #     })
    # return {"outcomes": slip_items}

    print(f"[booker] build_slip not yet implemented ({len(selections)} selections)")
    return {"outcomes": [], "selections": selections}


def generate_booking_code(slip: dict) -> Optional[str]:
    """
    Submit a bet slip to SportyBet and receive a booking code.

    Args:
        slip: The slip dict from build_slip().

    Returns:
        Booking code string (e.g. "3A4B5C") or None on failure.

    TODO: Reverse-engineer the SportyBet booking code endpoint.
          Likely something like:
          POST {SPORTYBET_BASE_URL}/orders/share
          Body: {"outcomes": [...]}
          Response: {"data": {"shareCode": "3A4B5C"}}
    """
    # TODO: Implement once SportyBet API endpoints are mapped.
    #
    # Pseudocode:
    # resp = requests.post(
    #     f"{SPORTYBET_BASE_URL}/orders/share",
    #     json=slip,
    #     headers={"Content-Type": "application/json"},
    # )
    # data = resp.json()
    # return data.get("data", {}).get("shareCode")

    print(f"[booker] generate_booking_code not yet implemented")
    return None


# ── Selection mapper (internal) ──────────────────────────────────────────────

def _map_selection(selection: str) -> tuple[str, str, str]:
    """
    Map a human-readable selection to SportyBet market/outcome/specifier.

    TODO: Complete mapping once SportyBet market IDs are confirmed.

    Known mappings (unverified):
        "Home Win"   → marketId="1", outcomeId="1", specifier=""
        "Draw"       → marketId="1", outcomeId="2", specifier=""
        "Away Win"   → marketId="1", outcomeId="3", specifier=""
        "Over 1.5"   → marketId="18", outcomeId="12", specifier="total=1.5"
        "Over 2.5"   → marketId="18", outcomeId="12", specifier="total=2.5"
        "Under 1.5"  → marketId="18", outcomeId="13", specifier="total=1.5"
        "Under 2.5"  → marketId="18", outcomeId="13", specifier="total=2.5"
    """
    selection = selection.strip()

    if selection == "Home Win":
        return ("1", "1", "")
    elif selection == "Draw":
        return ("1", "2", "")
    elif selection == "Away Win":
        return ("1", "3", "")
    elif selection.startswith("Over"):
        line = selection.split()[-1]
        return ("18", "12", f"total={line}")
    elif selection.startswith("Under"):
        line = selection.split()[-1]
        return ("18", "13", f"total={line}")
    else:
        return ("1", "1", "")  # fallback to home win
