"""Telegram inline keyboard builders for plant-ops-ai bot.

Provides reusable keyboard layouts for confirmations, plant stage
selection, and the main menu quick actions.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def confirm_action_keyboard(action: str) -> InlineKeyboardMarkup:
    """Yes/No confirmation for manual actions.

    The callback_data encodes the action so the confirm handler knows
    what to execute (or cancel) when the user taps a button.

    Args:
        action: Identifier for the pending action, e.g. "water_10"
            or "light_on". Passed through callback_data.

    Returns:
        Two-button inline keyboard: Confirm / Cancel.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Yes, do it", callback_data=f"confirm:{action}"
            ),
            InlineKeyboardButton(
                "Cancel", callback_data=f"cancel:{action}"
            ),
        ]
    ])


def plant_stage_keyboard() -> InlineKeyboardMarkup:
    """Growth stage selection keyboard.

    Returns:
        Four-button inline keyboard for seedling, vegetative,
        flowering, and fruiting stages.
    """
    stages = [
        ("Seedling", "stage:seedling"),
        ("Vegetative", "stage:vegetative"),
        ("Flowering", "stage:flowering"),
        ("Fruiting", "stage:fruiting"),
    ]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data)]
        for label, data in stages
    ])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Quick-action buttons for the main menu.

    Returns:
        Inline keyboard with Status, Photo, History, and Profile buttons.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu:status"),
            InlineKeyboardButton("Photo", callback_data="menu:photo"),
        ],
        [
            InlineKeyboardButton("History", callback_data="menu:history"),
            InlineKeyboardButton("Profile", callback_data="menu:profile"),
        ],
    ])
