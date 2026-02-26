"""Plant agent orchestrator - the main decision loop.

This is the core module that ties everything together:
  1. Read sensors
  2. Load plant context (profile + knowledge + history)
  3. Ask Claude for a decision
  4. Validate decision against safety limits
  5. Execute validated action
  6. Log everything
  7. Return a summary (for Telegram bot or CLI output)

Can be run standalone via CLI for testing, or called from the Telegram bot's
scheduled job.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.action_executor import ActionExecutor
from src.actuator_state import reconcile_actuator_state, update_after_action
from src.claude_client import get_plant_decision
from src.config_loader import load_hardware_profile, load_plant_profile, save_hardware_profile
from src.logger import (
    load_recent_decisions,
    load_recent_plant_log,
    log_decision,
    log_plant_observations,
    log_sensor_reading,
)
from src.plant_knowledge import ensure_plant_knowledge
from src.safety import validate_action
from src.sensor_reader import SensorData, SensorReadError, read_sensors, read_sensors_mock
from src.weather import fetch_weather

logger = logging.getLogger(__name__)

# Offline fallback rules - applied when Claude API is unreachable.
# These are intentionally simple and conservative.
FALLBACK_RULES = {
    "soil_moisture_critical": {
        "condition": lambda s: s.soil_moisture_pct < 25,
        "action": {"action": "water", "params": {"duration_sec": 5}},
        "reason": "Offline fallback: soil critically dry",
    },
    "temp_too_cold": {
        "condition": lambda s: s.temperature_c < 15,
        "action": {"action": "heater_on", "params": {}},
        "reason": "Offline fallback: temperature dangerously low",
    },
    "temp_too_hot": {
        "condition": lambda s: s.temperature_c > 32,
        "action": {"action": "heater_off", "params": {}},
        "reason": "Offline fallback: temperature too high, ensuring heater is off",
    },
}


def run_check(
    farmctl_path: str,
    data_dir: str,
    dry_run: bool = False,
    use_mock: bool = False,
    include_photo: bool = True,
    verbose: bool = False,
) -> dict:
    """Run a single monitoring check cycle.

    This is the main entry point called by the Telegram bot scheduler
    and the CLI. It performs the full sense -> think -> act loop.

    Args:
        farmctl_path: Absolute path to farmctl.py on the Pi.
        data_dir: Path to the data/ directory for logs and cached knowledge.
        dry_run: If True, log recommendations but don't execute hardware commands.
        use_mock: If True, use mock sensor data (for local development).
        include_photo: If True, capture a photo for Claude's analysis.
        verbose: If True, log at DEBUG level.

    Returns:
        Summary dict with keys: sensor_data, decision, validation,
        executed, photo_path, error (if any).
    """
    summary: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sensor_data": None,
        "decision": None,
        "actions_taken": [],
        "executed": False,
        "photo_path": None,
        "error": None,
        "mode": "dry-run" if dry_run else "live",
    }

    # --- 0. Load hardware profile ---
    try:
        hardware_profile = load_hardware_profile()
    except FileNotFoundError:
        hardware_profile = {}

    # --- 1. Read sensors ---
    try:
        if use_mock:
            sensor_data = read_sensors_mock()
            logger.info("Using mock sensor data")
        else:
            sensor_data = read_sensors(farmctl_path)
            logger.info("Sensor read OK: temp=%.1fC soil=%.0f%%",
                        sensor_data.temperature_c, sensor_data.soil_moisture_pct)
    except SensorReadError as e:
        logger.error("Sensor read failed: %s", e)
        summary["error"] = f"Sensor read failed: {e}"
        return summary

    summary["sensor_data"] = sensor_data.to_dict()
    log_sensor_reading(sensor_data, data_dir)

    # --- 1b. Fetch outdoor weather (optional, non-blocking) ---
    weather_data = fetch_weather()
    summary["weather_data"] = weather_data

    # --- 2. Optionally capture photo (with light) ---
    photo_path = None
    if include_photo and not use_mock:
        photo_executor = ActionExecutor(farmctl_path, dry_run=False)
        photo_path = photo_executor.take_photo_with_light(
            output_path=os.path.join(data_dir, "plant_latest.jpg"),
            data_dir=data_dir if not dry_run else None,
            photos_dir=os.path.join(data_dir, "photos"),
        )
        if photo_path:
            logger.info("Photo captured: %s", photo_path)
        else:
            logger.warning("Photo capture failed, continuing without photo")
    summary["photo_path"] = photo_path

    # --- 3. Load context ---
    profile = load_plant_profile()
    history = load_recent_decisions(10, data_dir)

    plant_knowledge = ""
    plant_name = profile.get("plant", {}).get("name", "")
    if plant_name:
        try:
            plant_knowledge = ensure_plant_knowledge(profile, data_dir)
        except Exception as e:
            logger.warning("Failed to load plant knowledge: %s", e)

    # --- 4. Load actuator state (reconciled with hardware) and plant log ---
    actuator_state = reconcile_actuator_state(sensor_data.to_dict(), data_dir)
    plant_log = load_recent_plant_log(20, data_dir)

    # --- 5. Ask Claude for decision ---
    decision = None
    try:
        decision = get_plant_decision(
            sensor_data=sensor_data.to_dict(),
            plant_profile=profile,
            plant_knowledge=plant_knowledge,
            history=history,
            photo_path=photo_path,
            actuator_state=actuator_state,
            plant_log=plant_log,
            hardware_profile=hardware_profile,
            weather_data=weather_data,
        )
        actions_summary = ", ".join(
            a.get("action", "?") for a in decision.get("actions", [])
        )
        logger.info("Claude decision: actions=[%s]", actions_summary)
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        # Apply offline fallback rules
        decision = _apply_fallback_rules(sensor_data)
        if decision:
            logger.info("Applying offline fallback: %s",
                        decision.get("actions", [{}])[0].get("reason", ""))
        else:
            decision = {
                "actions": [{"action": "do_nothing", "params": {}, "reason": f"API unreachable, no fallback triggered: {e}"}],
                "urgency": "attention",
                "notify_human": True,
                "assessment": "Unable to reach Claude API",
                "notes": str(e),
            }

    summary["decision"] = decision

    # --- 6. Validate, execute, and log each action ---
    executor = ActionExecutor(farmctl_path, dry_run=dry_run)
    decision_context = {
        "urgency": decision.get("urgency", "normal"),
        "notify_human": decision.get("notify_human", False),
        "assessment": decision.get("assessment", ""),
        "notes": decision.get("notes", ""),
    }
    actions_taken = execute_validated_actions(
        actions=decision.get("actions", []),
        decision_context=decision_context,
        sensor_data=sensor_data,
        history=history,
        executor=executor,
        data_dir=data_dir,
        dry_run=dry_run,
        source="scheduled",
    )

    summary["actions_taken"] = actions_taken
    summary["executed"] = any(a["executed"] for a in actions_taken) if actions_taken else False

    # --- 7. Log observations and handle knowledge updates ---
    observations = decision.get("observations", [])
    if observations:
        log_plant_observations(observations, data_dir, source="scheduled_check")
    summary["observations"] = observations

    knowledge_update = decision.get("knowledge_update")
    if knowledge_update:
        append_knowledge_update(knowledge_update, data_dir)
    summary["knowledge_update"] = knowledge_update

    hardware_update = decision.get("hardware_update")
    if hardware_update and isinstance(hardware_update, dict):
        apply_hardware_update(hardware_update, hardware_profile)
    summary["hardware_update"] = hardware_update

    return summary


def execute_validated_actions(
    actions: list[dict[str, Any]],
    decision_context: dict[str, Any],
    sensor_data: SensorData,
    history: list[dict[str, Any]],
    executor: ActionExecutor,
    data_dir: str,
    dry_run: bool,
    source: str = "scheduled",
) -> list[dict[str, Any]]:
    """Validate, execute, and log a list of actions.

    This is the canonical action execution pipeline used by both the
    scheduler and manual Telegram slash commands.

    Args:
        actions: List of action dicts, each with keys: action, params, reason.
        decision_context: Top-level decision metadata with keys:
            urgency, notify_human, assessment, notes.
        sensor_data: Current SensorData (required for safety validation).
        history: Recent decision history (required for rate limit checks).
        executor: Pre-configured ActionExecutor instance.
        data_dir: Path to data directory for logging.
        dry_run: Whether this is a dry-run execution.
        source: Origin label for log records (e.g. "scheduled", "manual_command").

    Returns:
        List of result dicts, each with keys:
            action (str), executed (bool), safety_reason (str|None).
    """
    actions_taken: list[dict] = []

    for act in actions:
        single = {
            "action": act.get("action", "do_nothing"),
            "params": act.get("params", {}),
            "reason": act.get("reason", ""),
            "urgency": decision_context.get("urgency", "normal"),
            "notify_human": decision_context.get("notify_human", False),
            "assessment": decision_context.get("assessment", ""),
            "notes": decision_context.get("notes", ""),
        }

        validation = validate_action(single, sensor_data, history)

        if not validation.valid:
            logger.warning("Safety rejected action %s: %s",
                           single["action"], validation.reason)
            log_decision(sensor_data, single, validation,
                         executed=False, data_dir=data_dir, source=source)
            actions_taken.append({
                "action": single["action"],
                "executed": False,
                "safety_reason": validation.reason,
            })
            continue

        final_action = validation.capped_action or single

        executed = False
        if final_action.get("action") not in ("do_nothing", "notify_human"):
            result = executor.execute(final_action)
            executed = result.success
            if not result.success:
                logger.error("Action execution failed: %s", result.error)
            else:
                logger.info("Action executed: %s", result.command)
                if not dry_run:
                    update_after_action(final_action["action"], data_dir)
        else:
            executed = True

        log_decision(sensor_data, single, validation,
                     executed=executed, data_dir=data_dir, source=source)
        actions_taken.append({
            "action": final_action.get("action", "unknown"),
            "executed": executed,
        })

    return actions_taken


def _apply_fallback_rules(sensor_data: SensorData) -> dict | None:
    """Check offline fallback rules against current sensor data.

    Returns the first matching rule's decision (multi-action format),
    or None if no rules match.
    """
    for name, rule in FALLBACK_RULES.items():
        if rule["condition"](sensor_data):
            return {
                "actions": [{
                    "action": rule["action"]["action"],
                    "params": rule["action"].get("params", {}),
                    "reason": rule["reason"],
                }],
                "urgency": "attention",
                "notify_human": True,
                "assessment": f"Offline mode - fallback rule: {name}",
                "notes": "Claude API was unreachable. Applied conservative fallback.",
            }
    return None


def append_knowledge_update(update_text: str, data_dir: str) -> None:
    """Append a timestamped AI knowledge update to plant_knowledge.md.

    Args:
        update_text: The knowledge text to append.
        data_dir: Path to the data directory.
    """
    knowledge_path = Path(data_dir) / "plant_knowledge.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"\n\n---\n*AI Update ({ts}):* {update_text}\n"
    with open(knowledge_path, "a") as f:
        f.write(entry)
    logger.info("Appended knowledge update to %s", knowledge_path)


def apply_hardware_update(
    updates: dict[str, Any],
    hardware_profile: dict[str, Any],
) -> None:
    """Apply dot-notation updates to the hardware profile and save.

    Args:
        updates: Dict with dot-notation keys like "pump.flow_rate_ml_per_sec".
        hardware_profile: Current hardware profile dict (mutated in place).
    """
    for dotkey, value in updates.items():
        parts = dotkey.split(".")
        target = hardware_profile
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
        logger.info("Hardware profile updated: %s = %s", dotkey, value)

    try:
        save_hardware_profile(hardware_profile)
    except Exception as e:
        logger.error("Failed to save hardware profile: %s", e)


def format_summary_text(summary: dict) -> str:
    """Format a check summary into a concise Telegram message.

    Concise by default: one-line status bar + Claude's message.
    Verbose detail (full sensor dump, action reasons, AI notes) is
    included only when urgency is attention/critical, an action was
    rejected by safety, or an error occurred.

    Args:
        summary: The dict returned by run_check().

    Returns:
        Formatted string suitable for Telegram.
    """
    dec = summary.get("decision")
    sd = summary.get("sensor_data")
    actions_taken = summary.get("actions_taken", [])
    urgency = dec.get("urgency", "normal") if dec else "normal"
    error = summary.get("error")

    verbose = (
        urgency in ("attention", "critical")
        or error
        or any(at.get("safety_reason") for at in actions_taken)
    )

    lines: list[str] = []

    # --- Status bar: one-line snapshot ---
    urgency_icon = {"normal": "🟢", "attention": "🟡", "critical": "🔴"}.get(
        urgency, "⚪"
    )
    status_parts = [urgency_icon]
    if sd:
        status_parts.append(f"{sd['temperature_c']}°C")
        status_parts.append(f"💧{sd['soil_moisture_pct']}%")
        if sd.get("water_tank_ok") is not None:
            tank = "🪣OK" if sd["water_tank_ok"] else "🪣LOW⚠️"
            status_parts.append(tank)
    lines.append(" | ".join(status_parts))

    # --- Actions executed (only if something happened) ---
    executed = [at for at in actions_taken if at.get("executed")]
    if executed:
        parts = []
        for at in executed:
            name = at.get("action", "?")
            params = at.get("params", {})
            dur = params.get("duration_sec")
            parts.append(f"⚡ {name}" + (f" {dur}s" if dur else ""))
        lines.append(" ".join(parts))

    # --- Claude's natural language message ---
    message = dec.get("message", "") if dec else ""
    if message:
        lines.append("")
        lines.append(message)

    # --- Error ---
    if error:
        lines.append(f"\n⚠️ Error: {error}")

    # --- Safety rejections (always shown) ---
    rejected = [at for at in actions_taken if at.get("safety_reason")]
    if rejected:
        for at in rejected:
            lines.append(f"❌ {at.get('action', '?')}: {at['safety_reason']}")

    # --- Verbose sections (only for attention/critical/error) ---
    if verbose:
        if sd:
            lines.append("")
            lines.append("📊 Sensors:")
            lines.append(f"  🌡 Temp: {sd['temperature_c']}°C")
            lines.append(f"  💧 Humidity: {sd['humidity_pct']}%")
            lines.append(f"  🌿 Soil: {sd['soil_moisture_pct']}%")
            lines.append(f"  💨 CO2: {sd['co2_ppm']} ppm")
            lines.append(f"  ☀️ Light: {sd['light_level']}")
            if sd.get("water_tank_ok") is not None:
                tank_str = "OK" if sd["water_tank_ok"] else "LOW ⚠️"
                lines.append(f"  🪣 Water tank: {tank_str}")
            if sd.get("heater_lockout"):
                lines.append("  🔒 Heater lockout: ACTIVE")

        if dec:
            actions = dec.get("actions", [])
            if actions:
                lines.append("")
                for a in actions:
                    lines.append(f"  - {a.get('action', '?')}: {a.get('reason', '')}")
            if dec.get("notes"):
                lines.append(f"  Notes: {dec['notes']}")

        observations = summary.get("observations", [])
        if observations:
            lines.append("")
            lines.append("📝 AI Notes:")
            for obs in observations:
                lines.append(f"  - {obs}")

    return "\n".join(lines)


def main():
    """CLI entry point for manual testing and cron jobs."""
    load_dotenv()

    parser = argparse.ArgumentParser(description="Plant AI Agent - monitoring check")
    parser.add_argument(
        "--once", action="store_true", help="Run a single check and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("AGENT_MODE", "dry-run") == "dry-run",
        help="Log decisions but don't execute hardware commands",
    )
    parser.add_argument(
        "--mock", action="store_true", help="Use mock sensor data (no hardware)"
    )
    parser.add_argument(
        "--no-photo", action="store_true", help="Skip photo capture"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    project_root = str(Path(__file__).resolve().parent.parent)
    farmctl_default = os.path.join(project_root, "farmctl", "farmctl.py")
    farmctl_path = os.getenv("FARMCTL_PATH", farmctl_default)
    data_dir = os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))

    if args.once:
        summary = run_check(
            farmctl_path=farmctl_path,
            data_dir=data_dir,
            dry_run=args.dry_run,
            use_mock=args.mock,
            include_photo=not args.no_photo,
            verbose=args.verbose,
        )
        print(format_summary_text(summary))
        if args.verbose:
            print("\n--- Raw summary ---")
            print(json.dumps(summary, indent=2, default=str))
    else:
        print("Use --once for a single check, or run the Telegram bot instead:")
        print("  python3 -m bot.telegram_bot")
        sys.exit(1)


if __name__ == "__main__":
    main()
