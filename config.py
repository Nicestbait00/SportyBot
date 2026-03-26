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
    140: "PD",     # La Liga
    135: "SA",     # Serie A
    78: "BL1",     # Bundesliga
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
    "la_liga": 140,
    "serie_a": 135,
    "bundesliga": 78,
    "ligue_1": 61,
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

# ── Strategy presets ──────────────────────────────────────────────────────────
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
    140: ["laliga"],
    135: ["serie a"],
    78: ["bundesliga"],
    61: ["ligue 1"],
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
    10: ["int. friendly games", "fifa series"],
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
LEAGUE_NAMES: dict[int, str] = {v: k.replace("_", " ").title() for k, v in LEAGUES.items() if k not in ("epl", "ucl")}

DEFAULT_USER_CONFIG = {
    "leagues": [39, 140, 135, 78, 61],  # Top 5 European leagues
    "strategy": "balanced",
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
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        # If per-user file missing, try global fallback
        if chat_id is not None:
            try:
                data = json.loads(USER_CONFIG_PATH.read_text())
                for k, v in DEFAULT_USER_CONFIG.items():
                    if k not in data:
                        data[k] = v
                return data
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        return dict(DEFAULT_USER_CONFIG)


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
