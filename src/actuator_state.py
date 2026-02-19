"""Track commanded actuator states for plant-ops-ai.

Since farmctl.py status --json only returns sensor data (temp, humidity, CO2,
light_level, soil_moisture), we track the last-commanded state of each actuator
ourselves in data/actuator_state.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = "actuator_state.json"

DEFAULT_STATE: dict[str, str] = {
    "light": "off",
    "heater": "off",
    "pump": "idle",
    "circulation": "idle",
}

# Maps action names to the state change they produce.
_ACTION_STATE_MAP: dict[str, tuple[str, str]] = {
    "light_on": ("light", "on"),
    "light_off": ("light", "off"),
    "heater_on": ("heater", "on"),
    "heater_off": ("heater", "off"),
    "water": ("pump", "idle"),        # pump is timed and self-stops
    "circulation": ("circulation", "idle"),  # fan is timed and self-stops
}


def load_actuator_state(data_dir: str) -> dict[str, str]:
    """Load the current actuator state from disk, or return defaults.

    Args:
        data_dir: Path to the data/ directory.

    Returns:
        Dict with keys: light, heater, pump, circulation.
    """
    filepath = Path(data_dir) / STATE_FILE
    if not filepath.exists():
        return dict(DEFAULT_STATE)

    try:
        with open(filepath, "r") as f:
            state = json.load(f)
        # Ensure all expected keys are present
        for key, default in DEFAULT_STATE.items():
            state.setdefault(key, default)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read actuator state, using defaults: %s", exc)
        return dict(DEFAULT_STATE)


def update_after_action(action_name: str, data_dir: str) -> None:
    """Update the actuator state file after a successful action execution.

    Args:
        action_name: The action that was executed (e.g. "light_on", "water").
        data_dir: Path to the data/ directory.
    """
    mapping = _ACTION_STATE_MAP.get(action_name)
    if mapping is None:
        return  # do_nothing, notify_human, etc. don't change state

    actuator, new_value = mapping
    state = load_actuator_state(data_dir)
    state[actuator] = new_value

    filepath = Path(data_dir) / STATE_FILE
    filepath.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(filepath, "w") as f:
            json.dump(state, f, indent=2)
        logger.debug("Actuator state updated: %s -> %s", actuator, new_value)
    except OSError as exc:
        logger.warning("Failed to write actuator state: %s", exc)
