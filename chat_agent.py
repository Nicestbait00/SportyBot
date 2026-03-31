"""
Chat Agent — conversational AI interface using Gemini 2.0 Flash function calling.

Provides a natural-language chat mode where users can ask about matches,
build tickets, check booking codes, and get analysis — all backed by real data
from the service layer.

No Telegram imports. Called from telegram_bot.py handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import requests

from analysis_service import score_fixture
from booking_service import fetch_booking_code, parse_outcomes
from config import (
    DEFAULT_ENABLED_MARKETS,
    LEAGUE_NAMES,
    LEAGUE_SPORTYBET_NAMES,
    STRATEGY_PRESETS,
    load_user_config,
)
from scorer import score_match, cross_check_with_odds
from sportybet_events import (
    build_booking_selection,
    create_booking_code,
    fetch_all_events,
    filter_events,
    find_event,
)
from ticket_engine import (
    build_qualified_pool,
    make_ticket_entry,
    select_ticket_from_pool,
)
from web_analyzer import get_team_results, _summarize_form

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

MAX_HISTORY = 20
HISTORY_TTL = 30 * 60  # 30 minutes
MAX_TOOL_ROUNDS = 5

# ── System prompt ────────────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """\
You are SportyBot, a sharp football betting analyst on Telegram. You use real \
form data, odds, and statistical models to help users make informed decisions.

Personality: Knowledgeable, confident, conversational — like a mate who \
genuinely understands football data. Warm but efficient. Plain language, not jargon.

Rules:
1. ALWAYS use tools to get real data. Never make up stats, odds, or predictions.
2. You do NOT make picks — the scoring engine does. You explain and present its output.
3. Encourage responsible gambling. If someone seems to be chasing losses, gently flag it.
4. Never guarantee outcomes. Betting always carries risk.
5. When presenting picks, always mention confidence levels and key reasons.
6. When building tickets, show picks clearly and ask "Want me to book this?" before booking.
7. Keep responses concise — 2-4 sentences for simple questions, longer for analysis.
8. Lead with the strongest insight, not a data dump.
9. If a tool returns no data or an error, say so honestly.
10. Format picks as numbered lists with team names, market, pick, odds, and confidence.
"""

# ── Tool declarations ────────────────────────────────────────────────────────

TOOL_DECLARATIONS = [
    {
        "name": "get_upcoming_matches",
        "description": (
            "Fetch upcoming football matches from SportyBet. Returns match list "
            "with team names, leagues, kick-off times, and key odds. Use when the "
            "user asks what matches are available or what's playing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timeframe": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "weekend", "7days", "14days"],
                    "description": "Time window. Defaults to user's configured timeframe.",
                },
            },
        },
    },
    {
        "name": "analyze_match",
        "description": (
            "Deep analysis of a specific match. Returns form data, scored picks "
            "with confidence ratings, and analysis reasons. Use when the user asks "
            "about a specific fixture."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "home_team": {"type": "string", "description": "Home team name"},
                "away_team": {"type": "string", "description": "Away team name"},
            },
            "required": ["home_team", "away_team"],
        },
    },
    {
        "name": "build_ticket",
        "description": (
            "Build a betting ticket at target odds. Runs the full analysis pipeline "
            "and returns the best combo. Use when user asks for picks or a ticket."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_odds": {
                    "type": "number",
                    "description": "Target total odds, e.g. 10.0",
                },
                "ticket_count": {
                    "type": "integer",
                    "description": "Number of tickets (1-5). Default 1.",
                },
            },
            "required": ["target_odds"],
        },
    },
    {
        "name": "check_booking_code",
        "description": (
            "Fetch and analyze a SportyBet booking code. Returns all selections "
            "with outcomes, odds, and match results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "SportyBet booking code (6-10 alphanumeric chars)",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "explain_pick",
        "description": (
            "Explain why a specific pick was rated the way it was. Returns detailed "
            "scoring breakdown. Use when user asks 'why' about a selection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "home_team": {"type": "string"},
                "away_team": {"type": "string"},
                "market": {
                    "type": "string",
                    "description": "e.g. '1X2', 'Over/Under', 'GG/NG'",
                },
                "pick": {
                    "type": "string",
                    "description": "e.g. 'Home', 'Over (total=1.5)', 'GG'",
                },
            },
            "required": ["home_team", "away_team", "market", "pick"],
        },
    },
    {
        "name": "book_ticket",
        "description": (
            "Generate a SportyBet booking code for a previously built ticket. "
            "Only call AFTER showing the user the ticket and getting confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Conversation history management ─────────────────────────────────────────

def _get_history(user_data: dict) -> list[dict]:
    """Get conversation history, applying TTL and returning messages list."""
    hist = user_data.get("agent_history")
    if not hist or time.time() - hist.get("last_active", 0) > HISTORY_TTL:
        user_data["agent_history"] = {"messages": [], "last_active": time.time()}
        return []
    hist["last_active"] = time.time()
    return hist["messages"]


def _append(user_data: dict, role: str, parts: list[dict]) -> None:
    """Append a message to history, enforcing max length."""
    hist = user_data.setdefault("agent_history", {"messages": [], "last_active": time.time()})
    hist["messages"].append({"role": role, "parts": parts})
    hist["last_active"] = time.time()
    while len(hist["messages"]) > MAX_HISTORY:
        hist["messages"].pop(0)


# ── OpenRouter API call ──────────────────────────────────────────────────────

def _history_to_openai(history: list[dict]) -> list[dict]:
    """Convert Gemini-style history to OpenAI-style messages for OpenRouter."""
    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    for entry in history:
        role = entry.get("role", "user")
        parts = entry.get("parts", [])

        # Map Gemini roles to OpenAI roles
        oai_role = "assistant" if role == "model" else "user"

        # Check for function calls (model → assistant with tool_calls)
        fn_calls = [p for p in parts if "functionCall" in p]
        if fn_calls:
            tool_calls = []
            for i, fc in enumerate(fn_calls):
                tool_calls.append({
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": fc["functionCall"]["name"],
                        "arguments": json.dumps(fc["functionCall"].get("args", {})),
                    },
                })
            messages.append({"role": "assistant", "tool_calls": tool_calls})
            continue

        # Check for function responses (user → tool messages)
        fn_responses = [p for p in parts if "functionResponse" in p]
        if fn_responses:
            for i, fr in enumerate(fn_responses):
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": json.dumps(fr["functionResponse"]["response"], default=str),
                })
            continue

        # Regular text
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if text:
            messages.append({"role": oai_role, "content": text})

    return messages


def _tool_declarations_to_openai() -> list[dict]:
    """Convert Gemini-style tool declarations to OpenAI-style tools."""
    tools = []
    for decl in TOOL_DECLARATIONS:
        tools.append({
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl["description"],
                "parameters": decl.get("parameters", {"type": "object", "properties": {}}),
            },
        })
    return tools


def _call_llm(history: list[dict]) -> dict | None:
    """Call OpenRouter with function calling. Returns Gemini-style content dict or None."""
    if not OPENROUTER_API_KEY:
        return None

    messages = _history_to_openai(history)
    tools = _tool_declarations_to_openai()

    body = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "tools": tools,
        "temperature": 0.4,
        "max_tokens": 1024,
    }

    for attempt in range(2):
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if resp.status_code == 429:
                logger.warning("OpenRouter rate limited (429)")
                return {"_error": "rate_limited"}
            if resp.status_code != 200:
                logger.warning(f"OpenRouter error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})

            # Convert OpenAI response back to Gemini-style content dict
            parts = []
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    parts.append({
                        "functionCall": {"name": fn["name"], "args": args}
                    })
            if msg.get("content"):
                parts.append({"text": msg["content"]})

            if not parts:
                return None
            return {"parts": parts}

        except requests.Timeout:
            if attempt == 0:
                continue
            return None
        except Exception as e:
            logger.warning(f"OpenRouter call failed: {e}")
            return None
    return None


# ── Tool implementations ─────────────────────────────────────────────────────

async def _tool_get_upcoming_matches(args: dict, chat_id: int) -> dict:
    """Fetch upcoming matches."""
    config = load_user_config(chat_id=chat_id)
    leagues = config.get("leagues", [])
    timeframe = args.get("timeframe") or config.get("timeframe", "7days")

    allowed = None
    if leagues:
        allowed = []
        for lid in leagues:
            allowed.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    events = await asyncio.to_thread(
        fetch_all_events, max_pages=10, allowed_tournaments=allowed or None,
    )
    if not events:
        return {"error": "Could not fetch matches from SportyBet right now."}

    filtered = filter_events(events, league_ids=leagues, timeframe=timeframe)
    if not filtered:
        return {"matches": [], "message": "No matches found for your leagues and timeframe."}

    # Summarize top 30
    matches = []
    for ev in filtered[:30]:
        m1x2 = ev.get("markets", {}).get("1", {}).get("outcomes", {})
        home_odds = m1x2.get("1", {}).get("odds", "?")
        draw_odds = m1x2.get("2", {}).get("odds", "?")
        away_odds = m1x2.get("3", {}).get("odds", "?")
        kickoff = ""
        ts = ev.get("estimateStartTime", 0)
        if ts:
            try:
                kickoff = datetime.fromtimestamp(ts / 1000).strftime("%a %d %b %H:%M")
            except Exception:
                pass
        matches.append({
            "home": ev["home"],
            "away": ev["away"],
            "league": ev.get("tournament", ""),
            "kickoff": kickoff,
            "odds": {"home": home_odds, "draw": draw_odds, "away": away_odds},
        })

    return {"match_count": len(filtered), "showing": len(matches), "matches": matches}


async def _tool_analyze_match(args: dict) -> dict:
    """Deep analysis of one fixture."""
    home = args["home_team"]
    away = args["away_team"]

    ev = await asyncio.to_thread(find_event, home, away)
    if not ev:
        return {"error": f"Could not find {home} vs {away} on SportyBet. Check team names."}

    home_results = await asyncio.to_thread(get_team_results, ev["home"], 10)
    away_results = await asyncio.to_thread(get_team_results, ev["away"], 10)

    picks = score_fixture(ev, home_results, away_results)
    picks.sort(key=lambda p: p.get("confidence", 0), reverse=True)

    # Build form summaries
    home_form = _summarize_form(home_results) if home_results else None
    away_form = _summarize_form(away_results) if away_results else None

    top_picks = []
    for p in picks[:8]:
        top_picks.append({
            "market": p["market"],
            "pick": p["pick"],
            "odds": round(p["odds"], 2),
            "confidence": p.get("confidence", 0),
            "verdict": p.get("verdict", "?"),
            "reasons": (p.get("analysis_reasons") or [])[:2],
            "data_quality": p.get("data_quality", "good"),
        })

    result = {
        "home": ev["home"],
        "away": ev["away"],
        "league": ev.get("tournament", ""),
        "picks_scored": len(picks),
        "top_picks": top_picks,
    }
    if home_form:
        result["home_form"] = {
            "wins": home_form.get("wins", 0),
            "draws": home_form.get("draws", 0),
            "losses": home_form.get("losses", 0),
            "goals_scored_avg": round(home_form.get("goals_scored_avg", 0), 1),
            "goals_conceded_avg": round(home_form.get("goals_conceded_avg", 0), 1),
        }
    if away_form:
        result["away_form"] = {
            "wins": away_form.get("wins", 0),
            "draws": away_form.get("draws", 0),
            "losses": away_form.get("losses", 0),
            "goals_scored_avg": round(away_form.get("goals_scored_avg", 0), 1),
            "goals_conceded_avg": round(away_form.get("goals_conceded_avg", 0), 1),
        }
    return result


async def _tool_build_ticket(args: dict, user_data: dict, chat_id: int) -> dict:
    """Build a ticket from the full analysis pipeline."""
    target = float(args.get("target_odds", 10))
    if target < 1.5 or target > 500:
        return {"error": "Target odds must be between 1.5 and 500."}

    config = load_user_config(chat_id=chat_id)
    leagues = config.get("leagues", [])
    timeframe = config.get("timeframe", "7days")

    # Fetch events
    allowed = None
    if leagues:
        allowed = []
        for lid in leagues:
            allowed.extend(LEAGUE_SPORTYBET_NAMES.get(lid, []))

    events = await asyncio.to_thread(
        fetch_all_events, max_pages=15, allowed_tournaments=allowed or None,
    )
    if not events:
        return {"error": "Could not fetch matches from SportyBet."}

    filtered = filter_events(events, league_ids=leagues, timeframe=timeframe)
    if not filtered:
        return {"error": "No matches found for your leagues and timeframe."}

    # Score all fixtures
    all_scored = []
    for ev in filtered:
        home_results = await asyncio.to_thread(get_team_results, ev["home"], 10)
        away_results = await asyncio.to_thread(get_team_results, ev["away"], 10)
        picks = score_fixture(ev, home_results, away_results)
        all_scored.extend(picks)

    if not all_scored:
        return {"error": "Could not score any fixtures. Try wider filters."}

    # Build pool and select
    pick_cfg = _get_pick_config(config)
    qualified = build_qualified_pool(all_scored, set(), pick_cfg, None, 0)
    if not qualified:
        return {"error": "No picks met the quality threshold. Try /strategy to adjust."}

    picks = select_ticket_from_pool(qualified, target, None, set())
    if not picks:
        return {"error": f"Could not build a ticket at {target} odds from available picks."}

    ticket = make_ticket_entry(1, "agent", target, picks)

    # Store for booking
    user_data["agent_pending_ticket"] = ticket

    ticket_picks = []
    for p in ticket["picks"]:
        ticket_picks.append({
            "home": p["home"],
            "away": p["away"],
            "market": p["market"],
            "pick": p["pick"],
            "odds": round(p["odds"], 2),
            "confidence": p.get("confidence", 0),
            "verdict": p.get("verdict", "?"),
            "reason": (p.get("analysis_reasons") or [""])[0],
        })

    return {
        "picks": ticket_picks,
        "total_odds": round(ticket["total_odds"], 2),
        "avg_confidence": ticket["avg_confidence"],
        "pick_count": len(ticket_picks),
        "target_odds": target,
    }


async def _tool_check_booking_code(args: dict) -> dict:
    """Fetch and analyze a booking code."""
    code = args.get("code", "").strip().upper()
    if not code or len(code) < 4:
        return {"error": "Invalid booking code."}

    data = await asyncio.to_thread(fetch_booking_code, code)
    if not data:
        return {"error": f"Could not fetch code {code}. It may be expired or invalid."}

    picks = parse_outcomes(data)
    if not picks:
        return {"error": f"No picks found in code {code}."}

    results = []
    won = lost = pending = 0
    total_odds = 1.0
    for p in picks:
        total_odds *= p["odds"]
        if p["match_status"] == "Ended":
            if p.get("is_winning") == 1:
                won += 1
            elif p.get("is_winning") == 0:
                lost += 1
        else:
            pending += 1
        results.append({
            "home": p["home"],
            "away": p["away"],
            "market": p["market"],
            "pick": p["pick"],
            "odds": round(p["odds"], 2),
            "status": p["match_status"],
            "result": p["rating"],
            "score": p.get("score", ""),
        })

    return {
        "code": code,
        "picks": results,
        "total_odds": round(total_odds, 2),
        "summary": {"won": won, "lost": lost, "pending": pending, "total": len(picks)},
    }


async def _tool_explain_pick(args: dict) -> dict:
    """Explain scoring for a specific pick."""
    home = args["home_team"]
    away = args["away_team"]
    market = args.get("market", "")
    pick = args.get("pick", "")

    ev = await asyncio.to_thread(find_event, home, away)
    if not ev:
        return {"error": f"Could not find {home} vs {away} on SportyBet."}

    home_results = await asyncio.to_thread(get_team_results, ev["home"], 10)
    away_results = await asyncio.to_thread(get_team_results, ev["away"], 10)

    all_picks = score_fixture(ev, home_results, away_results)

    # Find the matching pick
    target_market = market.lower()
    target_pick = pick.lower()
    match = None
    for p in all_picks:
        if target_market in p["market"].lower() and target_pick in p["pick"].lower():
            match = p
            break

    if not match:
        # Return best available for that market
        market_picks = [p for p in all_picks if target_market in p["market"].lower()]
        if market_picks:
            return {
                "error": f"Pick '{pick}' not found for {market}.",
                "available_picks": [
                    {"market": p["market"], "pick": p["pick"], "odds": round(p["odds"], 2), "confidence": p.get("confidence", 0)}
                    for p in sorted(market_picks, key=lambda x: x.get("confidence", 0), reverse=True)[:5]
                ],
            }
        return {"error": f"No picks found for {market} in {home} vs {away}."}

    return {
        "home": match["home"],
        "away": match["away"],
        "market": match["market"],
        "pick": match["pick"],
        "odds": round(match["odds"], 2),
        "confidence": match.get("confidence", 0),
        "verdict": match.get("verdict", "?"),
        "data_quality": match.get("data_quality", "good"),
        "reasons": match.get("analysis_reasons", []),
    }


async def _tool_book_ticket(user_data: dict) -> dict:
    """Book the pending ticket."""
    pending = user_data.get("agent_pending_ticket")
    if not pending:
        return {"error": "No ticket to book. Build one first with build_ticket."}

    picks = pending.get("picks", [])
    if not picks:
        return {"error": "Ticket has no picks."}

    booking_selections = []
    failed = []
    for p in picks:
        sporty_event = p.get("_sporty_event")
        if sporty_event:
            sel = await asyncio.to_thread(build_booking_selection, sporty_event, p["market"], p["pick"])
        else:
            found = await asyncio.to_thread(find_event, p["home"], p["away"])
            if found:
                sel = await asyncio.to_thread(build_booking_selection, found, p["market"], p["pick"])
            else:
                failed.append(f"{p['home']} vs {p['away']}: event not found")
                continue
        if sel:
            booking_selections.append(sel)
        else:
            failed.append(f"{p['home']} vs {p['away']}: market unavailable")

    code = None
    if booking_selections:
        code = await asyncio.to_thread(create_booking_code, booking_selections)

    user_data.pop("agent_pending_ticket", None)

    return {
        "booking_code": code,
        "booked_count": len(booking_selections),
        "failed": failed,
        "total_picks": len(picks),
    }


# ── Pick config helper (mirrors telegram_bot._get_pick_config) ───────────────

def _get_pick_config(user_config: dict) -> dict:
    if "min_confidence" in user_config and "enabled_markets" in user_config:
        return {
            "min_confidence": user_config.get("min_confidence", 75),
            "min_odds": user_config.get("min_odds", 1.05),
            "preferred_markets": user_config.get("enabled_markets", list(DEFAULT_ENABLED_MARKETS)),
        }
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return strat
    return STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])


# ── Tool dispatcher ──────────────────────────────────────────────────────────

async def _execute_tool(name: str, args: dict, user_data: dict, chat_id: int) -> dict:
    """Dispatch a function call to the appropriate tool."""
    try:
        if name == "get_upcoming_matches":
            return await _tool_get_upcoming_matches(args, chat_id)
        elif name == "analyze_match":
            return await _tool_analyze_match(args)
        elif name == "build_ticket":
            return await _tool_build_ticket(args, user_data, chat_id)
        elif name == "check_booking_code":
            return await _tool_check_booking_code(args)
        elif name == "explain_pick":
            return await _tool_explain_pick(args)
        elif name == "book_ticket":
            return await _tool_book_ticket(user_data)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        logger.warning(f"Tool {name} failed: {e}")
        return {"error": f"Tool failed: {e}"}


# ── Main entry point ─────────────────────────────────────────────────────────

async def agent_respond(user_message: str, user_data: dict, chat_id: int) -> str:
    """Process a user message through the Gemini function-calling agent.

    Returns the final text response to send to the user.
    """
    if not OPENROUTER_API_KEY:
        return "Chat agent not available — OPENROUTER_API_KEY not set in environment."

    history = _get_history(user_data)
    _append(user_data, "user", [{"text": user_message}])

    for _round in range(MAX_TOOL_ROUNDS):
        content = await asyncio.to_thread(_call_llm, history)
        if not content:
            return "LLM API is down or returned an error. Use /pick or /check instead."
        if isinstance(content, dict) and content.get("_error") == "rate_limited":
            return "API rate limited. Try again in a minute."

        parts = content.get("parts", [])

        # Check for function calls
        fn_calls = [p for p in parts if "functionCall" in p]
        if not fn_calls:
            # Text response — done
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            _append(user_data, "model", parts)
            return text or "I'm not sure how to respond to that."

        # Execute function calls
        _append(user_data, "model", parts)
        response_parts = []
        for fc in fn_calls:
            name = fc["functionCall"]["name"]
            args = fc["functionCall"].get("args", {})
            logger.info(f"Agent tool call: {name}({json.dumps(args)[:200]})")
            result = await _execute_tool(name, args, user_data, chat_id)
            # Truncate large results
            result_json = json.dumps(result, default=str)
            if len(result_json) > 8000:
                result = {"summary": "Result too large, showing first items.", "data": result_json[:7500]}
            response_parts.append({
                "functionResponse": {"name": name, "response": result},
            })
        _append(user_data, "user", response_parts)

    return "I got a bit tangled up processing that. Try rephrasing your question."
