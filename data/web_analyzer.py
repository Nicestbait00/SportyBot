"""
Web-based match analyzer for SportyBot /check flow.

Primary data source: football-data.org (free, 10 req/min, no daily cap)
  - 10 recent matches per team (current season)
  - 12 competitions: EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL, etc.

Fallback: thesportsdb.com (free, unlimited, but only ~1 recent result)
Last resort: odds-based estimation (no external data needed)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "web"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_KEY", "")
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# football-data.org team name mapping (built on first use, cached to disk)
_FD_TEAM_MAP_FILE = CACHE_DIR / "fd_team_map.json"
_fd_team_map: dict[str, int] = {}  # lowercase name/shortName → team ID
_fd_team_map_loaded = False

# Rate limiter for football-data.org (10 req/min)
_fd_last_call_time = 0.0
_FD_MIN_INTERVAL = 4.0  # seconds between calls (safe for 10/min limit, was 6.5)
_fd_rate_limit_lock = threading.Lock()
_fd_cooldown_until = 0.0
_FD_COOLDOWN_SECONDS = 90


# ── Cache helpers ────────────────────────────────────────────────────────────

def _cache_get(key: str, ttl: int = 86400) -> Optional[Any]:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data.get("_ts", 0) < ttl:
            return data.get("payload")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _cache_set(key: str, payload: Any) -> None:
    path = CACHE_DIR / f"{key}.json"
    path.write_text(json.dumps({"_ts": time.time(), "payload": payload}))


def _cache_key(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}|{value}".encode()).hexdigest()[:16]


# ── football-data.org API ────────────────────────────────────────────────────

def _fd_rate_limit():
    """Enforce rate limiting for football-data.org."""
    global _fd_last_call_time
    with _fd_rate_limit_lock:
        elapsed = time.time() - _fd_last_call_time
        if elapsed < _FD_MIN_INTERVAL:
            wait = _FD_MIN_INTERVAL - elapsed
            time.sleep(wait)
        _fd_last_call_time = time.time()


def _fd_is_cooling_down() -> bool:
    """Return True if football-data.org is in temporary cooldown after a 429."""
    with _fd_rate_limit_lock:
        return time.time() < _fd_cooldown_until


def _fd_start_cooldown() -> None:
    """Pause football-data.org usage briefly after rate limiting."""
    global _fd_cooldown_until
    with _fd_rate_limit_lock:
        _fd_cooldown_until = max(_fd_cooldown_until, time.time() + _FD_COOLDOWN_SECONDS)


def _fd_get(endpoint: str, params: dict = None) -> dict:
    """Make a rate-limited GET to football-data.org."""
    if not FOOTBALL_DATA_KEY:
        raise RuntimeError("FOOTBALL_DATA_KEY not set")
    if _fd_is_cooling_down():
        raise RuntimeError("football-data cooldown active after rate limit")
    _fd_rate_limit()
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
    try:
        resp = requests.get(
            f"{FOOTBALL_DATA_BASE}/{endpoint}",
            headers=headers,
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            _fd_start_cooldown()
            raise RuntimeError("football-data rate limit hit; cooling down") from exc
        raise
    return resp.json()


def _load_fd_team_map():
    """Build a name→ID map from all free-tier competitions' teams."""
    global _fd_team_map, _fd_team_map_loaded

    if _fd_team_map_loaded:
        return

    # Try disk cache first (24h TTL)
    if _FD_TEAM_MAP_FILE.exists():
        try:
            data = json.loads(_FD_TEAM_MAP_FILE.read_text())
            if time.time() - data.get("_ts", 0) < 86400:
                _fd_team_map = data.get("map", {})
                _fd_team_map_loaded = True
                logger.info(f"Loaded {len(_fd_team_map)} teams from cache")
                return
        except (json.JSONDecodeError, KeyError):
            pass

    # Build from API
    # Club + international competitions for broader coverage
    competitions = [
        # Club
        "PL", "PD", "SA", "BL1", "FL1", "CL", "DED", "PPL", "ELC", "BSA",
        # International
        "EC", "WC", "CLI",
    ]
    team_map = {}

    for comp in competitions:
        try:
            data = _fd_get(f"competitions/{comp}/teams")
            for team in data.get("teams", []):
                tid = team["id"]
                # Map multiple name variants to the same ID
                name = team.get("name", "")
                short = team.get("shortName", "")
                tla = team.get("tla", "")

                if name:
                    team_map[name.lower()] = tid
                    # Also store without "FC", "CF" etc.
                    clean = _strip_suffix(name)
                    if clean.lower() != name.lower():
                        team_map[clean.lower()] = tid
                if short:
                    team_map[short.lower()] = tid
                if tla:
                    team_map[tla.lower()] = tid
        except Exception as e:
            logger.warning(f"Failed to load teams for {comp}: {e}")

    # Save to disk
    _FD_TEAM_MAP_FILE.write_text(json.dumps({"_ts": time.time(), "map": team_map}))
    _fd_team_map = team_map
    _fd_team_map_loaded = True
    logger.info(f"Built team map: {len(team_map)} entries from {len(competitions)} competitions")


def fd_find_team_id(name: str) -> Optional[int]:
    """Find a football-data.org team ID by name (fuzzy matching)."""
    _load_fd_team_map()

    name_lower = name.lower().strip()

    # Exact match
    if name_lower in _fd_team_map:
        return _fd_team_map[name_lower]

    # Without suffixes (FC, CF, SC, etc.)
    clean = _strip_suffix(name).lower()
    if clean in _fd_team_map:
        return _fd_team_map[clean]

    # Substring match (e.g. "Arsenal" matches "Arsenal FC")
    for key, tid in _fd_team_map.items():
        if name_lower in key or key in name_lower:
            return tid

    # Last resort: fuzzy word overlap
    name_words = set(name_lower.split())
    best_match = None
    best_score = 0
    for key, tid in _fd_team_map.items():
        key_words = set(key.split())
        overlap = len(name_words & key_words)
        if overlap > best_score and overlap >= 1:
            best_score = overlap
            best_match = tid

    return best_match


def fd_get_team_matches(team_id: int, limit: int = 10) -> list[dict]:
    """Get a team's recent finished matches from football-data.org."""
    key = _cache_key("fd_matches", f"{team_id}_{limit}")
    cached = _cache_get(key, ttl=43200)  # 12h cache
    if cached is not None:
        return cached

    try:
        data = _fd_get(f"teams/{team_id}/matches", {"status": "FINISHED", "limit": limit})
        matches = data.get("matches", [])

        results = []
        for m in matches:
            ft = m.get("score", {}).get("fullTime", {})
            hs = ft.get("home")
            as_ = ft.get("away")
            if hs is None or as_ is None:
                continue

            home_name = m.get("homeTeam", {}).get("name", "?")
            away_name = m.get("awayTeam", {}).get("name", "?")
            home_id = m.get("homeTeam", {}).get("id")
            away_id = m.get("awayTeam", {}).get("id")
            is_home = home_id == team_id

            goals_for = hs if is_home else as_
            goals_against = as_ if is_home else hs

            if goals_for > goals_against:
                result = "W"
            elif goals_for == goals_against:
                result = "D"
            else:
                result = "L"

            results.append({
                "date": m.get("utcDate", "")[:10],
                "home": home_name,
                "away": away_name,
                "home_score": hs,
                "away_score": as_,
                "result": result,
                "goals_for": goals_for,
                "goals_against": goals_against,
                "competition": m.get("competition", {}).get("name", ""),
                "venue": "home" if is_home else "away",
            })

        _cache_set(key, results)
        return results

    except Exception as e:
        logger.warning(f"fd_get_team_matches failed for {team_id}: {e}")
        return []


# ── TheSportsDB fallback ────────────────────────────────────────────────────

def sportsdb_search_team(name: str) -> Optional[str]:
    """Search thesportsdb.com for a team ID (fallback)."""
    key = _cache_key("sdb_team", name.lower())
    cached = _cache_get(key, ttl=86400 * 30)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{SPORTSDB_BASE}/searchteams.php",
            params={"t": name},
            timeout=10,
        )
        teams = resp.json().get("teams") or []

        # Prefer soccer teams
        result_id = None
        for t in teams:
            if (t.get("strSport") or "").lower() == "soccer":
                result_id = t.get("idTeam")
                break
        if not result_id and teams:
            result_id = teams[0].get("idTeam")

        _cache_set(key, result_id)
        return result_id
    except Exception:
        return None


def sportsdb_get_last_results(team_id: str) -> list[dict]:
    """Get last results from thesportsdb (fallback, usually only 1 result)."""
    key = _cache_key("sdb_results", str(team_id))
    cached = _cache_get(key, ttl=43200)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{SPORTSDB_BASE}/eventslast.php",
            params={"id": team_id},
            timeout=10,
        )
        events = resp.json().get("results") or []

        results = []
        for e in events[:5]:
            hs = _safe_int(e.get("intHomeScore"))
            as_ = _safe_int(e.get("intAwayScore"))
            is_home = str(e.get("idHomeTeam")) == str(team_id)

            if is_home:
                result = "W" if hs > as_ else ("D" if hs == as_ else "L")
                gf, ga = hs, as_
            else:
                result = "W" if as_ > hs else ("D" if hs == as_ else "L")
                gf, ga = as_, hs

            results.append({
                "date": e.get("dateEvent", ""),
                "home": e.get("strHomeTeam", "?"),
                "away": e.get("strAwayTeam", "?"),
                "home_score": hs,
                "away_score": as_,
                "result": result,
                "goals_for": gf,
                "goals_against": ga,
                "competition": e.get("strLeague", ""),
                "venue": "home" if is_home else "away",
            })

        _cache_set(key, results)
        return results
    except Exception:
        return []


# ── Unified team data fetcher ────────────────────────────────────────────────

def get_team_results(team_name: str, count: int = 10) -> list[dict]:
    """
    Get recent results for a team. Tries sources in order:
    1. football-data.org (up to 10 results, current season, 12 competitions)
    2. API-Football (up to 5 results from season 2024, if budget allows)
    3. thesportsdb.com (1-2 results, unlimited)
    4. Empty list (no data)

    Results are MERGED and deduplicated if multiple sources return data,
    giving the most complete picture possible.
    """
  # Normalise name before building the cache key
def _normalise(n: str) -> str:
    return _strip_suffix(n).lower().strip()

    # Top-level cache: avoid re-running source lookups for the same team (12h TTL)
    top_key = _cache_key("team_results", f"{team_name.lower()}_{count}")
    cached = _cache_get(top_key, ttl=43200)
    if cached is not None:
        return cached

    all_results = []
    seen_dates = set()  # deduplicate by date+teams

    def _add_results(new_results):
        for r in new_results:
            key = f"{r['date']}_{r['home']}_{r['away']}"
            if key not in seen_dates:
                seen_dates.add(key)
                all_results.append(r)

    # Source 1: football-data.org (primary — best coverage, free, current season)
    if FOOTBALL_DATA_KEY and not _fd_is_cooling_down():
        fd_id = fd_find_team_id(team_name)
        if fd_id:
            results = fd_get_team_matches(fd_id, limit=count)
            _add_results(results)

    # Source 2: API-Football cached data (season 2024)
    # Always try — _api_get() returns cached data without using budget.
    # Only a cache MISS would trigger a live API call (which checks budget internally).
    try:
        from data_collector import get_team_form
        from analyzer import _search_team_id
        af_id = _search_team_id(team_name)
        if af_id:
            af_form = get_team_form(af_id, last_n=5)
            for m in af_form.get("matches", []):
                _add_results([{
                    "date": m["date"][:10],
                    "home": m["home"],
                    "away": m["away"],
                    "home_score": int(m["score"].split("-")[0]),
                    "away_score": int(m["score"].split("-")[1]),
                    "result": m["result"],
                    "goals_for": int(m["score"].split("-")[0]) if m["venue"] == "home" else int(m["score"].split("-")[1]),
                    "goals_against": int(m["score"].split("-")[1]) if m["venue"] == "home" else int(m["score"].split("-")[0]),
                    "competition": "",
                    "venue": m["venue"],
                }])
    except Exception:
        pass  # API-Football unavailable or cache miss with no budget — no problem

    # Source 3: thesportsdb.com (always free, always available)
    # Try whenever we have fewer results than requested — not just when empty
    if len(all_results) < count:
        try:
            sdb_id = sportsdb_search_team(team_name)
            if sdb_id:
                _add_results(sportsdb_get_last_results(sdb_id))
        except Exception:
            pass  # thesportsdb unavailable — no problem

    # Sort by date (most recent first) and limit
    all_results.sort(key=lambda r: r["date"], reverse=True)
    results = all_results[:count]

    # Cache merged results for 12 hours
    if results:
        _cache_set(top_key, results)

    return results


# ── Analysis engine ──────────────────────────────────────────────────────────

def analyze_pick(pick: dict) -> dict:
    """
    Analyze a single pick from a SportyBet booking code.

    Uses football-data.org (primary) + thesportsdb.com (fallback) for form data.
    Returns enriched pick with verdict, confidence, reasons, suggestions, and data quality.
    """
    home_name = pick.get("home", "?")
    away_name = pick.get("away", "?")
    market = pick.get("market", "?")
    pick_desc = pick.get("pick", "?")
    odds = pick.get("odds", 1.0)
    match_status = pick.get("match_status", "?")

    result = {
        **pick,
        "data_confidence": None,
        "verdict": "unknown",
        "analysis_reasons": [],
        "suggestion": None,
        "data_quality": "unknown",
    }

    # Skip already-ended matches
    if match_status == "Ended":
        result["verdict"] = "ended"
        result["analysis_reasons"] = ["Match already played"]
        result["data_confidence"] = pick.get("confidence", 50)
        return result

    # Fetch form data for both teams
    home_form = None
    away_form = None
    home_results_count = 0
    away_results_count = 0
    reasons = []

    home_results = get_team_results(home_name, count=10)
    home_results_count = len(home_results)
    if home_results:
        home_form = _summarize_form(home_results)
        form_str = home_form["form_string"]
        reasons.append(
            f"{home_name}: {form_str} "
            f"({home_form['wins']}W {home_form['draws']}D {home_form['losses']}L, "
            f"{home_form['avg_scored']:.1f} GS, {home_form['avg_conceded']:.1f} GA/game)"
        )
        # Show venue-specific stats if enough data
        if home_form["home_played"] >= 3:
            reasons.append(
                f"  ↳ At home: {home_form['home_wins']}W {home_form['home_draws']}D {home_form['home_losses']}L, "
                f"{home_form['home_avg_scored']:.1f} GS/game"
            )
    else:
        reasons.append(f"{home_name}: team data not found")

    away_results = get_team_results(away_name, count=10)
    away_results_count = len(away_results)
    if away_results:
        away_form = _summarize_form(away_results)
        form_str = away_form["form_string"]
        reasons.append(
            f"{away_name}: {form_str} "
            f"({away_form['wins']}W {away_form['draws']}D {away_form['losses']}L, "
            f"{away_form['avg_scored']:.1f} GS, {away_form['avg_conceded']:.1f} GA/game)"
        )
        if away_form["away_played"] >= 3:
            reasons.append(
                f"  ↳ Away: {away_form['away_wins']}W {away_form['away_draws']}D {away_form['away_losses']}L, "
                f"{away_form['away_avg_scored']:.1f} GS/game"
            )
    else:
        reasons.append(f"{away_name}: team data not found")

    # Data quality assessment
    total_results = home_results_count + away_results_count
    if total_results == 0:
        data_quality = "none"
        reasons.insert(0, "⚠️ NO DATA — using odds-based estimate only")
    elif total_results <= 3:
        data_quality = "limited"
        reasons.insert(0, f"⚠️ LIMITED DATA — only {total_results} match(es) found, treat with caution")
    elif total_results <= 8:
        data_quality = "fair"
        reasons.insert(0, f"📊 Fair data — {total_results} recent matches")
    else:
        data_quality = "good"
        reasons.insert(0, f"📊 Good data — {total_results} recent matches analyzed")

    # Score the pick
    confidence = _score_pick(
        market, pick_desc, odds,
        home_name, away_name,
        home_form, away_form,
        reasons,
    )

    # When data is poor, DO NOT silently fall back to odds-based estimates.
    # Instead, mark clearly so the user can decide.
    if data_quality == "none":
        confidence = 0  # No data = no confidence. User must decide.
        reasons.append("❌ Cannot analyze — no historical data found for these teams. You decide on this one.")
    elif data_quality == "limited":
        # Slight penalty, but keep data-driven score
        confidence = int(confidence * 0.8)

    # Determine verdict
    if data_quality == "none":
        verdict = "no_data"  # User must decide — we won't guess
    elif confidence >= 75:
        verdict = "strong"
    elif confidence >= 60:
        verdict = "moderate"
    elif confidence >= 45:
        verdict = "weak"
    else:
        verdict = "avoid"

    # Suggest alternatives for weak picks
    suggestion = None
    if verdict in ("weak", "avoid") and home_form and away_form:
        suggestion = _suggest_alternative(
            home_name, away_name, home_form, away_form, market, pick_desc
        )

    result["data_confidence"] = confidence
    result["verdict"] = verdict
    result["analysis_reasons"] = reasons
    result["suggestion"] = suggestion
    result["data_quality"] = data_quality

    return result


# ── Scoring logic ────────────────────────────────────────────────────────────

def _score_pick(
    market: str, pick_desc: str, odds: float,
    home_name: str, away_name: str,
    home_form: Optional[dict], away_form: Optional[dict],
    reasons: list[str],
) -> int:
    """Score a pick 0-100 based on form data and market type."""
    if not home_form and not away_form:
        return _odds_to_confidence(odds)

    market_lower = market.lower()
    pick_lower = pick_desc.lower()

    if "1x2" in market_lower or "match result" in market_lower or "winner" in market_lower:
        return _score_match_result(
            pick_desc, home_name, away_name, home_form, away_form, reasons
        )
    elif "over" in pick_lower or "under" in pick_lower:
        return _score_over_under(pick_desc, home_form, away_form, reasons)
    elif "gg" in market_lower or "btts" in market_lower or "both" in market_lower:
        return _score_btts(pick_desc, home_form, away_form, reasons)
    elif "double chance" in market_lower:
        return _score_double_chance(
            pick_desc, home_name, away_name, home_form, away_form, reasons
        )
    else:
        base = _odds_to_confidence(odds)
        if home_form and away_form:
            home_wr = home_form["wins"] / max(home_form["played"], 1)
            away_wr = away_form["wins"] / max(away_form["played"], 1)
            form_bonus = (home_wr + away_wr) * 10 - 10
            base = int(base + form_bonus)
        return max(0, min(100, base))


def _score_match_result(
    pick_desc: str, home_name: str, away_name: str,
    home_form: Optional[dict], away_form: Optional[dict],
    reasons: list[str],
) -> int:
    """Score a 1X2 / match result pick."""
    points = []

    if "home" in pick_desc.lower() or "1" == pick_desc.strip():
        # Home win
        if home_form:
            win_rate = home_form["wins"] / max(home_form["played"], 1)
            points.append(win_rate * 100)

            # Use home-specific stats if available
            if home_form["home_played"] >= 3:
                home_wr = home_form["home_wins"] / max(home_form["home_played"], 1)
                points.append(home_wr * 100)
                if home_wr >= 0.7:
                    reasons.append(f"🏟️ {home_name} wins {home_wr:.0%} at home")

            streak = _count_streak(home_form["form_string"], "W")
            if streak >= 3:
                points.append(85)
                reasons.append(f"🔥 {home_name} on {streak}-game win streak")
            elif home_form["form_string"] and home_form["form_string"][-1] == "L":
                points.append(35)
                reasons.append(f"⚠️ {home_name} lost their last game")

        if away_form:
            loss_rate = away_form["losses"] / max(away_form["played"], 1)
            points.append(loss_rate * 80)

            # Away-specific weakness
            if away_form["away_played"] >= 3:
                away_lr = away_form["away_losses"] / max(away_form["away_played"], 1)
                if away_lr >= 0.5:
                    reasons.append(f"📉 {away_name} loses {away_lr:.0%} of away games")

            losing_streak = _count_streak(away_form["form_string"], "L")
            if losing_streak >= 3:
                points.append(80)
                reasons.append(f"📉 {away_name} on {losing_streak}-game losing streak")

        points.append(60)  # Home advantage baseline

    elif "away" in pick_desc.lower() or "2" == pick_desc.strip():
        # Away win
        if away_form:
            win_rate = away_form["wins"] / max(away_form["played"], 1)
            points.append(win_rate * 100)

            if away_form["away_played"] >= 3:
                away_wr = away_form["away_wins"] / max(away_form["away_played"], 1)
                points.append(away_wr * 100)

            streak = _count_streak(away_form["form_string"], "W")
            if streak >= 3:
                points.append(82)
                reasons.append(f"🔥 {away_name} on {streak}-game win streak")

        if home_form:
            loss_rate = home_form["losses"] / max(home_form["played"], 1)
            points.append(loss_rate * 70)

        points.append(40)  # Away disadvantage

    elif "draw" in pick_desc.lower() or "x" == pick_desc.strip().lower():
        if home_form:
            draw_rate = home_form["draws"] / max(home_form["played"], 1)
            points.append(draw_rate * 100)
        if away_form:
            draw_rate = away_form["draws"] / max(away_form["played"], 1)
            points.append(draw_rate * 100)

    return int(sum(points) / max(len(points), 1)) if points else 50


def _score_over_under(
    pick_desc: str,
    home_form: Optional[dict], away_form: Optional[dict],
    reasons: list[str],
) -> int:
    """Score an Over/Under pick."""
    threshold = 1.5
    for val in ["0.5", "1.5", "2.5", "3.5", "4.5"]:
        if val in pick_desc:
            threshold = float(val)
            break

    is_over = "over" in pick_desc.lower()

    expected = 2.5
    if home_form and away_form:
        expected = (
            (home_form["avg_scored"] + away_form["avg_conceded"]) / 2 +
            (away_form["avg_scored"] + home_form["avg_conceded"]) / 2
        )
        reasons.append(f"Expected goals: {expected:.1f} (threshold: {threshold})")

    prob = _poisson_over_prob(expected, threshold)
    if not is_over:
        prob = 1.0 - prob

    return max(0, min(100, int(prob * 100)))


def _score_btts(
    pick_desc: str,
    home_form: Optional[dict], away_form: Optional[dict],
    reasons: list[str],
) -> int:
    """Score a Both Teams to Score pick."""
    is_yes = "yes" in pick_desc.lower() or "gg" in pick_desc.lower()
    points = []

    if home_form:
        scores = home_form["avg_scored"] > 0.8
        concedes = home_form["avg_conceded"] > 0.8
        if is_yes:
            points.append(85 if scores and concedes else 40)
        else:
            points.append(85 if not concedes else 40)

    if away_form:
        scores = away_form["avg_scored"] > 0.8
        concedes = away_form["avg_conceded"] > 0.8
        if is_yes:
            points.append(85 if scores and concedes else 40)
        else:
            points.append(85 if not scores else 40)

    return int(sum(points) / max(len(points), 1)) if points else 50


def _score_double_chance(
    pick_desc: str, home_name: str, away_name: str,
    home_form: Optional[dict], away_form: Optional[dict],
    reasons: list[str],
) -> int:
    """Score a Double Chance pick (1X, X2, 12)."""
    points = []

    if "1x" in pick_desc.lower():
        if home_form:
            no_loss = (home_form["wins"] + home_form["draws"]) / max(home_form["played"], 1)
            points.append(no_loss * 100)
        points.append(65)
    elif "x2" in pick_desc.lower():
        if away_form:
            no_loss = (away_form["wins"] + away_form["draws"]) / max(away_form["played"], 1)
            points.append(no_loss * 100)
        points.append(50)
    elif "12" in pick_desc.lower():
        if home_form and away_form:
            home_wr = home_form["wins"] / max(home_form["played"], 1)
            away_wr = away_form["wins"] / max(away_form["played"], 1)
            combined = 1 - (1 - home_wr) * (1 - away_wr)
            points.append(combined * 100)

    return int(sum(points) / max(len(points), 1)) if points else 60


# ── Suggestion engine ────────────────────────────────────────────────────────

def _suggest_alternative(
    home_name: str, away_name: str,
    home_form: dict, away_form: dict,
    current_market: str, current_pick: str,
) -> Optional[dict]:
    """Suggest a safer market if the current pick looks bad."""
    expected_goals = (
        (home_form["avg_scored"] + away_form["avg_conceded"]) / 2 +
        (away_form["avg_scored"] + home_form["avg_conceded"]) / 2
    )

    suggestions = []

    if expected_goals > 2.0:
        prob = _poisson_over_prob(expected_goals, 1.5)
        if prob > 0.70:
            suggestions.append(("Over 1.5 Goals", int(prob * 100)))

    home_wr = home_form["wins"] / max(home_form["played"], 1)
    if home_wr >= 0.6:
        suggestions.append((f"{home_name} Win", int(home_wr * 85)))

    if (home_form["avg_scored"] > 0.8 and home_form["avg_conceded"] > 0.8 and
            away_form["avg_scored"] > 0.8 and away_form["avg_conceded"] > 0.8):
        suggestions.append(("Both Teams to Score - Yes", 72))

    home_no_loss = (home_form["wins"] + home_form["draws"]) / max(home_form["played"], 1)
    if home_no_loss >= 0.7:
        suggestions.append((f"{home_name} or Draw (1X)", int(home_no_loss * 90)))

    if expected_goals > 1.5:
        suggestions.append(("Over 0.5 Goals", 93))

    if not suggestions:
        return None

    suggestions.sort(key=lambda x: x[1], reverse=True)
    for name, conf in suggestions:
        if name.lower() not in current_pick.lower():
            return {"market": name, "confidence": conf}

    return None


# ── Form summary helpers ─────────────────────────────────────────────────────

def _summarize_form(results: list[dict]) -> dict:
    """Summarize match results into comprehensive form stats (overall + venue-split)."""
    wins = draws = losses = 0
    goals_scored = goals_conceded = 0
    form_string = ""

    # Venue-specific
    home_wins = home_draws = home_losses = home_played = 0
    home_gs = home_ga = 0
    away_wins = away_draws = away_losses = away_played = 0
    away_gs = away_ga = 0

    for r in results:
        form_string += r["result"]
        gf = r["goals_for"]
        ga = r["goals_against"]
        goals_scored += gf
        goals_conceded += ga

        if r["result"] == "W":
            wins += 1
        elif r["result"] == "D":
            draws += 1
        else:
            losses += 1

        venue = r.get("venue", "")
        if venue == "home":
            home_played += 1
            home_gs += gf
            home_ga += ga
            if r["result"] == "W":
                home_wins += 1
            elif r["result"] == "D":
                home_draws += 1
            else:
                home_losses += 1
        elif venue == "away":
            away_played += 1
            away_gs += gf
            away_ga += ga
            if r["result"] == "W":
                away_wins += 1
            elif r["result"] == "D":
                away_draws += 1
            else:
                away_losses += 1

    played = wins + draws + losses
    return {
        "played": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_scored": goals_scored,
        "goals_conceded": goals_conceded,
        "avg_scored": round(goals_scored / max(played, 1), 2),
        "avg_conceded": round(goals_conceded / max(played, 1), 2),
        "form_string": form_string,
        # Home stats
        "home_played": home_played,
        "home_wins": home_wins,
        "home_draws": home_draws,
        "home_losses": home_losses,
        "home_avg_scored": round(home_gs / max(home_played, 1), 2),
        "home_avg_conceded": round(home_ga / max(home_played, 1), 2),
        # Away stats
        "away_played": away_played,
        "away_wins": away_wins,
        "away_draws": away_draws,
        "away_losses": away_losses,
        "away_avg_scored": round(away_gs / max(away_played, 1), 2),
        "away_avg_conceded": round(away_ga / max(away_played, 1), 2),
    }


# ── Utility helpers ──────────────────────────────────────────────────────────

def _strip_suffix(name: str) -> str:
    """Remove common club suffixes (FC, CF, SC, etc.)."""
    suffixes = [" FC", " CF", " SC", " AFC", " SV", " BV"]
    for s in suffixes:
        if name.endswith(s):
            return name[:-len(s)].strip()
    prefixes = ["FC ", "CF ", "SC ", "AFC "]
    for p in prefixes:
        if name.startswith(p):
            return name[len(p):].strip()
    return name


def _count_streak(form: str, char: str) -> int:
    count = 0
    for c in reversed(form):
        if c == char:
            count += 1
        else:
            break
    return count


def _poisson_over_prob(expected: float, threshold: float) -> float:
    if expected <= 0:
        return 0.0
    k = int(math.floor(threshold))
    cumulative = 0.0
    for i in range(k + 1):
        cumulative += (expected ** i) * math.exp(-expected) / math.factorial(i)
    return 1.0 - cumulative


def _odds_to_confidence(odds: float) -> int:
    if odds <= 0:
        return 50
    implied = (1 / odds) * 100
    confidence = int(implied - 5)
    return max(10, min(95, confidence))


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
