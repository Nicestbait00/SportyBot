"""
SportyBot Data Collector
Fetches fixtures, form, H2H, and stats from API-Football v3.

All responses are cached to disk with configurable TTL.
Budget tracking persists across sessions via .cache/api_usage.json.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import requests

from config import (
    API_FOOTBALL_BASE, API_FOOTBALL_KEY, CACHE_DIR, CACHE_TTL_SECONDS,
    FOOTBALL_DATA_BASE, FOOTBALL_DATA_KEY, LEAGUE_TO_FD_CODE,
)


# ── Caching layer ────────────────────────────────────────────────────────────

def _cache_key(endpoint: str, params: dict) -> str:
    """Produce a deterministic filename for a request."""
    raw = f"{endpoint}|{json.dumps(params, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _read_cache(key: str, ttl_override: int = None) -> Optional[dict]:
    """Return cached JSON if it exists and is still fresh, else None."""
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ttl = ttl_override if ttl_override is not None else CACHE_TTL_SECONDS
        if time.time() - data.get("_ts", 0) < ttl:
            return data.get("payload")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _write_cache(key: str, payload: Any) -> None:
    """Persist a response to the cache directory."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps({"_ts": time.time(), "payload": payload}, indent=2))


# ── Persistent API budget tracking ───────────────────────────────────────────
_BUDGET_FILE = CACHE_DIR / "api_usage.json"
_API_DAILY_LIMIT = 80  # Stay under 100/day with safety margin
_last_call_time = 0.0
_MIN_CALL_INTERVAL = 7.0  # seconds between calls (free plan: 10 req/min)
_api_rate_limit_lock = threading.Lock()


def _load_budget() -> dict:
    """Load today's API usage from disk. Resets if date changed."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(_BUDGET_FILE.read_text())
        if data.get("date") == today:
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"date": today, "calls": 0}


def _save_budget(budget: dict) -> None:
    _BUDGET_FILE.write_text(json.dumps(budget, indent=2))


def _increment_budget() -> int:
    """Increment today's call count. Returns new total."""
    budget = _load_budget()
    budget["calls"] += 1
    _save_budget(budget)
    return budget["calls"]


def get_api_budget() -> dict:
    """Return today's API budget status."""
    budget = _load_budget()
    return {
        "date": budget["date"],
        "used": budget["calls"],
        "limit": _API_DAILY_LIMIT,
        "remaining": max(0, _API_DAILY_LIMIT - budget["calls"]),
    }


def get_api_call_count() -> int:
    """Return number of API calls made today (persisted across sessions)."""
    return _load_budget()["calls"]


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _api_get(endpoint: str, params: dict | None = None) -> dict:
    """
    Make a GET request to API-Football v3 with caching.

    Returns the full JSON body on success; raises RuntimeError on failure.
    """
    params = params or {}
    key = _cache_key(endpoint, params)

    cached = _read_cache(key)
    if cached is not None:
        return cached

    if not API_FOOTBALL_KEY:
        raise RuntimeError(
            "API_FOOTBALL_KEY is not set. "
            "Export it or add it to your .env file."
        )

    global _last_call_time
    with _api_rate_limit_lock:
        budget = get_api_budget()
        if budget["remaining"] <= 0:
            raise RuntimeError(
                f"API call limit reached ({_API_DAILY_LIMIT}/day). "
                "Cached data will still work. Try again tomorrow."
            )

        # Rate limiting: wait between calls
        elapsed = time.time() - _last_call_time
        if elapsed < _MIN_CALL_INTERVAL:
            wait = _MIN_CALL_INTERVAL - elapsed
            print(f"    [rate limit: waiting {wait:.0f}s]", end="\r", flush=True)
            time.sleep(wait)

        _increment_budget()
        _last_call_time = time.time()

    url = f"{API_FOOTBALL_BASE}/{endpoint}"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"API-Football request failed: {exc}") from exc

    body = resp.json()

    # API-Football wraps errors in the response body
    errors = body.get("errors")
    if errors:
        # errors can be a dict or a list depending on the error type
        raise RuntimeError(f"API-Football error: {errors}")

    _write_cache(key, body)
    return body


# ── Public API ───────────────────────────────────────────────────────────────

def get_fixtures(
    date: Optional[str] = None,
    league_id: Optional[int] = None,
    season: Optional[int] = None,
) -> list[dict]:
    """
    Fetch upcoming fixtures.

    Args:
        date:      Date string "YYYY-MM-DD". Defaults to today.
        league_id: API-Football league ID (e.g. 39 for Premier League).
        season:    Season year (e.g. 2025). Defaults to current year.

    Returns:
        List of fixture dicts with keys: id, date, home, away, league, odds.
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if season is None:
        season = _current_season()

    params: dict[str, Any] = {"date": date}
    if league_id is not None:
        params["league"] = league_id
        params["season"] = season

    body = _api_get("fixtures", params)
    fixtures = body.get("response", [])

    return [_normalize_fixture(f) for f in fixtures]


def get_team_form(team_id: int, last_n: int = 5) -> dict:
    """
    Get a team's recent form (last N matches).

    Returns dict with:
        form: str of W/D/L (e.g. "WWDLW")
        matches: list of simplified match dicts
        stats: {wins, draws, losses, goals_scored, goals_conceded}
    """
    # Free plan only supports 2022-2024 seasons — use 2024
    params = {"team": team_id, "season": 2024, "status": "FT"}
    body = _api_get("fixtures", params)
    # Sort by date descending and take last_n
    all_matches = body.get("response", [])
    all_matches.sort(key=lambda m: m["fixture"]["date"], reverse=True)
    body["response"] = all_matches[:last_n]
    matches = body.get("response", [])

    form_str = ""
    wins, draws, losses = 0, 0, 0
    goals_scored, goals_conceded = 0, 0
    simplified: list[dict] = []

    for m in matches:
        home = m["teams"]["home"]
        away = m["teams"]["away"]
        score = m["goals"]
        is_home = home["id"] == team_id

        team_goals = score["home"] if is_home else score["away"]
        opp_goals = score["away"] if is_home else score["home"]

        # Skip matches that haven't been played yet (goals are None)
        if team_goals is None or opp_goals is None:
            continue

        goals_scored += team_goals
        goals_conceded += opp_goals

        if team_goals > opp_goals:
            form_str += "W"
            wins += 1
        elif team_goals == opp_goals:
            form_str += "D"
            draws += 1
        else:
            form_str += "L"
            losses += 1

        simplified.append({
            "fixture_id": m["fixture"]["id"],
            "date": m["fixture"]["date"],
            "home": home["name"],
            "away": away["name"],
            "score": f"{score['home']}-{score['away']}",
            "result": form_str[-1],
            "venue": "home" if is_home else "away",
        })

    played = wins + draws + losses
    return {
        "team_id": team_id,
        "form": form_str,
        "matches": simplified,
        "stats": {
            "played": played,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_scored": goals_scored,
            "goals_conceded": goals_conceded,
            "avg_goals_scored": round(goals_scored / max(played, 1), 2),
            "avg_goals_conceded": round(goals_conceded / max(played, 1), 2),
        },
    }


def get_head_to_head(team1_id: int, team2_id: int, last_n: int = 5) -> dict:
    """
    Get head-to-head history between two teams.

    Returns dict with:
        matches: list of H2H match dicts
        stats: {total, team1_wins, team2_wins, draws, avg_total_goals}
    """
    # Free plan doesn't support 'last' param for h2h
    params = {"h2h": f"{team1_id}-{team2_id}"}
    body = _api_get("fixtures/headtohead", params)
    # Sort by date descending and take last_n
    all_h2h = body.get("response", [])
    all_h2h.sort(key=lambda m: m["fixture"]["date"], reverse=True)
    body["response"] = all_h2h[:last_n]
    matches = body.get("response", [])

    t1_wins, t2_wins, draws = 0, 0, 0
    total_goals = 0
    simplified: list[dict] = []

    for m in matches:
        home = m["teams"]["home"]
        away = m["teams"]["away"]
        score = m["goals"]

        home_goals = score.get("home") or 0
        away_goals = score.get("away") or 0
        total_goals += home_goals + away_goals

        # Figure out which team is which
        if home["id"] == team1_id:
            if home_goals > away_goals:
                t1_wins += 1
            elif home_goals < away_goals:
                t2_wins += 1
            else:
                draws += 1
        else:
            if away_goals > home_goals:
                t1_wins += 1
            elif away_goals < home_goals:
                t2_wins += 1
            else:
                draws += 1

        simplified.append({
            "fixture_id": m["fixture"]["id"],
            "date": m["fixture"]["date"],
            "home": home["name"],
            "away": away["name"],
            "score": f"{home_goals}-{away_goals}",
        })

    played = t1_wins + t2_wins + draws
    return {
        "team1_id": team1_id,
        "team2_id": team2_id,
        "matches": simplified,
        "stats": {
            "total": played,
            "team1_wins": t1_wins,
            "team2_wins": t2_wins,
            "draws": draws,
            "avg_total_goals": round(total_goals / max(played, 1), 2),
        },
    }


def get_team_stats(team_id: int, league_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch detailed team statistics for a given league and season.

    Returns a dict with home/away form, average goals, clean sheets, etc.
    """
    if season is None:
        season = _current_season()

    params = {"team": team_id, "league": league_id, "season": season}
    body = _api_get("teams/statistics", params)
    stats = body.get("response", {})

    if not stats:
        return {"team_id": team_id, "league_id": league_id, "season": season}

    fixtures = stats.get("fixtures", {})
    goals_for = stats.get("goals", {}).get("for", {})
    goals_against = stats.get("goals", {}).get("against", {})
    clean_sheets = stats.get("clean_sheet", {})

    return {
        "team_id": team_id,
        "league_id": league_id,
        "season": season,
        "form": stats.get("form", ""),
        "fixtures": {
            "played": {
                "home": _safe_int(fixtures.get("played", {}).get("home")),
                "away": _safe_int(fixtures.get("played", {}).get("away")),
                "total": _safe_int(fixtures.get("played", {}).get("total")),
            },
            "wins": {
                "home": _safe_int(fixtures.get("wins", {}).get("home")),
                "away": _safe_int(fixtures.get("wins", {}).get("away")),
                "total": _safe_int(fixtures.get("wins", {}).get("total")),
            },
            "draws": {
                "home": _safe_int(fixtures.get("draws", {}).get("home")),
                "away": _safe_int(fixtures.get("draws", {}).get("away")),
                "total": _safe_int(fixtures.get("draws", {}).get("total")),
            },
            "losses": {
                "home": _safe_int(fixtures.get("losses", {}).get("home")),
                "away": _safe_int(fixtures.get("losses", {}).get("away")),
                "total": _safe_int(fixtures.get("losses", {}).get("total")),
            },
        },
        "goals": {
            "for": {
                "home": _safe_float(goals_for.get("average", {}).get("home")),
                "away": _safe_float(goals_for.get("average", {}).get("away")),
                "total": _safe_float(goals_for.get("average", {}).get("total")),
            },
            "against": {
                "home": _safe_float(goals_against.get("average", {}).get("home")),
                "away": _safe_float(goals_against.get("average", {}).get("away")),
                "total": _safe_float(goals_against.get("average", {}).get("total")),
            },
        },
        "clean_sheets": {
            "home": _safe_int(clean_sheets.get("home")),
            "away": _safe_int(clean_sheets.get("away")),
            "total": _safe_int(clean_sheets.get("total")),
        },
    }


def get_standings(league_id: int, season: Optional[int] = None) -> list[dict]:
    """
    Fetch league standings / table.

    Returns a flat list of team standings sorted by rank.
    """
    if season is None:
        season = _current_season()

    params = {"league": league_id, "season": season}
    body = _api_get("standings", params)
    response = body.get("response", [])

    if not response:
        return []

    # Standings can have multiple groups (e.g. Champions League groups)
    standings: list[dict] = []
    for league_obj in response:
        for group in league_obj.get("league", {}).get("standings", []):
            for entry in group:
                team = entry.get("team", {})
                standings.append({
                    "rank": entry.get("rank"),
                    "team_id": team.get("id"),
                    "team_name": team.get("name"),
                    "points": entry.get("points"),
                    "played": entry.get("all", {}).get("played"),
                    "won": entry.get("all", {}).get("win"),
                    "drawn": entry.get("all", {}).get("draw"),
                    "lost": entry.get("all", {}).get("lose"),
                    "goals_for": entry.get("all", {}).get("goals", {}).get("for"),
                    "goals_against": entry.get("all", {}).get("goals", {}).get("against"),
                    "goal_diff": entry.get("goalsDiff"),
                    "form": entry.get("form"),
                    "group": entry.get("group"),
                })

    return sorted(standings, key=lambda x: (x.get("group", ""), x.get("rank", 99)))


def get_fixtures_lookahead(
    league_id: int,
    date_from: str = None,
    date_to: str = None,
) -> list[dict]:
    """
    Fetch fixtures up to 2 weeks ahead using football-data.org (free tier).
    Falls back to API-Football if league not supported.

    Returns same format as get_fixtures() for compatibility.
    """
    fd_code = LEAGUE_TO_FD_CODE.get(league_id)
    if not fd_code or not FOOTBALL_DATA_KEY:
        return get_fixtures(date=date_from, league_id=league_id)

    if date_from is None:
        date_from = datetime.now().strftime("%Y-%m-%d")
    if date_to is None:
        # Default: 14 days ahead
        from datetime import timedelta
        dt = datetime.strptime(date_from, "%Y-%m-%d")
        date_to = (dt + timedelta(days=14)).strftime("%Y-%m-%d")

    # Cache this request
    cache_params = {"comp": fd_code, "from": date_from, "to": date_to}
    key = _cache_key("fd_matches", cache_params)
    cached = _read_cache(key, ttl_override=6 * 3600)  # 6h cache
    if cached is not None:
        return cached

    url = f"{FOOTBALL_DATA_BASE}/competitions/{fd_code}/matches"
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"football-data.org request failed: {exc}") from exc

    body = resp.json()
    matches = body.get("matches", [])

    # Normalize to our standard fixture format
    fixtures = []
    for m in matches:
        if m.get("status") in ("FINISHED", "IN_PLAY", "PAUSED"):
            continue  # Skip already played/live matches
        fixtures.append({
            "id": m.get("id"),
            "date": m.get("utcDate"),
            "timestamp": None,
            "status": m.get("status", "NS"),
            "league": {
                "id": league_id,
                "name": body.get("competition", {}).get("name", ""),
                "country": body.get("competition", {}).get("area", {}).get("name", ""),
                "season": None,
            },
            "home": {
                "id": m.get("homeTeam", {}).get("id"),
                "name": m.get("homeTeam", {}).get("name", "Unknown"),
            },
            "away": {
                "id": m.get("awayTeam", {}).get("id"),
                "name": m.get("awayTeam", {}).get("name", "Unknown"),
            },
            "goals": {
                "home": None,
                "away": None,
            },
            "_source": "football-data.org",
        })

    _write_cache(key, fixtures)
    return fixtures


def refresh_league(league_id: int, date_str: str = None, callback=None) -> dict:
    """
    Pre-cache all fixtures and team form for a league on a given date.
    Returns summary of what was cached and API calls used.

    Args:
        callback: optional function(msg: str) called with progress updates
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    def _log(msg):
        if callback:
            callback(msg)

    start_budget = get_api_budget()["used"]
    fixtures = get_fixtures(date=date_str, league_id=league_id)
    _log(f"  Found {len(fixtures)} fixtures")

    # Collect unique team IDs
    team_ids = set()
    for f in fixtures:
        team_ids.add(f["home"]["id"])
        team_ids.add(f["away"]["id"])

    # Pre-cache form for each team
    cached_teams = 0
    for tid in team_ids:
        budget = get_api_budget()
        if budget["remaining"] <= 5:  # Keep 5 calls as safety buffer
            _log(f"  Budget low ({budget['remaining']} left), stopping early")
            break
        try:
            get_team_form(tid, last_n=5)
            cached_teams += 1
        except Exception as e:
            _log(f"  Warning: form fetch failed for team {tid}: {e}")

    end_budget = get_api_budget()["used"]
    return {
        "league_id": league_id,
        "date": date_str,
        "fixtures": len(fixtures),
        "teams_cached": cached_teams,
        "api_calls_used": end_budget - start_budget,
    }


def refresh_all(league_ids: list[int], date_str: str = None, callback=None) -> dict:
    """
    Refresh data for multiple leagues. Budget-aware — stops if exhausted.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    results = []
    total_fixtures = 0
    total_calls = 0

    for lid in league_ids:
        budget = get_api_budget()
        if budget["remaining"] <= 5:
            if callback:
                callback(f"Budget exhausted ({budget['remaining']} left). Stopping.")
            break
        if callback:
            callback(f"Refreshing league {lid}...")
        result = refresh_league(lid, date_str, callback=callback)
        results.append(result)
        total_fixtures += result["fixtures"]
        total_calls += result["api_calls_used"]

    budget = get_api_budget()
    return {
        "date": date_str,
        "leagues_refreshed": len(results),
        "total_fixtures": total_fixtures,
        "total_api_calls": total_calls,
        "budget_remaining": budget["remaining"],
        "details": results,
    }


def get_odds(fixture_id: int) -> dict:
    """
    Fetch pre-match odds for a fixture.

    Returns dict mapping market labels to outcomes with odds.
    """
    params = {"fixture": fixture_id}
    body = _api_get("odds", params)
    response = body.get("response", [])

    if not response:
        return {}

    # Take the first bookmaker's odds
    markets: dict[str, list[dict]] = {}
    for odds_set in response:
        for bookmaker in odds_set.get("bookmakers", []):
            for bet in bookmaker.get("bets", []):
                label = bet.get("name", "Unknown")
                if label not in markets:
                    markets[label] = [
                        {"value": v.get("value"), "odd": _safe_float(v.get("odd"))}
                        for v in bet.get("values", [])
                    ]
            break  # first bookmaker only
        break  # first odds set only

    return markets


# ── Internal helpers ─────────────────────────────────────────────────────────

def _normalize_fixture(raw: dict) -> dict:
    """Flatten API-Football fixture into a cleaner dict."""
    fixture = raw.get("fixture", {})
    league = raw.get("league", {})
    teams = raw.get("teams", {})
    goals = raw.get("goals", {})

    return {
        "id": fixture.get("id"),
        "date": fixture.get("date"),
        "timestamp": fixture.get("timestamp"),
        "status": fixture.get("status", {}).get("short"),
        "league": {
            "id": league.get("id"),
            "name": league.get("name"),
            "country": league.get("country"),
            "season": league.get("season"),
        },
        "home": {
            "id": teams.get("home", {}).get("id"),
            "name": teams.get("home", {}).get("name"),
        },
        "away": {
            "id": teams.get("away", {}).get("id"),
            "name": teams.get("away", {}).get("name"),
        },
        "goals": {
            "home": goals.get("home"),
            "away": goals.get("away"),
        },
    }


def _current_season() -> int:
    """Return the current football season year (starts ~August).
    Capped at 2024 for API-Football free plan compatibility."""
    now = datetime.now()
    season = now.year if now.month >= 7 else now.year - 1
    return min(season, 2024)  # Free plan limit


def _safe_int(val: Any) -> int:
    """Convert to int, defaulting to 0."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _safe_float(val: Any) -> float:
    """Convert to float, defaulting to 0.0."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
