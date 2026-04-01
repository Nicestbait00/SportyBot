#!/usr/bin/env python3.13
"""
SportyBot CLI — thin wrapper so OpenClaw can call tools via shell.

Usage:
    python3.13 sportybot_cli.py <tool_name> [key=value ...]

Examples:
    python3.13 sportybot_cli.py fetch_booking code=ABC123
    python3.13 sportybot_cli.py analyze_booking_code code=ABC123
    python3.13 sportybot_cli.py score_match home=Arsenal away=Chelsea
    python3.13 sportybot_cli.py build_ticket target_odds=5 timeframe=today
    python3.13 sportybot_cli.py split_booking_code code=ABC123 target_odds=10,50,100
    python3.13 sportybot_cli.py list_events timeframe=today
    python3.13 sportybot_cli.py get_form_data team=Arsenal
    python3.13 sportybot_cli.py config_get
    python3.13 sportybot_cli.py config_set leagues=39,140,61 timeframe=today
"""

from __future__ import annotations

import json
import sys
import os

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def parse_args(args: list[str]) -> dict:
    """Parse key=value pairs into a dict."""
    result = {}
    for arg in args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            result[k] = v
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: sportybot_cli.py <tool_name> [key=value ...]")
        print("Tools: fetch_booking, analyze_booking_code, score_match, build_ticket,")
        print("       split_booking_code, book_split_ticket, list_events,")
        print("       find_sportybet_event, get_form_data, create_booking,")
        print("       config_get, config_set")
        sys.exit(1)

    tool = sys.argv[1]
    kwargs = parse_args(sys.argv[2:])

    # Import tools lazily to keep startup fast
    from mcp_server import (
        fetch_booking, analyze_booking_code, score_match,
        build_ticket, split_booking_code, book_split_ticket,
        list_events, find_sportybet_event, get_form_data,
        create_booking,
    )

    tools = {
        "fetch_booking": lambda: fetch_booking(code=kwargs["code"]),
        "analyze_booking_code": lambda: analyze_booking_code(code=kwargs["code"]),
        "score_match": lambda: score_match(home=kwargs["home"], away=kwargs["away"]),
        "get_form_data": lambda: get_form_data(
            team=kwargs["team"],
            count=int(kwargs.get("count", 10)),
        ),
        "list_events": lambda: list_events(
            timeframe=kwargs.get("timeframe", "7days"),
            league=kwargs.get("league"),
        ),
        "find_sportybet_event": lambda: find_sportybet_event(
            home=kwargs["home"], away=kwargs["away"],
        ),
        "build_ticket": lambda: build_ticket(
            ticket_count=int(kwargs.get("ticket_count", 1)),
            target_odds=kwargs.get("target_odds", "5.0"),
            timeframe=kwargs.get("timeframe", "7days"),
            league=kwargs.get("league"),
        ),
        "split_booking_code": lambda: split_booking_code(
            code=kwargs["code"],
            target_odds=kwargs["target_odds"],
        ),
        "book_split_ticket": lambda: book_split_ticket(
            code=kwargs["code"],
            target_odds=kwargs["target_odds"],
            ticket_number=int(kwargs["ticket_number"]),
        ),
        "create_booking": lambda: create_booking(
            selections=json.loads(kwargs["selections"]),
        ),
        "config_get": lambda: _config_get(),
        "config_set": lambda: _config_set(kwargs),
        "config_options": lambda: _config_options(),
        "config_preset": lambda: _config_preset(kwargs.get("preset", "")),
    }

    if tool not in tools:
        print(f"Unknown tool: {tool}")
        print(f"Available: {', '.join(tools.keys())}")
        sys.exit(1)

    try:
        result = tools[tool]()
        print(json.dumps(result, indent=2, default=str))
    except KeyError as e:
        print(json.dumps({"error": f"Missing required parameter: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def _config_get():
    from core.config import load_user_config
    return load_user_config()


def _config_set(kwargs: dict):
    from core.config import load_user_config, save_user_config
    cfg = load_user_config()
    for k, v in kwargs.items():
        if k == "leagues":
            cfg["leagues"] = [int(x) for x in v.split(",")]
        elif k == "min_confidence":
            cfg["min_confidence"] = int(v)
        elif k == "min_odds":
            cfg["min_odds"] = float(v)
        elif k == "timeframe":
            cfg["timeframe"] = v
        elif k == "days_ahead":
            cfg["days_ahead"] = int(v)
        elif k == "enabled_markets":
            cfg["enabled_markets"] = v.split(",")
        elif k == "strategy":
            # Apply a named preset
            return _config_preset(v)
        else:
            cfg[k] = v
    save_user_config(cfg)
    return {"status": "saved", "config": cfg}


def _config_options():
    from core.config import (
        STRATEGY_PRESETS, LEAGUE_SPORTYBET_NAMES,
        LEAGUE_DISPLAY_NAMES, DEFAULT_ENABLED_MARKETS,
        TIMEFRAME_PRESETS, load_user_config,
    )
    current = load_user_config()
    return {
        "current_config": {
            "leagues": current.get("leagues", []),
            "timeframe": current.get("timeframe"),
            "min_confidence": current.get("min_confidence"),
            "min_odds": current.get("min_odds"),
            "enabled_markets": current.get("enabled_markets", []),
        },
        "available_presets": {
            name: {
                "description": p["description"],
                "min_confidence": p["min_confidence"],
                "min_odds": p.get("min_odds"),
                "markets": p["preferred_markets"],
            }
            for name, p in STRATEGY_PRESETS.items()
        },
        "available_leagues": {
            str(lid): name
            for lid, name in LEAGUE_DISPLAY_NAMES.items()
            if lid in LEAGUE_SPORTYBET_NAMES
        },
        "available_markets": list(DEFAULT_ENABLED_MARKETS),
        "available_timeframes": list(TIMEFRAME_PRESETS.keys()),
    }


def _config_preset(preset_name: str):
    from core.config import STRATEGY_PRESETS, load_user_config, save_user_config
    preset_name = preset_name.lower().strip()
    if preset_name not in STRATEGY_PRESETS:
        return {
            "error": f"Unknown preset '{preset_name}'",
            "available": list(STRATEGY_PRESETS.keys()),
        }
    preset = STRATEGY_PRESETS[preset_name]
    cfg = load_user_config()
    cfg["min_confidence"] = preset["min_confidence"]
    cfg["min_odds"] = preset.get("min_odds", 1.10)
    cfg["enabled_markets"] = preset["preferred_markets"]
    save_user_config(cfg)
    return {
        "status": "applied",
        "preset": preset_name,
        "description": preset["description"],
        "config": {
            "min_confidence": cfg["min_confidence"],
            "min_odds": cfg["min_odds"],
            "enabled_markets": cfg["enabled_markets"],
        },
    }


if __name__ == "__main__":
    main()
