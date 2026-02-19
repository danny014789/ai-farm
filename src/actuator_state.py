"""Track actuator states for plant-ops-ai.

Hardware-reported relay states from farmctl.py are the source of truth.
The file data/actuator_state.json serves as a cache for when hardware
state is unavailable (mock mode, old firmware, sensor read failure).
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
    "water_tank": "ok",
    "heater_lockout": "normal",
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
        Dict with keys: light, heater, pump, circulation, water_tank,
        heater_lockout.
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


def reconcile_actuator_state(
    sensor_data_dict: dict[str, Any],
    data_dir: str,
) -> dict[str, str]:
    """Build actuator state from hardware data, falling back to file.

    When hardware relay states are available in *sensor_data_dict* (i.e.
    not ``None``), those take priority over the file-based state.  When
    unavailable (mock mode, old firmware), the file-based cached state
    is used instead.

    The reconciled state is written back to disk as a cache.

    Args:
        sensor_data_dict: Dict from ``SensorData.to_dict()``.
        data_dir: Path to the data/ directory.

    Returns:
        Dict with keys: light, heater, pump, circulation, water_tank,
        heater_lockout.
    """
    state = load_actuator_state(data_dir)

    # Override with hardware truth when available
    if sensor_data_dict.get("light_on") is not None:
        state["light"] = "on" if sensor_data_dict["light_on"] else "off"

    if sensor_data_dict.get("heater_on") is not None:
        state["heater"] = "on" if sensor_data_dict["heater_on"] else "off"

    if sensor_data_dict.get("water_pump_on") is not None:
        state["pump"] = "running" if sensor_data_dict["water_pump_on"] else "idle"

    if sensor_data_dict.get("circulation_on") is not None:
        state["circulation"] = "running" if sensor_data_dict["circulation_on"] else "idle"

    if sensor_data_dict.get("water_tank_ok") is not None:
        state["water_tank"] = "ok" if sensor_data_dict["water_tank_ok"] else "low"

    if sensor_data_dict.get("heater_lockout") is not None:
        state["heater_lockout"] = (
            "active" if sensor_data_dict["heater_lockout"] else "normal"
        )

    _save_state(state, data_dir)
    return state


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

    _save_state(state, data_dir)
    logger.debug("Actuator state updated: %s -> %s", actuator, new_value)


def _save_state(state: dict[str, str], data_dir: str) -> None:
    """Write state dict to actuator_state.json."""
    filepath = Path(data_dir) / STATE_FILE
    filepath.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(filepath, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        logger.warning("Failed to write actuator state: %s", exc)
