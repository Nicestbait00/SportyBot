"""
SportyBet Event Mapper
Fetches upcoming events from SportyBet's API and provides:
- Team name → event ID mapping
- Market + outcome ID lookup for booking
- Real odds from SportyBet

Cache: 2 hours (events change as kickoff approaches)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "sportybet"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SPORTYBET_API = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.sportybet.com/ng/sport/football",
}

# Cache file for the full event index
_INDEX_FILE = CACHE_DIR / "event_index.json"
_INDEX_TTL = 7200  # 2 hours


_ALIASES = {
    "wolverhampton wanderers": "wolves",
    "wolverhampton": "wolves",
    "tottenham hotspur": "tottenham",
    "tottenham hotspur fc": "tottenham",
    "brighton & hove albion": "brighton",
    "brighton and hove albion": "brighton",
    "brighton & hove albion fc": "brighton",
    "newcastle united": "newcastle",
    "newcastle united fc": "newcastle",
    "nottingham forest": "nott forest",
    "nottingham forest fc": "nott forest",
    "west ham united": "west ham",
    "manchester united": "man utd",
    "manchester city": "man city",
    "atletico madrid": "atl madrid",
    "atlético madrid": "atl madrid",
    "atletico de madrid": "atl madrid",
    "borussia dortmund": "dortmund",
    "bayer 04 leverkusen": "leverkusen",
    "bayer leverkusen": "leverkusen",
    "rb leipzig": "leipzig",
    "paris saint-germain": "paris sg",
    "paris saint germain": "paris sg",
    "fc bayern münchen": "bayern munich",
    "bayern münchen": "bayern munich",
    "inter miami": "inter miami",
    "internazionale": "inter",
    "ssc napoli": "napoli",
    "fc barcelona": "barcelona",
    "real madrid cf": "real madrid",
    "afc bournemouth": "bournemouth",
    "crystal palace fc": "crystal palace",
    "leicester city": "leicester",
    "sunderland afc": "sunderland",
    "ipswich town": "ipswich",
}


def _normalize_name(name: str) -> str:
    """Normalize a team name for fuzzy matching."""
    name = name.lower().strip()
    # Check aliases first
    if name in _ALIASES:
        return _ALIASES[name]
    # Remove common suffixes
    for suffix in [" fc", " cf", " sc", " afc", " sv", " bv"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    for prefix in ["fc ", "cf ", "sc ", "afc "]:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
    # Check aliases again after stripping
    if name in _ALIASES:
        return _ALIASES[name]
    return name


def fetch_all_events(
    max_pages: int = 10,
    allowed_tournaments: list[str] | None = None,
) -> list[dict]:
    """
    Fetch all upcoming football events from SportyBet.
    Returns a flat list of event dicts with team names, IDs, markets, and odds.

    allowed_tournaments: optional list of tournament name substrings.
        When provided, only events whose tournament name contains one of the
        substrings (case-insensitive) are included.
    """
    # Pre-lowercase the filter strings once for fast comparison
    _filters = [s.lower() for s in allowed_tournaments] if allowed_tournaments else None

    all_events = []

    for page in range(1, max_pages + 1):
        try:
            r = requests.get(
                SPORTYBET_API,
                params={
                    "sportId": "sr:sport:1",
                    "marketId": "1,18,29,10",  # 1X2, Over/Under, GG/NG, Double Chance
                    "pageSize": 100,
                    "pageNum": page,
                },
                headers=HEADERS,
                timeout=15,
            )
            data = r.json()
            if data.get("bizCode") != 10000:
                break

            tournaments = data.get("data", {}).get("tournaments", [])
            if not tournaments:
                break

            page_events = 0
            for t in tournaments:
                tournament_name = t.get("name", "")

                # Skip entire tournament if it doesn't match the filter
                # Uses exact match (==) not substring to avoid "Serie A" matching "Brasileiro Serie A"
                if _filters:
                    t_lower = tournament_name.lower().strip()
                    if not any(f == t_lower for f in _filters):
                        continue

                for e in t.get("events", []):
                    event = _parse_event(e, tournament_name)
                    if event:
                        all_events.append(event)
                        page_events += 1

            if page_events == 0:
                break

        except Exception as ex:
            logger.warning(f"SportyBet fetch page {page} failed: {ex}")
            break

    logger.info(f"Fetched {len(all_events)} SportyBet events")
    return all_events


def _parse_event(raw: dict, tournament: str) -> Optional[dict]:
    """Parse a raw SportyBet event into our format."""
    home = raw.get("homeTeamName", "")
    away = raw.get("awayTeamName", "")
    event_id = raw.get("eventId", "")

    if not home or not away or not event_id:
        return None

    # Parse markets
    # Note: Over/Under (market 18) appears MULTIPLE times — one per goal line.
    # We key by "marketId|specifier" to keep all lines (e.g. "18|total=1.5", "18|total=2.5")
    markets = {}
    for m in raw.get("markets", []):
        mid = str(m.get("marketId", m.get("id", "")))
        market_name = m.get("marketName", m.get("desc", ""))
        market_specifier = m.get("specifier", "")
        outcomes = {}
        for o in m.get("outcomes", []):
            oid = str(o.get("outcomeId", o.get("id", "")))
            outcomes[oid] = {
                "id": oid,
                "name": o.get("outcomeName", o.get("desc", "")),
                "odds": o.get("odds", "0"),
                "specifier": market_specifier,  # Inherit from market level
            }
        # For markets with specifiers (Over/Under lines), use compound key
        if market_specifier:
            dict_key = f"{mid}|{market_specifier}"
        else:
            dict_key = mid
        markets[dict_key] = {
            "id": mid,
            "name": market_name,
            "outcomes": outcomes,
            "specifier": market_specifier,
        }

    return {
        "eventId": event_id,
        "home": home,
        "away": away,
        "home_norm": _normalize_name(home),
        "away_norm": _normalize_name(away),
        "tournament": tournament,
        "estimateStartTime": raw.get("estimateStartTime", 0),
        "markets": markets,
    }


def build_event_index(force: bool = False) -> dict:
    """
    Build/load the event index: normalized team names → event data.
    Cached to disk for 2 hours.
    """
    # Check cache
    if not force and _INDEX_FILE.exists():
        try:
            data = json.loads(_INDEX_FILE.read_text())
            if time.time() - data.get("_ts", 0) < _INDEX_TTL:
                return data.get("index", {})
        except (json.JSONDecodeError, KeyError):
            pass

    # Fetch fresh
    events = fetch_all_events()

    # Build index keyed by "home_norm|away_norm"
    index = {}
    for e in events:
        key = f"{e['home_norm']}|{e['away_norm']}"
        index[key] = e

        # Also index by partial names for fuzzy matching
        # e.g., "arsenal" alone could match
        index[f"_home_{e['home_norm']}"] = e
        index[f"_away_{e['away_norm']}"] = e

    # Save cache
    _INDEX_FILE.write_text(json.dumps({"_ts": time.time(), "index": index}))
    logger.info(f"Built SportyBet event index: {len(events)} events")

    return index


def find_event(home_name: str, away_name: str) -> Optional[dict]:
    """
    Find a SportyBet event by team names.
    Uses fuzzy matching against the cached index.
    Returns the event dict with eventId, markets, odds, etc.
    """
    index = build_event_index()

    home_norm = _normalize_name(home_name)
    away_norm = _normalize_name(away_name)

    # Exact match
    key = f"{home_norm}|{away_norm}"
    if key in index:
        return index[key]

    # Try substring matching
    for idx_key, event in index.items():
        if idx_key.startswith("_"):
            continue
        eh = event["home_norm"]
        ea = event["away_norm"]

        # Check if our names are substrings of SportyBet names or vice versa
        home_match = (home_norm in eh or eh in home_norm)
        away_match = (away_norm in ea or ea in away_norm)

        if home_match and away_match:
            return event

    # Try with just the first word of each team (e.g., "crystal" matches "crystal palace")
    home_first = home_norm.split()[0] if home_norm else ""
    away_first = away_norm.split()[0] if away_norm else ""

    for idx_key, event in index.items():
        if idx_key.startswith("_"):
            continue
        eh = event["home_norm"]
        ea = event["away_norm"]

        home_match = (home_first and (home_first == eh.split()[0] if eh else False))
        away_match = (away_first and (away_first == ea.split()[0] if ea else False))

        if home_match and away_match:
            return event

    # Try word overlap matching — require majority of words to match
    home_words = set(home_norm.split())
    away_words = set(away_norm.split())

    best_event = None
    best_score = 0

    for idx_key, event in index.items():
        if idx_key.startswith("_"):
            continue
        eh_words = set(event["home_norm"].split())
        ea_words = set(event["away_norm"].split())

        home_overlap = len(home_words & eh_words)
        away_overlap = len(away_words & ea_words)

        # Require at least half the words to match on each side
        home_min = max(1, min(len(home_words), len(eh_words)) // 2 + 1)
        away_min = max(1, min(len(away_words), len(ea_words)) // 2 + 1)

        if home_overlap >= home_min and away_overlap >= away_min:
            score = home_overlap + away_overlap
            if score > best_score:
                best_score = score
                best_event = event

    return best_event


def filter_events(
    events: list[dict],
    league_ids: list[int] | None = None,
    timeframe: str = "7days",
) -> list[dict]:
    """
    Filter SportyBet events by league and timeframe.

    league_ids: list of our internal league IDs (e.g. [39, 140]) — maps to tournament names.
                None or empty means ALL leagues (no filter).
    timeframe: preset key from TIMEFRAME_PRESETS ("today", "tomorrow", "weekend", "7days", "14days")
    """
    from datetime import datetime, timedelta

    # Lazy import to avoid circular dependency
    from config import LEAGUE_SPORTYBET_NAMES, TIMEFRAME_PRESETS

    now = datetime.now()
    now_ts = now.timestamp() * 1000  # SportyBet uses ms timestamps

    # ── Time window ──
    preset = TIMEFRAME_PRESETS.get(timeframe, TIMEFRAME_PRESETS["7days"])

    if timeframe == "weekend":
        # Friday 6pm → Sunday 11pm
        days_until_friday = (4 - now.weekday()) % 7
        friday_6pm = now.replace(hour=18, minute=0, second=0, microsecond=0) + timedelta(days=days_until_friday)
        if friday_6pm < now:
            # We're past Friday 6pm — use current time as start
            start_ts = now_ts
        else:
            start_ts = friday_6pm.timestamp() * 1000
        sunday_11pm = friday_6pm.replace(hour=23, minute=0) + timedelta(days=2)
        end_ts = sunday_11pm.timestamp() * 1000
    elif timeframe == "tomorrow":
        tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = tomorrow_start.timestamp() * 1000
        end_ts = (tomorrow_start + timedelta(hours=24)).timestamp() * 1000
    else:
        start_ts = now_ts
        hours = preset.get("hours", 168)
        end_ts = (now + timedelta(hours=hours)).timestamp() * 1000

    # ── League matching ──
    allowed_tournament_parts = []
    if league_ids:
        for lid in league_ids:
            parts = LEAGUE_SPORTYBET_NAMES.get(lid, [])
            allowed_tournament_parts.extend(parts)

    filtered = []
    for ev in events:
        # Time filter
        kick_off = ev.get("estimateStartTime", 0)
        if kick_off < start_ts or kick_off > end_ts:
            continue

        # League filter — exact match (skip if no filter set — show all)
        if allowed_tournament_parts:
            tournament_lower = ev.get("tournament", "").lower().strip()
            if not any(part == tournament_lower for part in allowed_tournament_parts):
                continue

        filtered.append(ev)

    logger.info(f"Filtered {len(events)} → {len(filtered)} events (leagues={league_ids}, timeframe={timeframe})")
    return filtered


def build_booking_selection(event: dict, market_type: str, pick: str) -> Optional[dict]:
    """
    Build a SportyBet booking selection from an event and pick.

    market_type: "1X2", "Over/Under", "GG/NG"
    pick: "Home", "Away", "Draw", "Over (total=1.5)", "Over (total=2.5)", "GG", etc.

    Returns dict ready for the SportyBet share API:
        {"eventId": "sr:match:XXX", "marketId": "1", "outcomeId": "1", "specifier": ""}
    """
    markets = event.get("markets", {})
    pick_lower = pick.lower()

    # Determine market ID and outcome ID
    if "1x2" in market_type.lower() or "winner" in market_type.lower():
        market = markets.get("1", {})
        outcomes = market.get("outcomes", {})

        if "home" in pick_lower or pick_lower == "1":
            outcome = outcomes.get("1", {})
        elif "away" in pick_lower or pick_lower == "2":
            outcome = outcomes.get("3", {})
        elif "draw" in pick_lower or pick_lower == "x":
            outcome = outcomes.get("2", {})
        else:
            return None

        return {
            "eventId": event["eventId"],
            "marketId": "1",
            "outcomeId": outcome.get("id", "1"),
            "specifier": "",
            "odds": outcome.get("odds", "0"),
        }

    elif "over" in market_type.lower() or "under" in market_type.lower() or "over" in pick_lower or "under" in pick_lower:
        # Determine specifier (total=1.5, total=2.5, etc.)
        specifier = ""
        if "total=" in pick_lower:
            try:
                specifier = pick_lower.split("(")[1].rstrip(")")
            except (IndexError, ValueError):
                specifier = "total=2.5"
        elif "0.5" in pick_lower:
            specifier = "total=0.5"
        elif "1.5" in pick_lower:
            specifier = "total=1.5"
        elif "2.5" in pick_lower:
            specifier = "total=2.5"
        elif "3.5" in pick_lower:
            specifier = "total=3.5"
        else:
            specifier = "total=2.5"

        # Look up the specific goal line via compound key "18|total=X.5"
        market = markets.get(f"18|{specifier}", {})
        if not market:
            # Fallback: try plain "18"
            market = markets.get("18", {})

        is_over = "over" in pick_lower
        outcome_id = "12" if is_over else "13"

        outcomes = market.get("outcomes", {})
        odds = outcomes.get(outcome_id, {}).get("odds", "0")

        return {
            "eventId": event["eventId"],
            "marketId": "18",
            "outcomeId": outcome_id,
            "specifier": specifier,
            "odds": odds,
        }

    elif "gg" in market_type.lower() or "ng" in market_type.lower() or "btts" in market_type.lower() or "gg" in pick_lower:
        # GG/NG is market 29, NOT 14 (14 = Handicap)
        market = markets.get("29", {})
        outcomes = market.get("outcomes", {})

        if "gg" in pick_lower or "yes" in pick_lower:
            outcome_id = "74"
        else:
            outcome_id = "76"

        odds = outcomes.get(outcome_id, {}).get("odds", "0")

        return {
            "eventId": event["eventId"],
            "marketId": "29",
            "outcomeId": outcome_id,
            "specifier": "",
            "odds": odds,
        }

    return None


def create_booking_code(selections: list[dict]) -> Optional[str]:
    """
    Create a SportyBet booking code from a list of selections.
    Each selection should have: eventId, marketId, outcomeId, specifier.
    Returns the booking code string or None on failure.
    """
    if not selections:
        return None

    payload = {
        "selections": [
            {
                "eventId": s["eventId"],
                "marketId": str(s["marketId"]),
                "outcomeId": str(s["outcomeId"]),
                "specifier": s.get("specifier", ""),
            }
            for s in selections
        ]
    }

    try:
        r = requests.post(
            "https://www.sportybet.com/api/ng/orders/share",
            headers={**HEADERS, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        resp = r.json()
        if resp.get("bizCode") == 10000:
            return resp.get("data", {}).get("shareCode")
        else:
            logger.warning(f"Booking failed: {resp.get('message', 'unknown')}")
            return None
    except Exception as e:
        logger.warning(f"Booking request failed: {e}")
        return None
