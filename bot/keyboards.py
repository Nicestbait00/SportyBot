"""Telegram keyboard builders for SportyBot."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import LEAGUE_CATEGORIES, LEAGUE_DISPLAY_NAMES


def _build_league_keyboard(active: set[int], in_settings: bool = False) -> InlineKeyboardMarkup:
    """Build the league picker keyboard with category shortcuts."""
    buttons = []

    category_row = []
    for key, category in LEAGUE_CATEGORIES.items():
        category_ids = set(category["league_ids"])
        enabled = bool(category_ids) and category_ids.issubset(active)
        category_row.append(
            InlineKeyboardButton(
                f"{'✅' if enabled else '⬜'} {category['label']}",
                callback_data=f"league_cat_{key}",
            )
        )
        if len(category_row) == 2:
            buttons.append(category_row)
            category_row = []
    if category_row:
        buttons.append(category_row)

    for category in LEAGUE_CATEGORIES.values():
        row = []
        for lid in category["league_ids"]:
            name = LEAGUE_NAMES.get(lid, str(lid))
            check = "✅" if lid in active else "⬜"
            row.append(
                InlineKeyboardButton(
                    f"{check} {name}",
                    callback_data=f"league_toggle_{lid}",
                )
            )
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    categorized = {
        lid
        for category in LEAGUE_CATEGORIES.values()
        for lid in category["league_ids"]
    }
    other_leagues = sorted(
        (
            (lid, name)
            for lid, name in LEAGUE_NAMES.items()
            if lid not in categorized
        ),
        key=lambda item: item[1],
    )

    row = []
    for lid, name in other_leagues:
        check = "✅" if lid in active else "⬜"
        row.append(
            InlineKeyboardButton(
                f"{check} {name}",
                callback_data=f"league_toggle_{lid}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    done_btn = InlineKeyboardButton("⬅️ Back", callback_data="league_done") if in_settings else InlineKeyboardButton("✅ Done", callback_data="league_done")
    buttons.append(
        [
            InlineKeyboardButton("🧹 Clear All", callback_data="league_clear_all"),
            done_btn,
        ]
    )
    return InlineKeyboardMarkup(buttons)


def _format_league_selection_text(active: set[int]) -> str:
    """Render a short summary for the league picker."""
    preset_lines = []
    for category in LEAGUE_CATEGORIES.values():
        preset_lines.append(f"{category['label']}: {category['description']}")

    active_names = [LEAGUE_NAMES.get(lid, str(lid)) for lid in sorted(active)]
    active_summary = ", ".join(active_names) if active_names else "None selected yet"

    return (
        "Select leagues to analyze.\n"
        "Quick presets:\n"
        + "\n".join(preset_lines)
        + f"\n\nActive: {active_summary}\n"
        "Tap a category or individual leagues."
    )


