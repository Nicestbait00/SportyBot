"""
SportyBot MCP Server — exposes the Python service layer as MCP tools
for OpenClaw to call via stdio transport.

Install: pip install fastmcp
Run:     python mcp_server.py  (OpenClaw launches this automatically)
"""

from __future__ import annotations

import json
import logging
import asyncio
from typing import Any

from fastmcp import FastMCP

# ── Service imports ──────────────────────────────────────────────────────────
from data.sportybet_events import (
    fetch_all_events,
    build_event_index,
    find_event,
    filter_events,
    build_booking_selection,
)
from data.booking_service import fetch_booking_code, parse_outcomes
from data.web_analyzer import get_team_results, analyze_pick
from services.analysis_service import score_fixture
from services.ticket_engine import (
    generate_dynamic_bundle,
    build_qualified_pool,
    select_ticket_from_pool,
    make_ticket_entry,
)
from services.ticket_splitter import (
    validate_split_request,
    split_ticket,
    format_split_summary,
    format_single_ticket,
)
from core import config  # noqa: F401 — ensure env is loaded

logger = logging.getLogger(__name__)

# ── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP("sportybot")


# ─── Booking Code Tools ─────────────────────────────────────────────────────

@mcp.tool()
def fetch_booking(code: str) -> dict:
    """Fetch and parse a SportyBet booking code.

    Returns the full booking data including all picks with odds,
    teams, markets, and outcomes. Use this when the user provides
    a booking code they want to check, split, or analyze.

    Args:
        code: SportyBet booking code (e.g. "2F84B1")
    """
    data = fetch_booking_code(code)
    if not data:
        return {"error": f"Could not fetch booking code '{code}'. Check the code and try again."}
    picks = parse_outcomes(data)
    total_odds = 1.0
    for p in picks:
        total_odds *= float(p.get("odds", 1.0))
    return {
        "code": code,
        "pick_count": len(picks),
        "total_odds": round(total_odds, 2),
        "picks": picks,
    }


@mcp.tool()
def create_booking(selections: list[dict]) -> dict:
    """Create a new SportyBet booking code from a list of selections.

    Each selection needs: eventId, marketId, outcomeId, specifier.
    Use build_selection first to get these from an event + pick.

    Args:
        selections: List of SportyBet selection dicts
    """
    from data.booking_service import create_booking_code
    result = create_booking_code(selections)
    if not result:
        return {"error": "Failed to create booking code."}
    return result


# ─── Event Discovery Tools ──────────────────────────────────────────────────

@mcp.tool()
def list_events(
    timeframe: str | None = None,
    league: str | None = None,
) -> dict:
    """List upcoming football events from SportyBet.

    Respects user's configured leagues and timeframe by default.

    Args:
        timeframe: Override: "today", "tomorrow", "weekend", "7days", "14days"
        league: Optional extra filter like "Premier League", "La Liga"
    """
    from core.config import load_user_config, LEAGUE_SPORTYBET_NAMES

    user_cfg = load_user_config()
    cfg_timeframe = timeframe or user_cfg.get("timeframe", "7days")
    cfg_leagues = user_cfg.get("leagues", [])

    allowed_tournaments = []
    for lid in cfg_leagues:
        allowed_tournaments.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    events = fetch_all_events(
        max_pages=15,
        allowed_tournaments=allowed_tournaments or None,
    )
    filtered = filter_events(events, timeframe=cfg_timeframe)
    if league:
        league_lower = league.lower()
        filtered = [
            e for e in filtered
            if league_lower in e.get("tournament", "").lower()
        ]
    summary = []
    for ev in filtered[:50]:  # cap at 50 to avoid huge responses
        summary.append({
            "home": ev.get("home", "?"),
            "away": ev.get("away", "?"),
            "tournament": ev.get("tournament", "?"),
            "kickoff": ev.get("estimateStartTime", ""),
            "event_id": ev.get("eventId", ""),
        })
    return {"count": len(summary), "events": summary}


@mcp.tool()
def find_sportybet_event(home: str, away: str) -> dict:
    """Find a specific SportyBet event by team names.

    Uses fuzzy matching so exact names aren't required.

    Args:
        home: Home team name
        away: Away team name
    """
    ev = find_event(home, away)
    if not ev:
        return {"error": f"No event found for {home} vs {away}"}
    return {
        "home": ev.get("home"),
        "away": ev.get("away"),
        "tournament": ev.get("tournament"),
        "event_id": ev.get("eventId"),
        "market_count": len(ev.get("markets", {})),
    }


# ─── Analysis Tools ─────────────────────────────────────────────────────────

@mcp.tool()
def analyze_booking_code(code: str) -> dict:
    """Analyze all picks in a booking code using form data and scoring.

    Returns each pick enriched with verdict (BACK/SKIP/LEAN),
    confidence, reasons, and suggestions. Use this for /check flows.

    Args:
        code: SportyBet booking code
    """
    data = fetch_booking_code(code)
    if not data:
        return {"error": f"Could not fetch booking code '{code}'."}
    picks = parse_outcomes(data)
    analyzed = []
    for pick in picks:
        result = analyze_pick(pick)
        analyzed.append(result)
    backs = sum(1 for a in analyzed if a.get("verdict") == "BACK")
    skips = sum(1 for a in analyzed if a.get("verdict") == "SKIP")
    return {
        "code": code,
        "total_picks": len(analyzed),
        "backs": backs,
        "skips": skips,
        "picks": analyzed,
    }


@mcp.tool()
def get_form_data(team: str, count: int = 10) -> dict:
    """Get recent match results for a team.

    Pulls from football-data.org (primary) and thesportsdb.com (fallback).
    Useful for checking form before placing bets.

    Args:
        team: Team name (fuzzy matched)
        count: Number of recent matches (default 10)
    """
    results = get_team_results(team, count)
    if not results:
        return {"error": f"No form data found for '{team}'"}
    return {"team": team, "matches": len(results), "results": results}


@mcp.tool()
def score_match(
    home: str,
    away: str,
) -> dict:
    """Score a fixture — returns all market picks with confidence ratings.

    Fetches form data for both teams, runs the scoring engine,
    and returns picks across 1X2, BTTS, Over/Under, Double Chance, etc.

    Args:
        home: Home team name
        away: Away team name
    """
    ev = find_event(home, away)
    if not ev:
        return {"error": f"No event found for {home} vs {away}"}
    home_results = get_team_results(home, 10)
    away_results = get_team_results(away, 10)
    scored = score_fixture(ev, home_results, away_results)
    return {
        "home": home,
        "away": away,
        "pick_count": len(scored),
        "picks": scored,
    }


# ─── Ticket Building Tools ──────────────────────────────────────────────────

@mcp.tool()
def build_ticket(
    ticket_count: int = 1,
    target_odds: str = "5.0",
    timeframe: str | None = None,
    league: str | None = None,
) -> dict:
    """Build dynamic betting ticket(s) using the analysis engine.

    Loads user config for leagues, min_confidence, min_odds, enabled_markets.
    Only scores fixtures matching the user's configured leagues and timeframe.

    Args:
        ticket_count: Number of tickets to generate (1-5)
        target_odds: Target odds per ticket. Single number "10" or
                     comma-separated "5,10,25" for per-ticket targets.
        timeframe: Override timeframe. Default uses user config.
        league: Optional extra league filter (name, e.g. "Premier League")
    """
    from core.config import load_user_config, LEAGUE_SPORTYBET_NAMES

    # Load user config
    user_cfg = load_user_config()
    min_conf = user_cfg.get("min_confidence", 75)
    min_odds = user_cfg.get("min_odds", 1.15)
    enabled_markets = set(user_cfg.get("enabled_markets", []))
    cfg_timeframe = timeframe or user_cfg.get("timeframe", "7days")
    cfg_leagues = user_cfg.get("leagues", [])

    # Build pick_cfg from user settings
    pick_cfg = {
        "min_confidence": min_conf,
        "min_odds": min_odds,
        "preferred_markets": list(enabled_markets),
    }

    # Parse targets
    try:
        targets = [float(x.strip()) for x in target_odds.split(",")]
    except ValueError:
        return {"error": f"Invalid target_odds format: '{target_odds}'"}

    if len(targets) == 1:
        target = targets[0]
    else:
        target = targets
        ticket_count = len(targets)

    ticket_count = min(max(ticket_count, 1), 5)

    # Build allowed tournaments from user's league config
    allowed_tournaments = []
    for lid in cfg_leagues:
        allowed_tournaments.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    # Fetch only events matching user's leagues
    events = fetch_all_events(
        max_pages=15,
        allowed_tournaments=allowed_tournaments or None,
    )
    filtered = filter_events(events, timeframe=cfg_timeframe)

    # Extra league name filter if provided
    if league:
        league_lower = league.lower()
        filtered = [
            e for e in filtered
            if league_lower in e.get("tournament", "").lower()
        ]

    # Score fixtures
    all_scored = []
    for ev in filtered:
        home_results = get_team_results(ev.get("home", ""), 10)
        away_results = get_team_results(ev.get("away", ""), 10)
        scored = score_fixture(ev, home_results, away_results)
        all_scored.extend(scored)

    # Apply user filters: min_confidence, min_odds, enabled_markets
    if enabled_markets:
        all_scored = [
            p for p in all_scored
            if p.get("market") in enabled_markets
        ]
    all_scored = [
        p for p in all_scored
        if p.get("confidence", 0) >= min_conf
        and float(p.get("odds", 0)) >= min_odds
    ]

    if not all_scored:
        return {
            "error": "No picks passed your filters.",
            "config": {
                "leagues": len(cfg_leagues),
                "timeframe": cfg_timeframe,
                "min_confidence": min_conf,
                "min_odds": min_odds,
                "enabled_markets": len(enabled_markets),
                "events_found": len(filtered),
            },
        }

    tickets, profile, reused = generate_dynamic_bundle(
        all_scored, ticket_count, target, pick_cfg, set()
    )

    result_tickets = []
    for t in tickets:
        result_tickets.append({
            "ticket_id": t.get("ticket_id"),
            "target_odds": t.get("target_odds"),
            "actual_odds": t.get("actual_odds"),
            "pick_count": t.get("pick_count"),
            "picks": t.get("picks", []),
        })

    return {
        "ticket_count": len(result_tickets),
        "tickets": result_tickets,
    }


# ─── Ticket Splitting Tools ─────────────────────────────────────────────────

@mcp.tool()
def split_booking_code(
    code: str,
    target_odds: str,
) -> dict:
    """Split a SportyBet booking code into smaller tickets.

    Uses unique-first strategy — each ticket gets its own picks.
    Picks are only shared when remaining pool can't reach the target.
    Shared picks are marked so the user can remove them if desired.

    Args:
        code: SportyBet booking code to split
        target_odds: Comma-separated target odds per ticket, e.g. "10,50,100"
    """
    data = fetch_booking_code(code)
    if not data:
        return {"error": f"Could not fetch booking code '{code}'."}
    picks = parse_outcomes(data)

    try:
        targets = [float(x.strip()) for x in target_odds.split(",")]
    except ValueError:
        return {"error": f"Invalid target_odds format: '{target_odds}'"}

    err = validate_split_request(picks, targets)
    if err:
        return {"error": err}

    result = split_ticket(picks, targets)
    summary = format_split_summary(result)

    return {
        "summary_text": summary,
        "tickets": result["tickets"],
        "total_original_odds": result["total_original_odds"],
    }


@mcp.tool()
def book_split_ticket(
    code: str,
    target_odds: str,
    ticket_number: int,
) -> dict:
    """Book (create a new code for) one ticket from a split.

    First splits the booking code, then creates a new booking code
    for the specified ticket number.

    Args:
        code: Original SportyBet booking code
        target_odds: Same comma-separated targets used in the split
        ticket_number: Which ticket to book (1-based)
    """
    data = fetch_booking_code(code)
    if not data:
        return {"error": f"Could not fetch booking code '{code}'."}
    picks = parse_outcomes(data)

    try:
        targets = [float(x.strip()) for x in target_odds.split(",")]
    except ValueError:
        return {"error": f"Invalid target_odds: '{target_odds}'"}

    result = split_ticket(picks, targets)

    if ticket_number < 1 or ticket_number > len(result["tickets"]):
        return {"error": f"Ticket number must be 1-{len(result['tickets'])}"}

    ticket = result["tickets"][ticket_number - 1]
    selections = []
    for p in ticket["picks"]:
        ev = find_event(p.get("home", ""), p.get("away", ""))
        if ev:
            sel = build_booking_selection(ev, p.get("market", ""), p.get("pick", ""))
            if sel:
                selections.append(sel)

    if not selections:
        return {"error": "Could not build selections for booking."}

    from data.booking_service import create_booking_code
    booking = create_booking_code(selections)
    if not booking:
        return {"error": "Failed to create booking code."}

    return {
        "ticket_number": ticket_number,
        "booking_code": booking.get("code"),
        "actual_odds": ticket["actual_odds"],
        "pick_count": ticket["pick_count"],
    }


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
