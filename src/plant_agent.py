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

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.action_executor import ActionExecutor
from src.claude_client import get_plant_decision
from src.config_loader import load_plant_profile
from src.logger import (
    load_recent_decisions,
    log_decision,
    log_sensor_reading,
)
from src.plant_knowledge import ensure_plant_knowledge
from src.safety import validate_action, ValidationResult
from src.sensor_reader import SensorData, SensorReadError, read_sensors, read_sensors_mock

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
        "validation": None,
        "executed": False,
        "photo_path": None,
        "error": None,
        "mode": "dry-run" if dry_run else "live",
    }

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

    # --- 2. Optionally capture photo ---
    photo_path = None
    if include_photo and not use_mock:
        executor = ActionExecutor(farmctl_path, dry_run=False)
        photo_path = executor.take_photo(
            os.path.join(data_dir, "plant_latest.jpg")
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

    # --- 4. Ask Claude for decision ---
    decision = None
    try:
        decision = get_plant_decision(
            sensor_data=sensor_data.to_dict(),
            plant_profile=profile,
            plant_knowledge=plant_knowledge,
            history=history,
            photo_path=photo_path,
        )
        logger.info("Claude decision: action=%s reason=%s",
                     decision.get("action"), decision.get("reason"))
    except Exception as e:
        logger.error("Claude API call failed: %s", e)
        # Apply offline fallback rules
        decision = _apply_fallback_rules(sensor_data)
        if decision:
            logger.info("Applying offline fallback: %s", decision.get("reason"))
        else:
            decision = {
                "action": "do_nothing",
                "params": {},
                "reason": f"API unreachable, no fallback triggered: {e}",
                "urgency": "attention",
                "notify_human": True,
                "assessment": "Unable to reach Claude API",
                "notes": str(e),
            }

    summary["decision"] = decision

    # --- 5. Validate via safety module ---
    validation = validate_action(decision, sensor_data, history)
    summary["validation"] = {
        "valid": validation.valid,
        "reason": validation.reason,
        "capped_action": validation.capped_action,
    }

    if not validation.valid:
        logger.warning("Safety rejected action: %s", validation.reason)
        log_decision(sensor_data, decision, validation, executed=False, data_dir=data_dir)
        return summary

    # Use the safety-capped action (e.g. water 60s -> capped to 30s)
    final_action = validation.capped_action or decision

    # --- 6. Execute ---
    executed = False
    if final_action.get("action") not in ("do_nothing", "notify_human"):
        executor = ActionExecutor(farmctl_path, dry_run=dry_run)
        result = executor.execute(final_action)
        executed = result.success
        if not result.success:
            logger.error("Action execution failed: %s", result.error)
            summary["error"] = f"Execution failed: {result.error}"
        else:
            logger.info("Action executed: %s", result.command)
    else:
        executed = True  # do_nothing / notify_human are always "successful"

    summary["executed"] = executed

    # --- 7. Log ---
    log_decision(sensor_data, decision, validation, executed=executed, data_dir=data_dir)

    return summary


def _apply_fallback_rules(sensor_data: SensorData) -> dict | None:
    """Check offline fallback rules against current sensor data.

    Returns the first matching rule's action, or None if no rules match.
    """
    for name, rule in FALLBACK_RULES.items():
        if rule["condition"](sensor_data):
            return {
                "action": rule["action"]["action"],
                "params": rule["action"].get("params", {}),
                "reason": rule["reason"],
                "urgency": "attention",
                "notify_human": True,
                "assessment": f"Offline mode - fallback rule: {name}",
                "notes": "Claude API was unreachable. Applied conservative fallback.",
            }
    return None


def format_summary_text(summary: dict) -> str:
    """Format a check summary into a human-readable Telegram message.

    Args:
        summary: The dict returned by run_check().

    Returns:
        Formatted string suitable for Telegram.
    """
    lines = []
    ts = summary.get("timestamp", "")[:19].replace("T", " ")
    lines.append(f"🌱 Plant Check — {ts}")
    lines.append(f"Mode: {summary.get('mode', 'unknown')}")
    lines.append("")

    sd = summary.get("sensor_data")
    if sd:
        lines.append("📊 Sensors:")
        lines.append(f"  🌡 Temp: {sd['temperature_c']}°C")
        lines.append(f"  💧 Humidity: {sd['humidity_pct']}%")
        lines.append(f"  🌿 Soil: {sd['soil_moisture_pct']}%")
        lines.append(f"  💨 CO2: {sd['co2_ppm']} ppm")
        lines.append(f"  ☀️ Light: {sd['light_level']}")
        lines.append("")

    dec = summary.get("decision")
    if dec:
        action = dec.get("action", "unknown")
        reason = dec.get("reason", "")
        urgency = dec.get("urgency", "normal")
        urgency_icon = {"normal": "🟢", "attention": "🟡", "critical": "🔴"}.get(
            urgency, "⚪"
        )
        lines.append(f"🤖 Decision: {action} {urgency_icon}")
        lines.append(f"  Reason: {reason}")
        if dec.get("notes"):
            lines.append(f"  Notes: {dec['notes']}")
        lines.append("")

    val = summary.get("validation")
    if val:
        if val["valid"]:
            lines.append("✅ Safety: Approved")
        else:
            lines.append(f"❌ Safety: Rejected — {val['reason']}")
        lines.append("")

    if summary.get("executed"):
        lines.append("⚡ Action executed")
    elif summary.get("error"):
        lines.append(f"⚠️ Error: {summary['error']}")
    else:
        lines.append("⏸ Action not executed")

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

    farmctl_path = os.getenv("FARMCTL_PATH", "/home/pi/plant-ops/farmctl.py")
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
