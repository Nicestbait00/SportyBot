"""SportyBot /strategy conversation handler.

Handles settings hub: strategy presets, custom confidence/markets/over/min_odds, league/timeframe selection.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from core.config import (
    AVAILABLE_MARKETS,
    DEFAULT_ENABLED_MARKETS,
    LEAGUE_CATEGORIES,
    LEAGUE_NAMES,
    STRATEGY_PRESETS,
    TIMEFRAME_PRESETS,
    load_user_config,
    save_user_config,
)
from bot.keyboards import _build_league_keyboard, _format_league_selection_text

logger = logging.getLogger(__name__)

# Conversation states
STRAT_CUSTOM_CONFIDENCE, STRAT_CUSTOM_MARKETS, STRAT_CUSTOM_OVER, STRAT_CUSTOM_MIN_ODDS, STRAT_LEAGUES, STRAT_TIMEFRAME = range(20, 26)


def _get_strategy_config(user_config: dict) -> dict:
    """Return effective strategy config (alias for pick config)."""
    return _get_pick_config(user_config)

def _get_config_label(user_config: dict) -> str:
    """Return a one-line label for the current config."""
    pc = _get_pick_config(user_config)
    conf = pc["min_confidence"]
    odds = pc["min_odds"]
    markets = pc["preferred_markets"]
    return f"Confidence >= {conf} | Min odds {odds} | {len(markets)} markets"

def _get_strategy_label(user_config: dict) -> str:
    """Return the strategy preset label or 'Custom'."""
    return _get_config_label(user_config)

def _get_pick_config(user_config: dict) -> dict:
    """Return the effective pick configuration."""
    if "min_confidence" in user_config and "enabled_markets" in user_config:
        return {
            "min_confidence": user_config.get("min_confidence", 75),
            "min_odds": user_config.get("min_odds", 1.05),
            "preferred_markets": user_config.get("enabled_markets", DEFAULT_ENABLED_MARKETS),
        }
    strat = user_config.get("strategy", "balanced")
    if isinstance(strat, dict):
        return {
            "min_confidence": strat.get("min_confidence", 60),
            "min_odds": strat.get("min_odds", 1.15),
            "preferred_markets": strat.get("preferred_markets", list(DEFAULT_ENABLED_MARKETS)),
        }
    preset = STRATEGY_PRESETS.get(strat, STRATEGY_PRESETS["balanced"])
    return {
        "min_confidence": preset["min_confidence"],
        "min_odds": preset["min_odds"],
        "preferred_markets": preset["preferred_markets"],
    }


def _settings_hub_content(config: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the settings hub text and keyboard. Reused from multiple places."""
    min_conf = config.get("min_confidence", 75)
    min_odds = config.get("min_odds", 1.05)
    enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
    active_leagues = config.get("leagues", [])
    league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in active_leagues[:5]]
    league_summary = ", ".join(league_names) or "None"
    if len(active_leagues) > 5:
        league_summary += f" +{len(active_leagues) - 5} more"
    from core.config import TIMEFRAME_PRESETS
    tf_key = config.get("timeframe", "7days")
    tf_label = TIMEFRAME_PRESETS.get(tf_key, {}).get("label", tf_key)

    text = (
        "⚙️ *Settings*\n\n"
        f"📋 Leagues: {league_summary}\n"
        f"⏰ Timeframe: {tf_label}\n"
        f"🎯 Confidence: ≥{min_conf}%\n"
        f"📊 Markets: {len(enabled)} enabled\n"
        f"💰 Min Odds: ≥{min_odds}\n\n"
        "Tap to adjust:"
    )
    buttons = [
        [
            InlineKeyboardButton("📋 Leagues", callback_data="strat_leagues_menu"),
            InlineKeyboardButton("⏰ Timeframe", callback_data="strat_timeframe_menu"),
        ],
        [InlineKeyboardButton(f"🎯 Confidence ({min_conf}%)", callback_data="strat_conf_menu")],
        [InlineKeyboardButton(f"📊 Markets ({len(enabled)})", callback_data="strat_mkt_menu")],
        [InlineKeyboardButton(f"💰 Min Odds ({min_odds})", callback_data="strat_odds_menu")],
        [InlineKeyboardButton("✅ Done", callback_data="strat_done")],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show unified settings panel — leagues, timeframe, confidence, markets, min odds."""
    chat_id = update.effective_chat.id
    context.user_data["chat_id"] = chat_id
    config = load_user_config(chat_id=chat_id)
    text, markup = _settings_hub_content(config)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strategy_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings panel navigation."""
    query = update.callback_query
    await query.answer()
    data = query.data

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)

    if data == "strat_conf_menu":
        # Show confidence range buttons
        current = config.get("min_confidence", 75)
        buttons = [
            [
                InlineKeyboardButton(f"{'✅ ' if current == 50 else ''}50%", callback_data="strat_conf_50"),
                InlineKeyboardButton(f"{'✅ ' if current == 60 else ''}60%", callback_data="strat_conf_60"),
                InlineKeyboardButton(f"{'✅ ' if current == 65 else ''}65%", callback_data="strat_conf_65"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == 70 else ''}70%", callback_data="strat_conf_70"),
                InlineKeyboardButton(f"{'✅ ' if current == 75 else ''}75%", callback_data="strat_conf_75"),
                InlineKeyboardButton(f"{'✅ ' if current == 80 else ''}80%", callback_data="strat_conf_80"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == 85 else ''}85%", callback_data="strat_conf_85"),
                InlineKeyboardButton(f"{'✅ ' if current == 90 else ''}90%", callback_data="strat_conf_90"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")],
        ]
        await query.edit_message_text(
            f"🎯 Minimum Confidence\nCurrent: {current}%\n\n"
            "Lower = more picks (riskier)\nHigher = fewer picks (safer)\n\n"
            "Recommended: 75% for balanced results",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_CONFIDENCE

    elif data == "strat_mkt_menu":
        # Show market toggle panel
        enabled = set(config.get("enabled_markets", DEFAULT_ENABLED_MARKETS))
        buttons = []
        for mkt_name, mkt_info in AVAILABLE_MARKETS.items():
            check = "✅" if mkt_name in enabled else "⬜"
            state = "on" if mkt_name in enabled else "off"
            buttons.append([InlineKeyboardButton(
                f"{check} {mkt_name} — {mkt_info['description']}",
                callback_data=f"strat_mkt_{state}_{mkt_name}",
            )])
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="strat_back")])
        await query.edit_message_text(
            "📊 Market Selection\nTap to toggle on/off:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_MARKETS

    elif data == "strat_odds_menu":
        # Show min odds buttons
        current = config.get("min_odds", 1.05)
        options = [1.01, 1.05, 1.10, 1.15, 1.20, 1.30, 1.40, 1.60]
        row1 = []
        row2 = []
        for i, o in enumerate(options):
            mark = "✅ " if abs(current - o) < 0.005 else ""
            btn = InlineKeyboardButton(f"{mark}{o:.2f}", callback_data=f"strat_minodds_{o:.2f}")
            if i < 4:
                row1.append(btn)
            else:
                row2.append(btn)
        buttons = [row1, row2, [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")]]
        await query.edit_message_text(
            f"💰 Minimum Odds Per Pick\nCurrent: {current}\n\n"
            "Lower = allows safer low-odds picks (e.g. Over 0.5)\n"
            "Higher = filters for bigger odds per pick",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_CUSTOM_MIN_ODDS

    elif data == "strat_leagues_menu":
        active = set(config.get("leagues", []))
        await query.edit_message_text(
            _format_league_selection_text(active),
            reply_markup=_build_league_keyboard(active, in_settings=True),
        )
        return STRAT_LEAGUES

    elif data == "strat_timeframe_menu":
        from core.config import TIMEFRAME_PRESETS
        current = config.get("timeframe", "7days")
        buttons = [
            [
                InlineKeyboardButton(f"{'✅ ' if current == 'today' else ''}Today", callback_data="strat_tf_today"),
                InlineKeyboardButton(f"{'✅ ' if current == 'tomorrow' else ''}Tomorrow", callback_data="strat_tf_tomorrow"),
                InlineKeyboardButton(f"{'✅ ' if current == 'weekend' else ''}Weekend", callback_data="strat_tf_weekend"),
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current == '7days' else ''}Next 7 Days", callback_data="strat_tf_7days"),
                InlineKeyboardButton(f"{'✅ ' if current == '14days' else ''}Next 14 Days", callback_data="strat_tf_14days"),
            ],
            [InlineKeyboardButton("⬅️ Back", callback_data="strat_back")],
        ]
        current_label = TIMEFRAME_PRESETS.get(current, {}).get("label", current)
        await query.edit_message_text(
            f"⏰ Timeframe\nCurrent: {current_label}\n\nHow far ahead should /pick scan?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return STRAT_TIMEFRAME

    elif data == "strat_back":
        # Back to main settings hub
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    elif data == "strat_done":
        min_conf = config.get("min_confidence", 75)
        min_odds = config.get("min_odds", 1.05)
        enabled = config.get("enabled_markets", DEFAULT_ENABLED_MARKETS)
        active_leagues = config.get("leagues", [])
        league_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in active_leagues]
        from core.config import TIMEFRAME_PRESETS
        tf_label = TIMEFRAME_PRESETS.get(config.get("timeframe", "7days"), {}).get("label", "7 days")
        await query.edit_message_text(
            f"✅ Settings saved!\n\n"
            f"Leagues: {', '.join(league_names) or 'None'}\n"
            f"Timeframe: {tf_label}\n"
            f"Confidence: ≥{min_conf}%\n"
            f"Min Odds: ≥{min_odds}\n"
            f"Markets: {', '.join(enabled)}\n\n"
            "Run /pick to get selections."
        )
        return ConversationHandler.END

    # Legacy preset fallback
    preset_key = data.replace("strat_", "")
    if preset_key in STRATEGY_PRESETS:
        preset = STRATEGY_PRESETS[preset_key]
        config["min_confidence"] = preset["min_confidence"]
        config["min_odds"] = preset.get("min_odds", 1.10)
        config["enabled_markets"] = preset["preferred_markets"]
        save_user_config(config, chat_id=chat_id)
        await query.edit_message_text(
            f"Settings applied from {preset['label']} preset.\n\n"
            f"Confidence: ≥{preset['min_confidence']}%\n"
            f"Min Odds: ≥{preset.get('min_odds', 1.10)}\n"
            f"Markets: {', '.join(preset['preferred_markets'])}\n\n"
            "Run /pick to get selections."
        )
        return ConversationHandler.END

    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_custom_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle confidence selection."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_conf_menu", "strat_mkt_menu", "strat_odds_menu", "strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    conf = int(data.replace("strat_conf_", ""))
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["min_confidence"] = conf
    save_user_config(config, chat_id=chat_id)

    # Show updated settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Confidence set to {conf}%\n\n" + text,
        reply_markup=markup,
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_custom_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle market toggle in settings."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    enabled = config.get("enabled_markets", list(DEFAULT_ENABLED_MARKETS))

    # Parse toggle: strat_mkt_{on|off}_{market_name}
    parts = data.replace("strat_mkt_", "").split("_", 1)
    current_state = parts[0]
    market_name = parts[1]

    if current_state == "on" and market_name in enabled:
        enabled.remove(market_name)
    elif current_state == "off" and market_name not in enabled:
        enabled.append(market_name)

    # Ensure at least one market is enabled
    if not enabled:
        enabled = list(DEFAULT_ENABLED_MARKETS)

    config["enabled_markets"] = enabled
    save_user_config(config, chat_id=chat_id)

    # Rebuild buttons
    buttons = []
    active = set(enabled)
    for mkt_name, mkt_info in AVAILABLE_MARKETS.items():
        check = "✅" if mkt_name in active else "⬜"
        state = "on" if mkt_name in active else "off"
        buttons.append([InlineKeyboardButton(
            f"{check} {mkt_name} — {mkt_info['description']}",
            callback_data=f"strat_mkt_{state}_{mkt_name}",
        )])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="strat_back")])

    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    return STRAT_CUSTOM_MARKETS


async def callback_strat_custom_over(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy — redirect to main settings."""
    return await callback_strategy_select(update, context)


async def callback_strat_custom_min_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle min odds selection."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("strat_back", "strat_done"):
        return await callback_strategy_select(update, context)

    min_odds = float(data.replace("strat_minodds_", ""))
    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["min_odds"] = min_odds
    save_user_config(config, chat_id=chat_id)

    # Show updated settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Min Odds set to {min_odds}\n\n" + text,
        reply_markup=markup,
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def callback_strat_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league toggles within the settings ConversationHandler."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data == "league_done":
        # Back to settings hub
        config = load_user_config(chat_id=chat_id)
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    # Reuse the existing toggle logic
    config = load_user_config(chat_id=chat_id)
    leagues = list(config.get("leagues", []))

    if data == "league_clear_all":
        leagues = []
    elif data.startswith("league_cat_"):
        category_key = data.replace("league_cat_", "")
        category = LEAGUE_CATEGORIES.get(category_key)
        if category:
            category_ids = category["league_ids"]
            if all(lid in leagues for lid in category_ids):
                leagues = [lid for lid in leagues if lid not in category_ids]
            else:
                leagues = list(dict.fromkeys(leagues + category_ids))
    elif data.startswith("league_toggle_"):
        lid = int(data.replace("league_toggle_", ""))
        if lid in leagues:
            leagues.remove(lid)
        else:
            leagues.append(lid)

    config["leagues"] = leagues
    save_user_config(config, chat_id=chat_id)
    active = set(leagues)
    await query.edit_message_text(
        _format_league_selection_text(active),
        reply_markup=_build_league_keyboard(active, in_settings=True),
    )
    return STRAT_LEAGUES


async def callback_strat_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe selection within settings ConversationHandler."""
    from core.config import TIMEFRAME_PRESETS

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "strat_back":
        config = load_user_config(chat_id=update.effective_chat.id)
        text, markup = _settings_hub_content(config)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return STRAT_CUSTOM_CONFIDENCE

    preset_key = data.replace("strat_tf_", "")
    preset = TIMEFRAME_PRESETS.get(preset_key)
    if not preset:
        return STRAT_TIMEFRAME

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["timeframe"] = preset_key
    save_user_config(config, chat_id=chat_id)

    # Return to settings hub
    text, markup = _settings_hub_content(config)
    await query.edit_message_text(
        f"✅ Timeframe set to {preset['label']}\n\n" + text,
        reply_markup=markup,
        parse_mode="Markdown",
    )
    return STRAT_CUSTOM_CONFIDENCE


async def strategy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the settings flow."""
    context.user_data.pop("custom_strategy", None)
    await update.message.reply_text("Settings cancelled.")
    return ConversationHandler.END




async def cmd_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect to unified settings."""
    return await cmd_strategy(update, context)


async def cmd_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect to unified settings."""
    return await cmd_strategy(update, context)


async def callback_league_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league toggle from standalone /leagues (outside settings)."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data
    if data == "league_done":
        config = load_user_config(chat_id=chat_id)
        names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in config["leagues"]]
        await query.edit_message_text(
            f"Leagues set: {', '.join(names) or 'None'}\n\n"
            "Now run /pick to get selections.",
        )
        return

    config = load_user_config(chat_id=chat_id)
    leagues = list(config.get("leagues", []))

    if data == "league_clear_all":
        leagues = []
    elif data.startswith("league_cat_"):
        category_key = data.replace("league_cat_", "")
        category = LEAGUE_CATEGORIES.get(category_key)
        if category:
            category_ids = category["league_ids"]
            if all(lid in leagues for lid in category_ids):
                leagues = [lid for lid in leagues if lid not in category_ids]
            else:
                leagues = list(dict.fromkeys(leagues + category_ids))
    else:
        lid = int(data.replace("league_toggle_", ""))
        if lid in leagues:
            leagues.remove(lid)
        else:
            leagues.append(lid)

    config["leagues"] = leagues
    save_user_config(config, chat_id=chat_id)

    active = set(leagues)
    await query.edit_message_text(
        _format_league_selection_text(active),
        reply_markup=_build_league_keyboard(active),
    )


async def callback_timeframe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle timeframe from standalone /timeframe (outside settings)."""
    from core.config import TIMEFRAME_PRESETS

    query = update.callback_query
    await query.answer()
    preset_key = query.data.replace("tf_", "")
    preset = TIMEFRAME_PRESETS.get(preset_key)
    if not preset:
        await query.edit_message_text("Unknown timeframe. Try /settings.")
        return

    chat_id = update.effective_chat.id
    config = load_user_config(chat_id=chat_id)
    config["timeframe"] = preset_key
    save_user_config(config, chat_id=chat_id)
    await query.edit_message_text(
        f"✅ Timeframe set to: {preset['label']}\n\nRun /pick to get selections."
    )


# ── /split conversation flow ──────────────────────────────────────────────────

