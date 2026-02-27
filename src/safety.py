"""Safety layer for plant-ops-ai.

Enforces hardcoded safety limits that the AI agent cannot override.
All actions must pass through validate_action() before execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.config_loader import load_safety_limits
from src.sensor_reader import SensorData

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validating a proposed action."""

    valid: bool
    reason: str
    capped_action: dict[str, Any]


# Actions the AI is allowed to request. Anything else is rejected.
ALLOWED_ACTIONS = {
    "water",          # water(duration_sec)
    "light_on",       # turn grow light on
    "light_off",      # turn grow light off
    "heater_on",      # turn heater on
    "heater_off",     # turn heater off
    "circulation",    # circulation(duration_sec)
    "do_nothing",     # always allowed
    "notify_human",   # always allowed
}


def _load_limits() -> dict[str, Any]:
    """Load safety limits from config, with sane defaults if file is missing."""
    try:
        return load_safety_limits()
    except FileNotFoundError:
        logger.warning("safety_limits.yaml not found, using built-in defaults")
        return {
            "water": {
                "max_duration_sec": 30,
                "min_interval_min": 60,
                "daily_max_count": 6,
            },
            "heater": {
                "max_temp_c": 30.0,
                "min_temp_c": 10.0,
                "max_continuous_min": 120,
            },
            "light": {
                "max_hours_per_day": 18,
                "schedule_on": "06:00",
                "schedule_off": "24:00",
            },
            "circulation": {
                "max_duration_sec": 3600,
            },
            "emergency_stop_file": "/tmp/plant-agent-stop",
            "max_actions_per_hour": 10,
        }


def check_emergency_stop(limits: dict[str, Any] | None = None) -> bool:
    """Check if the emergency stop file exists.

    Args:
        limits: Safety limits dict. Loaded from config if None.

    Returns:
        True if emergency stop is active (file exists).
    """
    if limits is None:
        limits = _load_limits()

    stop_file = Path(limits.get("emergency_stop_file", "/tmp/plant-agent-stop"))
    return stop_file.exists()


def validate_action(
    action: dict[str, Any],
    sensor_data: SensorData,
    history: list[dict[str, Any]],
) -> ValidationResult:
    """Validate a proposed action against safety limits.

    Checks performed in order:
    1. Emergency stop file
    2. Action is in the allowlist
    3. Global rate limit (max actions per hour)
    4. Action-specific limits (duration caps, temp checks, rate limits)

    Args:
        action: Proposed action dict with at least an "action" key.
            Examples:
                {"action": "water", "duration_sec": 15}
                {"action": "heater", "state": "on"}
                {"action": "light", "state": "off"}
                {"action": "do_nothing"}
        sensor_data: Current sensor readings.
        history: List of recent action dicts, each with an "executed_at"
            ISO timestamp and an "action" key.

    Returns:
        ValidationResult with valid flag, reason, and (possibly capped) action.
    """
    limits = _load_limits()
    # Flatten params into top-level for validation convenience.
    # Claude returns {"action": "water", "params": {"duration_sec": 8}}
    # but validators expect {"action": "water", "duration_sec": 8}.
    capped = dict(action)
    if "params" in capped and isinstance(capped["params"], dict):
        for k, v in capped["params"].items():
            capped.setdefault(k, v)

    # 1. Emergency stop
    if check_emergency_stop(limits):
        return ValidationResult(
            valid=False,
            reason="Emergency stop file is present. All actions halted.",
            capped_action=capped,
        )

    action_type = action.get("action", "")

    # 2. Allowlist check
    if action_type not in ALLOWED_ACTIONS:
        return ValidationResult(
            valid=False,
            reason=f"Action '{action_type}' is not in the allowlist. "
                   f"Allowed: {sorted(ALLOWED_ACTIONS)}",
            capped_action=capped,
        )

    # Passthrough actions always valid (after emergency stop check)
    if action_type in ("do_nothing", "notify_human"):
        return ValidationResult(valid=True, reason="OK", capped_action=capped)

    # 3. Global rate limit
    now = datetime.now(timezone.utc)
    max_per_hour = limits.get("max_actions_per_hour", 10)
    recent_actions = _actions_in_window(history, now, minutes=60)

    # Only count actions that actually did something (not do_nothing/notify)
    real_actions = [
        a for a in recent_actions
        if a.get("decision", {}).get("action") not in ("do_nothing", "notify_human")
    ]
    if len(real_actions) >= max_per_hour:
        return ValidationResult(
            valid=False,
            reason=f"Global rate limit reached: {len(real_actions)}/{max_per_hour} "
                   f"actions in the last hour.",
            capped_action=capped,
        )

    # 4. Action-specific validation
    if action_type == "water":
        return _validate_water(capped, limits, history, now, sensor_data)

    if action_type in ("heater_on", "heater_off"):
        return _validate_heater(capped, limits, sensor_data)

    if action_type in ("light_on", "light_off"):
        return _validate_light(capped, limits)

    if action_type == "circulation":
        return _validate_circulation(capped, limits, history, now)

    # Should not reach here given allowlist check above
    return ValidationResult(valid=True, reason="OK", capped_action=capped)


def _validate_water(
    action: dict[str, Any],
    limits: dict[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
    sensor_data: SensorData | None = None,
) -> ValidationResult:
    """Validate and cap water action."""
    # Block watering if water tank is low
    if sensor_data is not None and sensor_data.water_tank_ok is False:
        return ValidationResult(
            valid=False,
            reason="Water tank level is LOW. Refill the tank before watering.",
            capped_action=action,
        )

    water_limits = limits.get("water", {})
    max_duration = water_limits.get("max_duration_sec", 30)
    min_interval = water_limits.get("min_interval_min", 60)
    daily_max = water_limits.get("daily_max_count", 6)

    # Cap duration
    requested = action.get("duration_sec", 0)
    if requested <= 0:
        return ValidationResult(
            valid=False,
            reason="Water duration_sec must be positive.",
            capped_action=action,
        )

    if requested > max_duration:
        logger.info("Capping water duration from %ds to %ds", requested, max_duration)
        action["duration_sec"] = max_duration
        action["_capped"] = True

    # Min interval check
    recent_water = _actions_in_window(
        history, now, minutes=min_interval, action_type="water"
    )
    if recent_water:
        last = recent_water[-1].get("timestamp", "unknown")
        last_ts = _parse_timestamp(last) if last != "unknown" else None
        if last_ts is not None:
            remaining_min = int(min_interval - (now - last_ts).total_seconds() / 60)
            detail = f"Last watering at {last} ({remaining_min} min remaining)."
        else:
            detail = f"Last watering at {last}."
        return ValidationResult(
            valid=False,
            reason=f"Water rate limit: must wait {min_interval} min between waterings. "
                   f"{detail}",
            capped_action=action,
        )

    # Daily max count
    today_water = _actions_today(history, now, action_type="water")
    if len(today_water) >= daily_max:
        return ValidationResult(
            valid=False,
            reason=f"Daily water limit reached: {len(today_water)}/{daily_max} today.",
            capped_action=action,
        )

    return ValidationResult(valid=True, reason="OK", capped_action=action)


def _validate_heater(
    action: dict[str, Any],
    limits: dict[str, Any],
    sensor_data: SensorData,
) -> ValidationResult:
    """Validate heater action against temperature limits."""
    heater_limits = limits.get("heater", {})
    max_temp = heater_limits.get("max_temp_c", 30.0)

    action_type = action.get("action", "")

    # Turning off is always allowed
    if action_type == "heater_off":
        return ValidationResult(valid=True, reason="OK", capped_action=action)

    # Block heater_on if firmware lockout is active
    if sensor_data.heater_lockout is True:
        return ValidationResult(
            valid=False,
            reason="Heater lockout is active (firmware safety). Cannot turn heater on.",
            capped_action=action,
        )

    # Refuse heater_on if temperature is already above max
    if sensor_data.temperature_c >= max_temp:
        return ValidationResult(
            valid=False,
            reason=f"Cannot turn heater on: current temp {sensor_data.temperature_c}C "
                   f">= max {max_temp}C.",
            capped_action=action,
        )

    return ValidationResult(valid=True, reason="OK", capped_action=action)


def _validate_light(
    action: dict[str, Any],
    limits: dict[str, Any],
) -> ValidationResult:
    """Validate light action against schedule."""
    action_type = action.get("action", "")

    # Turning off is always allowed
    if action_type == "light_off":
        return ValidationResult(valid=True, reason="OK", capped_action=action)

    # Check against light schedule
    light_limits = limits.get("light", {})
    schedule_on = light_limits.get("schedule_on", "06:00")
    schedule_off = light_limits.get("schedule_off", "24:00")

    now_time = datetime.now().strftime("%H:%M")
    if now_time < schedule_on:
        return ValidationResult(
            valid=False,
            reason=f"Too early for lights: current time {now_time}, "
                   f"earliest on at {schedule_on}.",
            capped_action=action,
        )

    # "24:00" means no cutoff; only check if it's a real time
    if schedule_off != "24:00" and now_time >= schedule_off:
        return ValidationResult(
            valid=False,
            reason=f"Too late for lights: current time {now_time}, "
                   f"latest off at {schedule_off}.",
            capped_action=action,
        )

    return ValidationResult(valid=True, reason="OK", capped_action=action)


def _validate_circulation(
    action: dict[str, Any],
    limits: dict[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
) -> ValidationResult:
    """Validate and cap circulation fan action."""
    circ_limits = limits.get("circulation", {})
    max_duration = circ_limits.get("max_duration_sec", 3600)

    # Cap duration
    requested = action.get("duration_sec", 0)
    if requested <= 0:
        return ValidationResult(
            valid=False,
            reason="Circulation duration_sec must be positive.",
            capped_action=action,
        )

    if requested > max_duration:
        logger.info(
            "Capping circulation duration from %ds to %ds", requested, max_duration
        )
        action["duration_sec"] = max_duration
        action["_capped"] = True

    return ValidationResult(valid=True, reason="OK", capped_action=action)


# --- History helpers ---


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO timestamp string, returning None on failure."""
    try:
        dt = datetime.fromisoformat(ts)
        # Ensure timezone-aware for comparison
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _actions_in_window(
    history: list[dict[str, Any]],
    now: datetime,
    minutes: int,
    action_type: str | None = None,
) -> list[dict[str, Any]]:
    """Filter history to actions within the last N minutes.

    Args:
        history: List of decision log records with "timestamp" and nested "decision" keys.
        now: Current time (timezone-aware).
        minutes: Window size in minutes.
        action_type: If set, only include actions of this type.

    Returns:
        Filtered list of actions within the window.
    """
    cutoff = now - timedelta(minutes=minutes)
    results = []

    for entry in history:
        # Log records store timestamp under "timestamp", action under "decision.action".
        ts = _parse_timestamp(entry.get("timestamp", ""))
        if ts is None:
            continue
        if ts < cutoff:
            continue
        entry_action = entry.get("decision", {}).get("action", "")
        if action_type and entry_action != action_type:
            continue
        results.append(entry)

    return results


def _actions_today(
    history: list[dict[str, Any]],
    now: datetime,
    action_type: str | None = None,
) -> list[dict[str, Any]]:
    """Filter history to actions from today (UTC).

    Args:
        history: List of decision log records with "timestamp" and nested "decision" keys.
        now: Current time (timezone-aware).
        action_type: If set, only include actions of this type.

    Returns:
        Filtered list of actions from today.
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    results = []

    for entry in history:
        # Log records store timestamp under "timestamp", action under "decision.action".
        ts = _parse_timestamp(entry.get("timestamp", ""))
        if ts is None:
            continue
        if ts < today_start:
            continue
        entry_action = entry.get("decision", {}).get("action", "")
        if action_type and entry_action != action_type:
            continue
        results.append(entry)

    return results
