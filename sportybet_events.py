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
    max_pages: int = 20,
    allowed_tournaments: list[str] | None = None,
) -> list[dict]:
    """
    Fetch all upcoming football events from SportyBet.
    Returns a flat list of event dicts with team names, IDs, markets, and odds.

    allowed_tournaments: optional list of exact tournament names.
        When provided, only events whose tournament name exactly matches one
        of the entries (case-insensitive) are included.
    """
    # Pre-lowercase the filter strings once for fast comparison
    _filters = [s.lower() for s in allowed_tournaments] if allowed_tournaments else None

    all_events = []
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        try:
            r = requests.get(
                SPORTYBET_API,
                params={
                    "sportId": "sr:sport:1",
                    "marketId": "1,10,11,14,18,19,20,21,26,29,31,32,35,36,37,45,47,60,68,75,854,855,856,857,858,859,860,861,862",
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
                # Uses exact match — all name variants must be listed in LEAGUE_SPORTYBET_NAMES
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
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
            else:
                consecutive_empty = 0

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


def clear_cache():
    """Delete the SportyBet event index cache so next fetch is fresh."""
    if _INDEX_FILE.exists():
        _INDEX_FILE.unlink()
        logger.info("Cleared SportyBet event index cache")


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

    market_type: "1X2", "Over/Under", "GG/NG", "Double Chance", "Draw No Bet",
                 "Odd/Even", "1X2 & GG/NG", "HT 1X2", "HT/FT", "HT Over/Under",
                 "HT GG/NG", "Home Clean Sheet", "Away Clean Sheet",
                 "Over/Under & GG/NG", "1X2 & Over/Under", "Correct Score",
                 "Handicap", "Exact Goals", "Home Team Goals", "Away Team Goals"
    pick: "Home", "Away", "Draw", "Over (total=1.5)", "GG", "Odd", "Even",
          "Home & GG", "H/H", etc.

    Returns dict ready for the SportyBet share API:
        {"eventId": "sr:match:XXX", "marketId": "1", "outcomeId": "1", "specifier": ""}
    """
    markets = event.get("markets", {})
    pick_lower = pick.lower()
    mt_lower = market_type.lower()

    def _make(market_id: str, outcome_id: str, specifier: str = "", odds: str = "0"):
        return {
            "eventId": event["eventId"],
            "marketId": market_id,
            "outcomeId": outcome_id,
            "specifier": specifier,
            "odds": odds,
        }

    def _find_outcome(market_dict_key: str, outcome_id: str, spec: str = ""):
        mkt = markets.get(market_dict_key, {})
        o = mkt.get("outcomes", {}).get(outcome_id, {})
        return o.get("odds", "0")

    # ── 1X2 (market 1) ──
    if mt_lower == "1x2":
        market = markets.get("1", {})
        outcomes = market.get("outcomes", {})
        if "home" in pick_lower or pick_lower == "1":
            oid = "1"
        elif "away" in pick_lower or pick_lower == "2":
            oid = "3"
        elif "draw" in pick_lower or pick_lower == "x":
            oid = "2"
        else:
            return None
        return _make("1", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Double Chance (market 10) ──
    if mt_lower == "double chance":
        market = markets.get("10", {})
        outcomes = market.get("outcomes", {})
        if "1x" in pick_lower or "home or draw" in pick_lower:
            oid = "9"
        elif "12" in pick_lower or "home or away" in pick_lower:
            oid = "10"
        elif "x2" in pick_lower or "draw or away" in pick_lower:
            oid = "11"
        else:
            return None
        return _make("10", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Draw No Bet (market 11) ──
    if mt_lower == "draw no bet":
        market = markets.get("11", {})
        outcomes = market.get("outcomes", {})
        oid = "4" if "home" in pick_lower else "5"
        return _make("11", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Handicap (market 14) ──
    if mt_lower == "handicap":
        # Pick format: "Home (1:0)" or "Away (0:1)" etc.
        specifier = ""
        if "hcp=" in pick_lower:
            try:
                specifier = pick_lower.split("(")[1].rstrip(")")
            except (IndexError, ValueError):
                specifier = "hcp=1:0"
        else:
            # Try to extract from pick text like "Home (1:0)"
            import re
            m = re.search(r'\((\d+:\d+)\)', pick)
            if m:
                specifier = f"hcp={m.group(1)}"
        mkt_key = f"14|{specifier}" if specifier else "14"
        mkt = markets.get(mkt_key, {})
        outcomes = mkt.get("outcomes", {})
        if "home" in pick_lower:
            oid = "1711"
        elif "draw" in pick_lower:
            oid = "1712"
        else:
            oid = "1713"
        return _make("14", oid, specifier, outcomes.get(oid, {}).get("odds", "0"))

    # ── Over/Under (market 18) — also handles HT Over/Under (68) ──
    if mt_lower == "over/under":
        specifier = _extract_total_specifier(pick_lower)
        mkt_key = f"18|{specifier}"
        mkt = markets.get(mkt_key, markets.get("18", {}))
        is_over = "over" in pick_lower
        oid = "12" if is_over else "13"
        odds = mkt.get("outcomes", {}).get(oid, {}).get("odds", "0")
        return _make("18", oid, specifier, odds)

    # ── Home Over/Under (market 19) ──
    if mt_lower == "home over/under":
        specifier = _extract_total_specifier(pick_lower)
        mkt_key = f"19|{specifier}"
        mkt = markets.get(mkt_key, markets.get("19", {}))
        is_over = "over" in pick_lower
        oid = "12" if is_over else "13"
        odds = mkt.get("outcomes", {}).get(oid, {}).get("odds", "0")
        return _make("19", oid, specifier, odds)

    # ── Away Over/Under (market 20) ──
    if mt_lower == "away over/under":
        specifier = _extract_total_specifier(pick_lower)
        mkt_key = f"20|{specifier}"
        mkt = markets.get(mkt_key, markets.get("20", {}))
        is_over = "over" in pick_lower
        oid = "12" if is_over else "13"
        odds = mkt.get("outcomes", {}).get(oid, {}).get("odds", "0")
        return _make("20", oid, specifier, odds)

    # ── HT Over/Under (market 68) ──
    if mt_lower in ("ht over/under", "1st half over/under"):
        specifier = _extract_total_specifier(pick_lower)
        mkt_key = f"68|{specifier}"
        mkt = markets.get(mkt_key, markets.get("68", {}))
        is_over = "over" in pick_lower
        oid = "12" if is_over else "13"
        odds = mkt.get("outcomes", {}).get(oid, {}).get("odds", "0")
        return _make("68", oid, specifier, odds)

    # ── GG/NG (market 29) ──
    if mt_lower in ("gg/ng", "btts"):
        market = markets.get("29", {})
        outcomes = market.get("outcomes", {})
        oid = "74" if ("gg" in pick_lower or "yes" in pick_lower) else "76"
        return _make("29", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── HT GG/NG (market 75) ──
    if mt_lower in ("ht gg/ng", "1st half gg/ng"):
        market = markets.get("75", {})
        outcomes = market.get("outcomes", {})
        oid = "74" if ("yes" in pick_lower or "gg" in pick_lower) else "76"
        return _make("75", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Odd/Even (market 26) ──
    if mt_lower == "odd/even":
        market = markets.get("26", {})
        outcomes = market.get("outcomes", {})
        oid = "70" if "odd" in pick_lower else "72"
        return _make("26", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Exact Goals (market 21) ──
    if mt_lower == "exact goals":
        # Pick format: "2" or "3+" etc
        market = markets.get("21", markets.get("21|variant=sr:exact_goals:6+", {}))
        # Search across all market 21 variants
        for k, mkt in markets.items():
            if mkt.get("id") == "21":
                for oid, o in mkt.get("outcomes", {}).items():
                    if o.get("name", "").lower().strip() == pick_lower.strip():
                        return _make("21", oid, mkt.get("specifier", ""), o.get("odds", "0"))
        return None

    # ── Home Team Goals (market 23) ──
    if mt_lower == "home team goals":
        for k, mkt in markets.items():
            if mkt.get("id") == "23":
                for oid, o in mkt.get("outcomes", {}).items():
                    if o.get("name", "").lower().strip() == pick_lower.strip():
                        return _make("23", oid, mkt.get("specifier", ""), o.get("odds", "0"))
        return None

    # ── Away Team Goals (market 24) ──
    if mt_lower == "away team goals":
        for k, mkt in markets.items():
            if mkt.get("id") == "24":
                for oid, o in mkt.get("outcomes", {}).items():
                    if o.get("name", "").lower().strip() == pick_lower.strip():
                        return _make("24", oid, mkt.get("specifier", ""), o.get("odds", "0"))
        return None

    # ── Home Clean Sheet (market 31) ──
    if mt_lower == "home clean sheet":
        market = markets.get("31", {})
        outcomes = market.get("outcomes", {})
        oid = "74" if "yes" in pick_lower else "76"
        return _make("31", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── Away Clean Sheet (market 32) ──
    if mt_lower == "away clean sheet":
        market = markets.get("32", {})
        outcomes = market.get("outcomes", {})
        oid = "74" if "yes" in pick_lower else "76"
        return _make("32", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── 1X2 & GG/NG (market 35) ──
    if mt_lower == "1x2 & gg/ng":
        market = markets.get("35", {})
        outcomes = market.get("outcomes", {})
        outcome_map = {
            "home & gg": "78", "home & yes": "78",
            "home & ng": "80", "home & no": "80",
            "draw & gg": "82", "draw & yes": "82",
            "draw & ng": "84", "draw & no": "84",
            "away & gg": "86", "away & yes": "86",
            "away & ng": "88", "away & no": "88",
        }
        oid = outcome_map.get(pick_lower, "")
        if not oid:
            # Fuzzy match
            for label, o_id in outcome_map.items():
                if all(w in pick_lower for w in label.split(" & ")):
                    oid = o_id
                    break
        if oid:
            return _make("35", oid, "", outcomes.get(oid, {}).get("odds", "0"))
        return None

    # ── Over/Under & GG/NG (market 36) ──
    if mt_lower == "over/under & gg/ng":
        specifier = "total=2.5"  # Default
        if "total=" in pick_lower:
            try:
                specifier = pick_lower.split("(")[1].rstrip(")")
            except (IndexError, ValueError):
                pass
        mkt_key = f"36|{specifier}"
        mkt = markets.get(mkt_key, markets.get("36", {}))
        outcomes = mkt.get("outcomes", {})
        if "over" in pick_lower and ("gg" in pick_lower or "yes" in pick_lower):
            oid = "90"
        elif "under" in pick_lower and ("gg" in pick_lower or "yes" in pick_lower):
            oid = "92"
        elif "over" in pick_lower and ("ng" in pick_lower or "no" in pick_lower):
            oid = "94"
        elif "under" in pick_lower and ("ng" in pick_lower or "no" in pick_lower):
            oid = "96"
        else:
            return None
        return _make("36", oid, specifier, outcomes.get(oid, {}).get("odds", "0"))

    # ── 1X2 & Over/Under (market 37) ──
    if mt_lower == "1x2 & over/under":
        specifier = _extract_total_specifier(pick_lower)
        mkt_key = f"37|{specifier}"
        mkt = markets.get(mkt_key, markets.get("37", {}))
        outcomes = mkt.get("outcomes", {})
        if "home" in pick_lower and "under" in pick_lower:
            oid = "794"
        elif "home" in pick_lower and "over" in pick_lower:
            oid = "796"
        elif "draw" in pick_lower and "under" in pick_lower:
            oid = "798"
        elif "draw" in pick_lower and "over" in pick_lower:
            oid = "800"
        elif "away" in pick_lower and "under" in pick_lower:
            oid = "802"
        elif "away" in pick_lower and "over" in pick_lower:
            oid = "804"
        else:
            return None
        return _make("37", oid, specifier, outcomes.get(oid, {}).get("odds", "0"))

    # ── HT 1X2 (market 60) ──
    if mt_lower in ("ht 1x2", "1st half 1x2"):
        market = markets.get("60", {})
        outcomes = market.get("outcomes", {})
        if "home" in pick_lower:
            oid = "1"
        elif "away" in pick_lower:
            oid = "3"
        else:
            oid = "2"
        return _make("60", oid, "", outcomes.get(oid, {}).get("odds", "0"))

    # ── HT/FT (market 47) ──
    if mt_lower in ("ht/ft", "half time/full time"):
        market = markets.get("47", {})
        outcomes = market.get("outcomes", {})
        ht_ft_map = {
            "h/h": "418", "home/home": "418",
            "h/d": "420", "home/draw": "420",
            "h/a": "422", "home/away": "422",
            "d/h": "424", "draw/home": "424",
            "d/d": "426", "draw/draw": "426",
            "d/a": "428", "draw/away": "428",
            "a/h": "430", "away/home": "430",
            "a/d": "432", "away/draw": "432",
            "a/a": "434", "away/away": "434",
        }
        oid = ht_ft_map.get(pick_lower.replace(" ", ""), "")
        if not oid:
            # Try fuzzy
            for label, o_id in ht_ft_map.items():
                if label.replace("/", "") in pick_lower.replace("/", "").replace(" ", ""):
                    oid = o_id
                    break
        if oid:
            return _make("47", oid, "", outcomes.get(oid, {}).get("odds", "0"))
        return None

    # ── Correct Score (market 45) ──
    if mt_lower == "correct score":
        market = markets.get("45", {})
        outcomes = market.get("outcomes", {})
        # Pick format: "1:0", "2:1", etc.
        import re
        score_match = re.search(r'(\d+:\d+)', pick)
        if score_match:
            score_str = score_match.group(1)
            for oid, o in outcomes.items():
                if o.get("name", "").strip() == score_str:
                    return _make("45", oid, "", o.get("odds", "0"))
        return None

    # ── Conditional OR markets (854-862) ──
    # All use outcome 74 (Yes) / 76 (No), specifier total=X.5 for 854-859
    _or_market_ids = {
        "home or over": "854", "home or under": "855",
        "draw or over": "856", "draw or under": "857",
        "away or over": "858", "away or under": "859",
        "home or gg": "860", "draw or gg": "861", "away or gg": "862",
    }
    if mt_lower in _or_market_ids:
        mid = _or_market_ids[mt_lower]
        pick_lower = pick.lower().strip()
        outcome_id = "74" if pick_lower == "yes" else "76" if pick_lower == "no" else "74"
        # For 854-859, need specifier
        if mid in ("854", "855", "856", "857", "858", "859"):
            spec = _extract_total_specifier(pick_lower) if "total=" in pick_lower else "total=2.5"
            market_key = f"{mid}|{spec}"
        else:
            market_key = mid
            spec = ""
        market = markets.get(market_key, markets.get(mid, {}))
        outcomes = market.get("outcomes", {})
        o = outcomes.get(outcome_id, {})
        return _make(mid, outcome_id, spec, o.get("odds", "0"))

    return None


def _extract_total_specifier(pick_lower: str) -> str:
    """Extract total=X.X specifier from a pick string."""
    if "total=" in pick_lower:
        try:
            return pick_lower.split("(")[1].rstrip(")")
        except (IndexError, ValueError):
            pass
    for val in ["0.5", "1.5", "2.5", "3.5", "4.5", "5.5"]:
        if val in pick_lower:
            return f"total={val}"
    return "total=2.5"


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
