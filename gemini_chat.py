"""
LLM conversational layer for SportyBot.

Handles intent parsing and natural language conversation via OpenRouter.
The LLM does NOT make pick decisions — only the deterministic scoring engine does that.
"""

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")

SYSTEM_PROMPT = """\
You are SportyBot, a sharp but friendly football betting assistant on Telegram.

Your personality: Knowledgeable, confident, slightly casual — like a mate who genuinely knows football data. You're warm but efficient. Use conversational language, not corporate speak. Short replies (1-2 sentences). You can use occasional slang or football expressions but keep it natural.

Your ONLY role is to understand what the user wants and return structured JSON.
You do NOT make pick decisions — the deterministic scoring engine handles that.

Return a JSON object (no markdown fences, no extra text) with these fields:
{
  "intent": "<one of: pick, check, leagues, timeframe, stats, chat>",
  "params": { ... },
  "reply": "A short conversational message to show the user"
}

Intent definitions:
- "pick": The user wants betting picks or a combo. Extract from their message:
  params.target_odds (float or null) — e.g. "give me 10 odds" → 10.0
  params.ticket_type ("single"|"multiple"|null) — set when the user clearly asks for one ticket or several
  params.ticket_count (int or null) — set when the user explicitly asks for multiple tickets, e.g. "3 tickets"
  params.ticket_mode ("unique"|"dynamic"|null) — "unique" for same games/different markets, "dynamic" for different games
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
- If the user says "hi", "hello", "hey" etc. → intent "chat", warm greeting reply. Be personable.
- If the user says something like "get me picks", "find me a bet", "5 odds", "what's good today", "anything hitting?" → intent "pick".
- If the user says "2 tickets", "3 unique tickets", "multiple tickets", "dynamic tickets" etc. → intent "pick" and populate the ticket fields.
- If the user mentions a booking code (alphanumeric ~6-10 chars) → intent "check".
- If the user asks "how is Arsenal doing", "Chelsea form" → intent "stats".
- If ambiguous, default to "chat" with a helpful reply asking what they need.
- Keep replies warm but concise (1-2 sentences). Sound like a knowledgeable friend, not a robot.
- ALWAYS return valid JSON only. No markdown code fences.
"""


# Prompt for understanding user messages during an active combo review session
REVIEW_PROMPT = """\
You are SportyBot's review assistant. The user is currently reviewing a {combo_size}-pick betting combo.

Your job: understand what the user wants to do with their current combo and return structured JSON.

The current combo has {combo_size} picks numbered 1-{combo_size}.

Return JSON (no markdown fences):
{{
  "intent": "<one of: edit, book, explain, reshuffle, chat>",
  "params": {{ ... }},
  "reply": "A short, warm response to the user"
}}

Intent definitions:
- "edit": The user wants to modify the combo.
  params.actions: list of actions, each:
    {{"type": "remove", "index": <1-based pick number>}}
    {{"type": "change", "index": <1-based pick number>, "market": "over"|"home"|"away"|"btts"|"draw", "threshold": <float for over, e.g. 0.5, 1.5, 2.5> or null}}
  Examples:
    "take out 1 and 3" → actions: [{{"type":"remove","index":1}},{{"type":"remove","index":3}}]
    "I don't like game 5" → actions: [{{"type":"remove","index":5}}]
    "make 2 an over 1.5" → actions: [{{"type":"change","index":2,"market":"over","threshold":1.5}}]
    "switch 4 to home win" → actions: [{{"type":"change","index":4,"market":"home","threshold":null}}]
    "get rid of the last one and change 3 to btts" → actions: [{{"type":"remove","index":{combo_size}}},{{"type":"change","index":3,"market":"btts","threshold":null}}]

- "book": The user wants to confirm and book the combo.
  Triggered by: "book it", "let's go", "send it", "looks good", "confirm", "yes", "lock it", "place it", "bet", "cool", "perfect", "nice", "that works", "I'm happy", "done", or any affirmative/approval language.
  params: {{}}

- "explain": The user wants to understand WHY a specific pick (or all picks) were chosen.
  Triggered by: "why pick 3?", "explain", "tell me more about 5", "why that one?", "break it down", "synopsis", "why?", "how confident are you about 2?"
  params.picks: list of 1-based pick numbers to explain, or [] for all picks

- "reshuffle": The user wants different picks for the same target.
  Triggered by: "give me different ones", "reshuffle", "try again", "new picks", "other options", "not feeling these", "different combo"
  params: {{}}

- "chat": The user is asking something unrelated or you genuinely can't understand.
  If you're unsure what they mean, ask for clarification in the reply. Be helpful, not dismissive.
  params: {{}}

Rules:
- Be smart about understanding intent. "nah" after seeing picks = "reshuffle". "that's fire" = "book".
- If the user says something ambiguous like "hmm" or "idk", ask what they'd like to do in a friendly way.
- Keep replies warm and concise. You're a knowledgeable mate, not a customer service bot.
- ALWAYS return valid JSON only.
"""


# ── OpenRouter API call ──────────────────────────────────────────────────────

def _call_openrouter(system_prompt: str, user_text: str) -> str | None:
    """Call OpenRouter and return raw text response, or None on failure."""
    if not OPENROUTER_API_KEY:
        return None
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning(f"OpenRouter error {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"OpenRouter call failed: {e}")
        return None


def _parse_json_response(text: str, allowed_intents: set, default_intent: str = "chat") -> dict:
    """Parse raw LLM text into intent/params/reply dict."""
    if not text:
        return {"intent": default_intent, "params": {}, "reply": ""}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"LLM returned non-JSON: {text[:200]}")
        return {
            "intent": default_intent,
            "params": {},
            "reply": text[:500] if text else "",
        }

    if "intent" not in result:
        result["intent"] = default_intent
    if "params" not in result:
        result["params"] = {}
    if "reply" not in result:
        result["reply"] = ""

    if result["intent"] not in allowed_intents:
        result["intent"] = default_intent

    return result


# ── Public API ───────────────────────────────────────────────────────────────

def chat(message: str, context: dict = None) -> dict:
    """
    Parse a user message for intent using OpenRouter.

    Returns dict with keys: intent, params, reply
    """
    user_text = message
    if context:
        user_text = f"[User context: {json.dumps(context)}]\n\nUser message: {message}"

    raw = _call_openrouter(SYSTEM_PROMPT, user_text)
    if not raw:
        return _fallback_parse(message)

    return _parse_json_response(
        raw, {"pick", "check", "leagues", "timeframe", "stats", "chat"}
    )


def parse_review_message(message: str, combo_size: int) -> dict:
    """Parse a user message during combo review."""
    prompt = REVIEW_PROMPT.format(combo_size=combo_size)
    raw = _call_openrouter(prompt, f"User says: {message}")
    if not raw:
        return _fallback_review_parse(message, combo_size)

    return _parse_json_response(
        raw, {"edit", "book", "explain", "reshuffle", "chat"}
    )


GAME_DIALOGUE_PROMPT = """\
You are SportyBot, discussing a specific football match with the user during combo review.

**Current pick:**
{current_pick}

**Available alternatives for this match:**
{alternatives}

The user is asking about this specific game. Your job:
1. Understand their intent and return structured JSON
2. Give data-backed opinions — reference confidence scores, odds, and analysis reasons

**Intents:**
- "compare": User wants to compare markets (e.g. "what about over 1.5?", "how does GG look?")
  → Show a brief comparison with confidence and reasoning. Params: {{"market": "the market they asked about"}}
- "switch": User wants to switch to a different market (e.g. "change to double chance", "switch it")
  → Params: {{"market": "market name", "pick": "specific outcome"}}
- "keep": User wants to keep the current pick (e.g. "keep it", "nah leave it", "it's fine")
- "chat": General question about the game or anything else
  → Give a warm, data-backed reply

Respond ONLY with valid JSON:
{{"intent": "compare|switch|keep|chat", "params": {{}}, "reply": "your message"}}
"""


def parse_game_dialogue(message: str, current_pick: dict, alternatives: list[dict]) -> dict:
    """Parse a user message during per-game dialogue."""
    current_str = (
        f"{current_pick.get('home', '')} vs {current_pick.get('away', '')}\n"
        f"Market: {current_pick.get('market', '')} | Pick: {current_pick.get('pick', '')} "
        f"| Odds: {current_pick.get('odds', 0):.2f} | Confidence: {current_pick.get('confidence', '?')}%\n"
        f"Reasons: {'; '.join(current_pick.get('analysis_reasons', [])[:3])}"
    )

    alt_lines = []
    for a in sorted(alternatives, key=lambda x: x.get("confidence", 0), reverse=True)[:8]:
        alt_lines.append(
            f"- {a.get('market', '')}: {a.get('pick', '')} @ {a.get('odds', 0):.2f} "
            f"[{a.get('confidence', '?')}%] — {'; '.join(a.get('analysis_reasons', [])[:1])}"
        )
    alt_str = "\n".join(alt_lines) if alt_lines else "No alternatives scored for this match."

    prompt = GAME_DIALOGUE_PROMPT.format(current_pick=current_str, alternatives=alt_str)
    raw = _call_openrouter(prompt, f"User says: {message}")
    if not raw:
        return _fallback_game_dialogue(message, alternatives)

    return _parse_json_response(
        raw, {"compare", "switch", "keep", "chat"}
    )


# ── Fallbacks (keyword-based, no API needed) ────────────────────────────────

def _fallback_review_parse(message: str, combo_size: int) -> dict:
    """Regex fallback for review message parsing."""
    msg = message.lower().strip()

    book_words = ("book", "confirm", "yes", "keep", "lock", "go ahead", "place",
                  "bet", "lets go", "let's go", "do it", "send it", "cool",
                  "perfect", "nice", "that works", "looks good", "done", "fire",
                  "lgtm", "good", "great", "ok", "okay", "sure", "yep", "yeah",
                  "absolutely", "definitely", "for sure", "ship it", "i'm happy")
    if msg in book_words or any(msg.startswith(w) for w in ("book ", "confirm ", "yes ")):
        return {"intent": "book", "params": {}, "reply": ""}

    reshuffle_words = ("reshuffle", "shuffle", "different", "other", "try again",
                       "new picks", "not feeling", "nah", "nope", "meh",
                       "give me different", "other options", "change all")
    if any(w in msg for w in reshuffle_words):
        return {"intent": "reshuffle", "params": {}, "reply": ""}

    explain_words = ("why", "explain", "tell me more", "break it down", "synopsis",
                     "how confident", "reasoning", "what makes", "data behind")
    if any(w in msg for w in explain_words):
        nums = re.findall(r"\d+", msg)
        picks = [int(n) for n in nums if 1 <= int(n) <= combo_size]
        return {"intent": "explain", "params": {"picks": picks}, "reply": ""}

    if any(w in msg for w in ("remove", "drop", "delete", "exclude", "take out",
                               "get rid", "change", "swap", "switch", "make")):
        return {"intent": "edit", "params": {}, "reply": ""}

    return {"intent": "chat", "params": {}, "reply": ""}


def _fallback_game_dialogue(message: str, alternatives: list[dict]) -> dict:
    """Regex fallback for game dialogue parsing."""
    msg = message.lower().strip()

    if any(w in msg for w in ("keep", "leave", "fine", "ok", "good", "nah")):
        return {"intent": "keep", "params": {}, "reply": ""}

    switch_match = re.search(r"(?:switch|change|swap)\s+(?:to\s+)?(.+)", msg)
    if switch_match:
        market = switch_match.group(1).strip()
        return {"intent": "switch", "params": {"market": market, "pick": ""}, "reply": ""}

    compare_match = re.search(r"(?:what about|how about|how does|how is|compare)\s+(.+?)(?:\?|$)", msg)
    if compare_match:
        market = compare_match.group(1).strip()
        return {"intent": "compare", "params": {"market": market}, "reply": ""}

    for alt in alternatives[:8]:
        alt_market = alt.get("market", "").lower()
        alt_pick = alt.get("pick", "").lower()
        if alt_market in msg or alt_pick in msg:
            return {"intent": "compare", "params": {"market": alt_market}, "reply": ""}

    return {"intent": "chat", "params": {}, "reply": ""}


def _parse_market_slots(msg: str) -> list | None:
    """Extract market slot distribution from a natural language message."""
    slots = []

    win_match = re.search(r"(\d+)\s*(?:straight\s+)?win", msg)
    if win_match:
        slots.append({"market": "1X2", "count": int(win_match.group(1))})

    over_matches = re.finditer(r"(\d+)\s*over\s*(\d+(?:\.\d+)?)", msg)
    for m in over_matches:
        slots.append({
            "market": "Over/Under",
            "count": int(m.group(1)),
            "threshold": float(m.group(2)),
        })

    btts_match = re.search(r"(\d+)\s*(?:btts|gg|both\s+teams)", msg)
    if btts_match:
        slots.append({"market": "GG/NG", "count": int(btts_match.group(1))})

    rest_match = re.search(r"rest\s+(?:over\s*)?(\d+(?:\.\d+)?)", msg)
    if rest_match:
        slots.append({
            "market": "Over/Under",
            "threshold": float(rest_match.group(1)),
            "fill": True,
        })

    if len(slots) >= 2:
        return slots
    return None


def _fallback_parse(message: str) -> dict:
    """Basic keyword-based fallback when API is unavailable."""
    msg = message.lower().strip()

    if msg in ("hi", "hello", "hey", "yo", "sup", "good morning", "good evening"):
        return {
            "intent": "chat",
            "params": {},
            "reply": "Hey! I'm SportyBot. Ask me for picks, check a booking code, or just chat about football.",
        }

    odds_match = re.search(r"(\d+(?:\.\d+)?)\s*odds", msg)
    if odds_match or any(kw in msg for kw in ("pick", "bet", "combo", "find me", "get me")):
        target = float(odds_match.group(1)) if odds_match else None
        market_slots = _parse_market_slots(msg)
        ticket_type = None
        ticket_count = None
        ticket_mode = None

        ticket_count_match = re.search(r"\b([2-5])\s*tickets?\b", msg)
        if ticket_count_match:
            ticket_type = "multiple"
            ticket_count = int(ticket_count_match.group(1))
        elif "multiple ticket" in msg or "multiple bet" in msg:
            ticket_type = "multiple"
        elif "single ticket" in msg:
            ticket_type = "single"

        if "unique ticket" in msg or "unique bets" in msg:
            ticket_type = "multiple"
            ticket_mode = "unique"
        elif "dynamic ticket" in msg or "different games" in msg:
            ticket_type = "multiple"
            ticket_mode = "dynamic"

        market = None
        if not market_slots:
            if "over" in msg:
                market = "over_2.5"
            elif "home" in msg or "win" in msg:
                market = "1x2"

        return {
            "intent": "pick",
            "params": {
                "target_odds": target,
                "ticket_type": ticket_type,
                "ticket_count": ticket_count,
                "ticket_mode": ticket_mode,
                "market": market,
                "market_slots": market_slots,
            },
            "reply": f"Looking for picks{' at ' + str(target) + ' odds' if target else ''}...",
        }

    code_match = re.search(r"\b([A-Za-z0-9]{6,10})\b", msg)
    if "check" in msg or "code" in msg or "analyze" in msg:
        return {
            "intent": "check",
            "params": {"code": code_match.group(1) if code_match else None},
            "reply": "Let me analyze that for you.",
        }

    if any(kw in msg for kw in ("form", "stats", "how is", "how are", "results for")):
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

    if "league" in msg:
        return {"intent": "leagues", "params": {}, "reply": "Here are the available leagues."}
    if any(kw in msg for kw in ("timeframe", "window", "today", "weekend", "time")):
        return {"intent": "timeframe", "params": {}, "reply": "Let's set your time window."}

    return {
        "intent": "chat",
        "params": {},
        "reply": "I'm SportyBot — your football betting assistant. Try asking for picks (e.g. '10 odds') or type /help to see commands.",
    }
