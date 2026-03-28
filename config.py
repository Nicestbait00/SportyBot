"""
SportyBot Configuration
Loads API keys and settings from environment variables or .env file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

# ── API-Football (v3) ────────────────────────────────────────────────────────
API_FOOTBALL_KEY: str = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE: str = "https://v3.football.api-sports.io"

# ── football-data.org (fixtures lookahead) ───────────────────────────────────
FOOTBALL_DATA_KEY: str = os.getenv("FOOTBALL_DATA_KEY", "")
FOOTBALL_DATA_BASE: str = "https://api.football-data.org/v4"

# Mapping: our league IDs (API-Football) → football-data.org competition codes
LEAGUE_TO_FD_CODE: dict[int, str] = {
    39: "PL",      # Premier League
    40: "ELC",     # Championship
    140: "PD",     # La Liga
    135: "SA",     # Serie A
    94: "PPL",     # Primeira Liga
    78: "BL1",     # Bundesliga
    88: "DED",     # Eredivisie
    61: "FL1",     # Ligue 1
    2: "CL",       # Champions League
}

# ── SportyBet ────────────────────────────────────────────────────────────────
SPORTYBET_BASE_URL: str = os.getenv(
    "SPORTYBET_BASE_URL", "https://www.sportybet.com/api/ng"
)

# ── Cache settings ───────────────────────────────────────────────────────────
CACHE_DIR: Path = Path(__file__).resolve().parent / ".cache"
CACHE_TTL_SECONDS: int = 6 * 60 * 60  # 6 hours

# ── Common league IDs (API-Football v3) ──────────────────────────────────────
LEAGUES: dict[str, int] = {
    "premier_league": 39,
    "championship": 40,
    "la_liga": 140,
    "serie_a": 135,
    "primeira_liga": 94,
    "portugal_primera": 94,
    "bundesliga": 78,
    "eredivisie": 88,
    "ligue_1": 61,
    "saudi_pro_league": 307,
    "saudi_league": 307,
    "a_league": 188,
    "australian_top_league": 188,
    "champions_league": 2,
    "europa_league": 3,
    "nations_league": 5,
    "afcon": 6,
    "afcon_qualifiers": 36,
    "world_cup": 1,
    "world_cup_qualifiers_africa": 29,
    "world_cup_qualifiers_europe": 32,
    "world_cup_qualifiers_asia": 30,
    "world_cup_qualifiers_south_america": 31,
    "international_friendlies": 10,
    "epl": 39,  # alias
    "ucl": 2,   # alias
}

LEAGUE_DISPLAY_NAMES: dict[int, str] = {
    39: "Premier League",
    40: "Championship",
    140: "La Liga",
    135: "Serie A",
    94: "Primeira Liga",
    78: "Bundesliga",
    88: "Eredivisie",
    61: "Ligue 1",
    307: "Saudi Pro League",
    188: "A-League",
    2: "Champions League",
    3: "Europa League",
    5: "Nations League",
    6: "AFCON",
    36: "AFCON Qualifiers",
    1: "World Cup",
    29: "World Cup Qualifiers Africa",
    32: "World Cup Qualifiers Europe",
    30: "World Cup Qualifiers Asia",
    31: "World Cup Qualifiers South America",
    10: "International Friendlies",
}

LEAGUE_CATEGORIES: dict[str, dict] = {
    "A": {
        "label": "Category A",
        "description": "Top 5 leagues",
        "league_ids": [39, 140, 135, 78, 61],
    },
    "B": {
        "label": "Category B",
        "description": "Championship, Primeira Liga, Saudi Pro League, Eredivisie, A-League",
        "league_ids": [40, 94, 307, 88, 188],
    },
}

# ── Available markets (SportyBet market IDs) ─────────────────────────────────
# All markets the user can toggle on/off. Key = internal label, value = metadata.
AVAILABLE_MARKETS: dict[str, dict] = {
    # ── Core markets ──
    "1X2": {
        "sportybet_id": "1",
        "description": "Home / Draw / Away",
        "outcomes": {"1": "Home", "2": "Draw", "3": "Away"},
    },
    "Over/Under": {
        "sportybet_id": "18",
        "description": "Total goals Over/Under",
        "outcomes": {"12": "Over", "13": "Under"},
        "specifier": True,
    },
    "GG/NG": {
        "sportybet_id": "29",
        "description": "Both Teams To Score",
        "outcomes": {"74": "GG (Yes)", "76": "NG (No)"},
    },
    "Double Chance": {
        "sportybet_id": "10",
        "description": "1X / 12 / X2",
        "outcomes": {"9": "1X", "10": "12", "11": "X2"},
    },
    # ── Win markets ──
    "Draw No Bet": {
        "sportybet_id": "11",
        "description": "Home or Away (draw = void)",
        "outcomes": {"4": "Home", "5": "Away"},
    },
    # ── Goals markets ──
    "Home Over/Under": {
        "sportybet_id": "19",
        "description": "Home team goals Over/Under",
        "outcomes": {"12": "Over", "13": "Under"},
        "specifier": True,
    },
    "Away Over/Under": {
        "sportybet_id": "20",
        "description": "Away team goals Over/Under",
        "outcomes": {"12": "Over", "13": "Under"},
        "specifier": True,
    },
    "Odd/Even": {
        "sportybet_id": "26",
        "description": "Total goals Odd or Even",
        "outcomes": {"70": "Odd", "72": "Even"},
    },
    "Exact Goals": {
        "sportybet_id": "21",
        "description": "Exact number of goals (0-6+)",
        "outcomes": {},
        "specifier": True,
    },
    # ── Combo markets ──
    "1X2 & GG/NG": {
        "sportybet_id": "35",
        "description": "Result + Both Teams Score combo",
        "outcomes": {"78": "Home & GG", "80": "Home & NG", "82": "Draw & GG", "84": "Draw & NG", "86": "Away & GG", "88": "Away & NG"},
    },
    "1X2 & Over/Under": {
        "sportybet_id": "37",
        "description": "Result + Over/Under combo",
        "outcomes": {},
        "specifier": True,
    },
    "Over/Under & GG/NG": {
        "sportybet_id": "36",
        "description": "Over/Under + BTTS combo",
        "outcomes": {"90": "Over 2.5 & GG", "92": "Under 2.5 & GG", "94": "Over 2.5 & NG", "96": "Under 2.5 & NG"},
        "specifier": True,
    },
    # ── Half-time markets ──
    "HT 1X2": {
        "sportybet_id": "60",
        "description": "Half-Time result",
        "outcomes": {"1": "Home", "2": "Draw", "3": "Away"},
    },
    "HT/FT": {
        "sportybet_id": "47",
        "description": "Half-Time / Full-Time",
        "outcomes": {"418": "H/H", "420": "H/D", "422": "H/A", "424": "D/H", "426": "D/D", "428": "D/A", "430": "A/H", "432": "A/D", "434": "A/A"},
    },
    "HT Over/Under": {
        "sportybet_id": "68",
        "description": "1st Half Over/Under",
        "outcomes": {"12": "Over", "13": "Under"},
        "specifier": True,
    },
    "HT GG/NG": {
        "sportybet_id": "75",
        "description": "1st Half Both Teams Score",
        "outcomes": {"74": "Yes", "76": "No"},
    },
    # ── Clean sheet / scoring ──
    "Home Clean Sheet": {
        "sportybet_id": "31",
        "description": "Home keeps clean sheet",
        "outcomes": {"74": "Yes", "76": "No"},
    },
    "Away Clean Sheet": {
        "sportybet_id": "32",
        "description": "Away keeps clean sheet",
        "outcomes": {"74": "Yes", "76": "No"},
    },
    # ── Handicap ──
    "Handicap": {
        "sportybet_id": "14",
        "description": "European Handicap (0:1, 1:0, etc.)",
        "outcomes": {"1711": "Home", "1712": "Draw", "1713": "Away"},
        "specifier": True,
    },
    # ── Correct Score ──
    "Correct Score": {
        "sportybet_id": "45",
        "description": "Exact final score",
        "outcomes": {},
    },
}

# ── Market categories for cross-ticket hedging ────────────────────────────
MARKET_CATEGORIES = {
    "goals": ["Over/Under", "GG/NG", "Home Over/Under", "Away Over/Under",
              "Over/Under & GG/NG", "1X2 & Over/Under", "Exact Goals"],
    "result": ["1X2", "Double Chance", "Draw No Bet", "1X2 & GG/NG"],
    "halftime": ["HT 1X2", "HT/FT", "HT Over/Under", "HT GG/NG"],
    "other": ["Odd/Even", "Home Clean Sheet", "Away Clean Sheet",
              "Handicap", "Correct Score"],
}


def get_market_category(market_name: str) -> str:
    """Return the category for a market name, or 'other' if not found."""
    for cat, markets in MARKET_CATEGORIES.items():
        if market_name in markets:
            return cat
    return "other"


# Default enabled markets for new users
DEFAULT_ENABLED_MARKETS = [
    "1X2",
    "Over/Under",
    "GG/NG",
    "Home Over/Under",
    "Away Over/Under",
]

# Legacy strategy presets — kept for backward compatibility with saved configs
# New system uses direct config values instead
STRATEGY_PRESETS: dict[str, dict] = {
    "conservative": {
        "label": "Conservative",
        "min_confidence": 70,
        "min_odds": 1.15,
        "preferred_markets": ["1X2", "Over/Under", "GG/NG"],
        "over_threshold": 1.5,
        "description": "High confidence, Over 1.5+, min 1.15 odds per pick",
    },
    "balanced": {
        "label": "Balanced",
        "min_confidence": 55,
        "min_odds": 1.20,
        "preferred_markets": ["1X2", "Over/Under", "GG/NG"],
        "over_threshold": 1.5,
        "description": "All markets, Over 1.5+, min 1.20 odds per pick",
    },
    "aggressive": {
        "label": "Aggressive",
        "min_confidence": 45,
        "min_odds": 1.40,
        "preferred_markets": ["1X2", "Over/Under", "GG/NG"],
        "over_threshold": 2.5,
        "description": "Over 2.5 + wins, min 1.40 odds — fewer picks, bigger odds",
    },
    "overs_only": {
        "label": "Overs Only",
        "min_confidence": 55,
        "min_odds": 1.15,
        "preferred_markets": ["Over/Under"],
        "over_threshold": 1.5,
        "description": "Only Over/Under picks, no wins or BTTS",
    },
    "btts_mix": {
        "label": "BTTS Mix",
        "min_confidence": 50,
        "min_odds": 1.30,
        "preferred_markets": ["GG/NG", "Over/Under"],
        "over_threshold": 2.5,
        "description": "Both Teams To Score + Over 2.5 — attacking matches",
    },
    "favourites": {
        "label": "Favourites",
        "min_confidence": 65,
        "min_odds": 1.10,
        "preferred_markets": ["1X2"],
        "over_threshold": 99,
        "description": "Home/Away wins only — back the favourites",
    },
}


# ── SportyBet tournament name mapping ──────────────────────────────────────
# Maps our league IDs to partial tournament names on SportyBet (case-insensitive substring match)
# Maps our league IDs to EXACT SportyBet tournament names (case-insensitive)
# Use exact names from SportyBet API, not substrings, to avoid false matches
LEAGUE_SPORTYBET_NAMES: dict[int, list[str]] = {
    39: ["premier league"],
    40: ["efl championship", "championship"],
    140: ["laliga"],
    135: ["serie a"],
    94: ["primeira liga", "liga portugal"],
    78: ["bundesliga"],
    88: ["eredivisie"],
    61: ["ligue 1"],
    307: ["saudi professional league", "saudi pro league"],
    188: ["australia a-league", "a-league"],
    2: ["uefa champions league"],
    3: ["uefa europa league"],
    5: ["uefa nations league"],
    6: ["africa cup of nations qualification"],
    36: ["africa cup of nations qualification"],
    1: ["fifa world cup"],
    29: ["fifa world cup qualification"],
    32: ["fifa world cup qualification, uefa"],
    30: ["fifa world cup qualification"],
    31: ["fifa world cup qualification"],
    10: ["int. friendly games", "fifa series", "international friendly", "international friendlies", "friendly international"],
}


# ── Timeframe presets ──────────────────────────────────────────────────────
TIMEFRAME_PRESETS = {
    "today": {"label": "Today", "hours": 24},
    "tomorrow": {"label": "Tomorrow", "start_offset_hours": 24, "hours": 24},
    "weekend": {"label": "This Weekend"},  # Handled dynamically (Fri 6pm → Sun 11pm)
    "7days": {"label": "Next 7 Days", "hours": 168},
    "14days": {"label": "Next 14 Days", "hours": 336},
}


# ── User config (persisted preferences) ────────────────────────────────────
USER_CONFIG_PATH: Path = Path(__file__).resolve().parent / "user_config.json"
USER_CONFIG_DIR: Path = Path(__file__).resolve().parent / ".config"

# Friendly league names for Telegram display
LEAGUE_NAMES: dict[int, str] = dict(LEAGUE_DISPLAY_NAMES)

DEFAULT_USER_CONFIG = {
    "leagues": [39, 140, 135, 78, 61],  # Top 5 European leagues
    "strategy": "balanced",  # Legacy — kept for migration
    "min_confidence": 75,    # New config: 0-100
    "min_odds": 1.05,        # New config: minimum odds per pick
    "enabled_markets": list(DEFAULT_ENABLED_MARKETS),  # New config: toggled markets
    "days_ahead": 14,
    "timeframe": "7days",  # Default timeframe preset
    "telegram_chat_ids": [],
}


def _user_config_path(chat_id=None) -> Path:
    """Return the config file path for a given chat_id, or the global default."""
    if chat_id is not None:
        return USER_CONFIG_DIR / f"user_{chat_id}.json"
    return USER_CONFIG_PATH


def load_user_config(chat_id=None) -> dict:
    """Load user config from disk, or return defaults.

    If *chat_id* is provided, tries the per-user file first
    (``.config/user_{chat_id}.json``). Falls back to the global
    ``user_config.json`` if the per-user file doesn't exist.
    """
    path = _user_config_path(chat_id)
    try:
        data = json.loads(path.read_text())
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_USER_CONFIG.items():
            if k not in data:
                data[k] = v
        _migrate_user_config(data)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        # If per-user file missing, try global fallback
        if chat_id is not None:
            try:
                data = json.loads(USER_CONFIG_PATH.read_text())
                for k, v in DEFAULT_USER_CONFIG.items():
                    if k not in data:
                        data[k] = v
                _migrate_user_config(data)
                return data
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        return dict(DEFAULT_USER_CONFIG)


def _migrate_user_config(config: dict) -> None:
    """Apply lightweight migrations for older saved configs."""
    if config.get("enabled_markets") == ["1X2", "Over/Under", "GG/NG"]:
        config["enabled_markets"] = list(DEFAULT_ENABLED_MARKETS)

    leagues = config.get("leagues", DEFAULT_USER_CONFIG["leagues"])
    config["leagues"] = list(dict.fromkeys(leagues))


def save_user_config(config: dict, chat_id=None) -> None:
    """Save user config to disk.

    If *chat_id* is provided, writes to ``.config/user_{chat_id}.json``.
    Otherwise writes to the global ``user_config.json``.
    """
    path = _user_config_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))


def validate() -> list[str]:
    """Return a list of configuration warnings (empty if all OK)."""
    warnings: list[str] = []
    if not API_FOOTBALL_KEY:
        warnings.append(
            "API_FOOTBALL_KEY is not set. "
            "Export it or add it to .env (see .env.example)."
        )
    return warnings
