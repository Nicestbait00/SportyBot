"""
Gemini conversational layer for SportyBot.

Handles intent parsing and natural language conversation.
Gemini does NOT make pick decisions — only the deterministic scoring engine does that.
"""

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

SYSTEM_PROMPT = """\
You are SportyBot, a friendly football betting assistant on Telegram.

Your ONLY role is to understand what the user wants and return structured JSON.
You do NOT make pick decisions — the deterministic engine handles that.

Return a JSON object (no markdown fences, no extra text) with these fields:
{
  "intent": "<one of: pick, check, leagues, timeframe, stats, chat>",
  "params": { ... },
  "reply": "A short conversational message to show the user"
}

Intent definitions:
- "pick": The user wants betting picks or a combo. Extract from their message:
  params.target_odds (float or null) — e.g. "give me 10 odds" → 10.0
  params.market (string or null) — e.g. "over 2.5 picks" → "over_2.5", "home wins" → "1x2"
  params.market_slots (list or null) — when the user specifies a DISTRIBUTION of markets, extract it as a list of slot objects:
    Each slot: {"market": "1X2"|"Over/Under"|"GG/NG", "count": int, "threshold": float|null, "fill": bool}
    Examples:
      "3 wins, 3 over 1.5, rest over 0.5" → [{"market":"1X2","count":3},{"market":"Over/Under","count":3,"threshold":1.5},{"market":"Over/Under","fill":true,"threshold":0.5}]
      "5 home wins and some overs" → [{"market":"1X2","count":5},{"market":"Over/Under","fill":true,"threshold":1.5}]
      "2 btts, 2 over 2.5, 1 win" → [{"market":"GG/NG","count":2},{"market":"Over/Under","count":2,"threshold":2.5},{"market":"1X2","count":1}]
    "fill": true means "use this market for remaining picks to reach target odds"
    "threshold" only applies to Over/Under (0.5, 1.5, 2.5, 3.5). Default to 1.5 if not specified.
    If user says "straight win" or "home/away win" → market "1X2"
    If user says "btts" or "both teams to score" or "GG" → market "GG/NG"
    Only populate market_slots when the user explicitly requests multiple market types with counts. For simple requests like "10 odds" leave it null.
- "check": The user wants to check/analyze a SportyBet booking code.
  params.code (string or null) — the booking code if they provided one
- "leagues": The user wants to see or change which leagues are active.
  params — empty {}
- "timeframe": The user wants to change the time window (today, weekend, 7 days, etc.).
  params — empty {}
- "stats": The user is asking about a specific team's form, results, or stats.
  params.team (string) — the team name
- "chat": General football chat, greetings, questions about how the bot works, etc.
  params — empty {}

Rules:
- If the user says "hi", "hello", "hey" etc. → intent "chat", friendly greeting reply mentioning they can ask for picks.
- If the user says something like "get me picks", "find me a bet", "5 odds" → intent "pick".
- If the user mentions a booking code (alphanumeric ~6-10 chars) → intent "check".
- If the user asks "how is Arsenal doing", "Chelsea form" → intent "stats".
- If ambiguous, default to "chat" with a helpful reply.
- Keep replies short (1-3 sentences).
- ALWAYS return valid JSON only. No markdown code fences.
"""


def chat(message: str, context: dict = None) -> dict:
    """
    Send a message to Gemini for intent parsing.

    Args:
        message: The user's raw text message.
        context: Optional dict with user context (active leagues, timeframe, etc.).

    Returns:
        dict with keys: intent, params, reply
    """
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set — falling back to basic parsing")
        return _fallback_parse(message)

    # Build the user prompt with optional context
    user_text = message
    if context:
        user_text = f"[User context: {json.dumps(context)}]\n\nUser message: {message}"

    body = {
        "contents": [
            {"parts": [{"text": user_text}]}
        ],
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 512,
        },
    }

    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract text from Gemini response
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        result = json.loads(text)

        # Validate required fields
        if "intent" not in result:
            result["intent"] = "chat"
        if "params" not in result:
            result["params"] = {}
        if "reply" not in result:
            result["reply"] = "I'm here to help with football picks!"

        # Validate intent is one of allowed values
        allowed = {"pick", "check", "leagues", "timeframe", "stats", "chat"}
        if result["intent"] not in allowed:
            result["intent"] = "chat"

        return result

    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON: {text[:200] if 'text' in dir() else '(no text)'}")
        return {
            "intent": "chat",
            "params": {},
            "reply": "Sorry, I had trouble understanding that. Try /pick for picks or /check to analyze a code.",
        }
    except requests.RequestException as e:
        logger.warning(f"Gemini API error: {e}")
        return _fallback_parse(message)
    except Exception as e:
        logger.warning(f"Gemini chat error: {e}")
        return _fallback_parse(message)


def _parse_market_slots(msg: str) -> list | None:
    """Extract market slot distribution from a natural language message.

    Returns list of slot dicts or None if no structured distribution found.
    """
    slots = []

    # Pattern: "N win(s)" or "N straight win(s)" → 1X2
    win_match = re.search(r"(\d+)\s*(?:straight\s+)?win", msg)
    if win_match:
        slots.append({"market": "1X2", "count": int(win_match.group(1))})

    # Pattern: "N over X.5" → Over/Under with threshold
    over_matches = re.finditer(r"(\d+)\s*over\s*(\d+(?:\.\d+)?)", msg)
    for m in over_matches:
        slots.append({
            "market": "Over/Under",
            "count": int(m.group(1)),
            "threshold": float(m.group(2)),
        })

    # Pattern: "N btts" or "N gg" → GG/NG
    btts_match = re.search(r"(\d+)\s*(?:btts|gg|both\s+teams)", msg)
    if btts_match:
        slots.append({"market": "GG/NG", "count": int(btts_match.group(1))})

    # Pattern: "rest over X.5" or "the rest over X.5" → fill slot
    rest_match = re.search(r"rest\s+(?:over\s*)?(\d+(?:\.\d+)?)", msg)
    if rest_match:
        slots.append({
            "market": "Over/Under",
            "threshold": float(rest_match.group(1)),
            "fill": True,
        })

    # Only return if we found at least 2 different market types or explicit distribution
    if len(slots) >= 2:
        return slots
    return None


def _fallback_parse(message: str) -> dict:
    """Basic keyword-based fallback when Gemini is unavailable."""
    msg = message.lower().strip()

    # Greetings
    if msg in ("hi", "hello", "hey", "yo", "sup", "good morning", "good evening"):
        return {
            "intent": "chat",
            "params": {},
            "reply": "Hey! I'm SportyBot. Ask me for picks, check a booking code, or just chat about football.",
        }

    # Pick intent
    odds_match = re.search(r"(\d+(?:\.\d+)?)\s*odds", msg)
    if odds_match or any(kw in msg for kw in ("pick", "bet", "combo", "find me", "get me")):
        target = float(odds_match.group(1)) if odds_match else None
        market = None
        market_slots = None

        # Try to extract market distribution from structured requests
        slots = _parse_market_slots(msg)
        if slots:
            market_slots = slots
        elif "over" in msg:
            market = "over_2.5"
        elif "home" in msg or "win" in msg:
            market = "1x2"

        return {
            "intent": "pick",
            "params": {"target_odds": target, "market": market, "market_slots": market_slots},
            "reply": f"Looking for picks{' at ' + str(target) + ' odds' if target else ''}...",
        }

    # Check intent — look for booking code patterns
    code_match = re.search(r"\b([A-Za-z0-9]{6,10})\b", msg)
    if "check" in msg or "code" in msg or "analyze" in msg:
        return {
            "intent": "check",
            "params": {"code": code_match.group(1) if code_match else None},
            "reply": "Let me analyze that for you.",
        }

    # Stats
    if any(kw in msg for kw in ("form", "stats", "how is", "how are", "results for")):
        # Try to extract a team name (everything after the keyword)
        for kw in ("form of", "stats for", "how is", "how are", "results for", "form"):
            if kw in msg:
                team = msg.split(kw, 1)[1].strip().rstrip("?. ")
                if team:
                    return {
                        "intent": "stats",
                        "params": {"team": team.title()},
                        "reply": f"Checking {team.title()} form...",
                    }
        return {
            "intent": "chat",
            "params": {},
            "reply": "Which team do you want to check? e.g. 'Arsenal form'",
        }

    # Leagues / timeframe
    if "league" in msg:
        return {"intent": "leagues", "params": {}, "reply": "Here are the available leagues."}
    if any(kw in msg for kw in ("timeframe", "window", "today", "weekend", "time")):
        return {"intent": "timeframe", "params": {}, "reply": "Let's set your time window."}

    # Default chat
    return {
        "intent": "chat",
        "params": {},
        "reply": "I'm SportyBot — your football betting assistant. Try asking for picks (e.g. '10 odds') or type /help to see commands.",
    }
