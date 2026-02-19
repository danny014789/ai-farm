"""Telegram command handlers for plant-ops-ai bot.

Each handler is an async function following python-telegram-bot v21
conventions: ``async def handler(update, context)``.

Dependencies (farmctl_path, data_dir, etc.) are read from
``context.bot_data`` which is populated during bot initialization.
This module does NOT import the bot module to avoid circular imports.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import (
    confirm_action_keyboard,
    main_menu_keyboard,
    plant_stage_keyboard,
)
from src.action_executor import ActionExecutor
from src.actuator_state import load_actuator_state, update_after_action
from src.claude_client import get_chat_response
from src.config_loader import load_plant_profile, save_plant_profile
from src.logger import load_recent_decisions, load_recent_plant_log, log_plant_observations
from src.plant_knowledge import ensure_plant_knowledge
from src.safety import validate_action
from src.sensor_reader import SensorData, SensorReadError, read_sensors, read_sensors_mock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization decorator
# ---------------------------------------------------------------------------

def authorized_only(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
    """Decorator to restrict commands to authorized chat IDs.

    The allowed IDs are read from ``context.bot_data["authorized_chat_ids"]``
    (a list of string chat IDs). If the list is empty, all users are allowed
    (useful for initial setup / development).
    """

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Any:
        allowed_ids = context.bot_data.get("authorized_chat_ids", [])
        if allowed_ids and str(update.effective_chat.id) not in allowed_ids:
            await update.message.reply_text("Unauthorized.")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bot_data(context: ContextTypes.DEFAULT_TYPE, key: str) -> Any:
    """Shorthand for context.bot_data.get(key)."""
    return context.bot_data.get(key)


def _farmctl_path(context: ContextTypes.DEFAULT_TYPE) -> str:
    return _bot_data(context, "farmctl_path") or ""


def _data_dir(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return Path(_bot_data(context, "data_dir") or "data")


def _is_dry_run(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return _bot_data(context, "agent_mode") != "live"


def _pause_file(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return _data_dir(context) / ".paused"


def _decisions_path(context: ContextTypes.DEFAULT_TYPE) -> Path:
    return _data_dir(context) / "decisions.jsonl"


TELEGRAM_MAX_LENGTH = 4096


def _split_text(text: str, max_length: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split text into chunks that fit within *max_length*.

    Tries to split at newline boundaries. If a single line exceeds
    *max_length*, falls back to a hard character split.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) <= max_length:
            current = candidate
        else:
            # Flush current chunk if it has content
            if current:
                chunks.append(current)
                current = ""
            # If the single line itself exceeds max_length, hard-split it
            if len(line) > max_length:
                while line:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
            else:
                current = line

    if current:
        chunks.append(current)

    return chunks


async def _send_long_message(message, text: str, **kwargs) -> None:
    """Send *text* via Telegram, splitting into chunks if it exceeds 4096 chars.

    Any extra *kwargs* (e.g. ``reply_markup``) are passed only to the
    **last** chunk so that inline keyboards appear at the end.
    """
    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await message.reply_text(chunk, **kwargs)
        else:
            await message.reply_text(chunk)


def _format_sensor_data(data: SensorData) -> str:
    """Format sensor data with emoji for Telegram readability."""
    lines = [
        f"Temp:  {data.temperature_c:.1f} C",
        f"Humidity:  {data.humidity_pct:.1f}%",
        f"CO2:  {data.co2_ppm} ppm",
        f"Light:  {data.light_level}",
        f"Soil moisture:  {data.soil_moisture_pct:.1f}%",
        f"Timestamp: {data.timestamp}",
    ]
    return "\n".join(lines)


def _load_recent_decisions(
    decisions_path: Path, n: int = 5
) -> list[dict[str, Any]]:
    """Load the last *n* decisions from the JSONL log."""
    if not decisions_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with open(decisions_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries[-n:]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@authorized_only
async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /start - welcome message with overview."""
    text = (
        "Welcome to Plant-Ops AI!\n\n"
        "I monitor your plant and control watering, lighting, heating, "
        "and circulation automatically using AI.\n\n"
        "Quick commands:\n"
        "/status  - Current sensor readings\n"
        "/photo   - Take a plant photo\n"
        "/water   - Manual watering\n"
        "/light   - Light on/off\n"
        "/heater  - Heater on/off\n"
        "/profile - Plant profile & ideal conditions\n"
        "/help    - Full command list\n"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


@authorized_only
async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /help - list all commands."""
    text = (
        "Plant-Ops AI Commands\n"
        "========================\n\n"
        "Monitoring:\n"
        "  /status          - Current sensor readings\n"
        "  /photo           - Take a plant photo\n"
        "  /history [n]     - Last N decisions (default 5)\n"
        "  /profile         - Plant profile + ideal conditions\n\n"
        "Manual control:\n"
        "  /water [sec]     - Water (default 5s, max 30s)\n"
        "  /light on|off    - Light control\n"
        "  /heater on|off   - Heater control\n"
        "  /circulation [s] - Circulation fan (default 60s)\n\n"
        "Configuration:\n"
        "  /setplant <name> - Set plant species\n"
        "  /mode dry-run|live - Switch execution mode\n\n"
        "Automation:\n"
        "  /pause           - Pause scheduled monitoring\n"
        "  /resume          - Resume scheduled monitoring\n"
    )
    await _send_long_message(update.message, text)


@authorized_only
async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /status - read sensors and display current data."""
    try:
        data = read_sensors(_farmctl_path(context))
        text = "Current Sensor Readings\n\n" + _format_sensor_data(data)
    except SensorReadError as exc:
        text = f"Failed to read sensors: {exc}"
    except Exception as exc:
        logger.exception("Unexpected error in /status")
        text = f"Error reading sensors: {exc}"

    await update.message.reply_text(text)


@authorized_only
async def photo_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /photo - take a photo and send it."""
    try:
        executor = ActionExecutor(
            _farmctl_path(context), dry_run=_is_dry_run(context)
        )
        photo_path = "/tmp/plant_photo.jpg"
        result = executor.take_photo(photo_path)

        if result and Path(result).exists():
            await update.message.reply_photo(
                photo=open(result, "rb"),
                caption="Plant photo taken just now.",
            )
        elif _is_dry_run(context):
            await update.message.reply_text(
                "[Dry-run] Photo would be captured to: " + photo_path
            )
        else:
            await update.message.reply_text("Failed to capture photo.")
    except Exception as exc:
        logger.exception("Unexpected error in /photo")
        await update.message.reply_text(f"Error taking photo: {exc}")


@authorized_only
async def water_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /water [sec] - manual watering with confirmation.

    Default 5 seconds, max 30.
    """
    args = context.args or []
    duration = 5

    if args:
        try:
            duration = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: /water [seconds]  (e.g. /water 10)"
            )
            return

    if duration < 1 or duration > 30:
        await update.message.reply_text(
            "Duration must be between 1 and 30 seconds."
        )
        return

    # Store pending action in user_data for the confirmation callback
    context.user_data["pending_action"] = {
        "action": "water",
        "params": {"duration_sec": duration},
    }

    await update.message.reply_text(
        f"Water the plant for {duration} seconds?",
        reply_markup=confirm_action_keyboard(f"water_{duration}"),
    )


@authorized_only
async def light_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /light on|off - manual light control with confirmation."""
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /light on  or  /light off")
        return

    state = args[0].lower()
    action_name = f"light_{state}"

    context.user_data["pending_action"] = {
        "action": action_name,
        "params": {},
    }

    await update.message.reply_text(
        f"Turn light {state.upper()}?",
        reply_markup=confirm_action_keyboard(action_name),
    )


@authorized_only
async def heater_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /heater on|off - manual heater control with confirmation."""
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /heater on  or  /heater off")
        return

    state = args[0].lower()
    action_name = f"heater_{state}"

    context.user_data["pending_action"] = {
        "action": action_name,
        "params": {},
    }

    await update.message.reply_text(
        f"Turn heater {state.upper()}?",
        reply_markup=confirm_action_keyboard(action_name),
    )


@authorized_only
async def circulation_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /circulation [sec] - manual circulation fan."""
    args = context.args or []
    duration = 60

    if args:
        try:
            duration = int(args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: /circulation [seconds]  (e.g. /circulation 120)"
            )
            return

    if duration < 1 or duration > 300:
        await update.message.reply_text(
            "Duration must be between 1 and 300 seconds."
        )
        return

    context.user_data["pending_action"] = {
        "action": "circulation",
        "params": {"duration_sec": duration},
    }

    await update.message.reply_text(
        f"Run circulation fan for {duration} seconds?",
        reply_markup=confirm_action_keyboard(f"circulation_{duration}"),
    )


@authorized_only
async def setplant_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /setplant <name> - set plant species then pick growth stage."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /setplant <plant name>\n"
            "Example: /setplant basil"
        )
        return

    plant_name = " ".join(args)
    context.user_data["pending_plant_name"] = plant_name

    await update.message.reply_text(
        f"Setting plant to: {plant_name}\n\n"
        "Select the current growth stage:",
        reply_markup=plant_stage_keyboard(),
    )


@authorized_only
async def profile_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /profile - show current plant profile and ideal conditions."""
    try:
        profile = load_plant_profile()
        plant = profile.get("plant", {})
        ideal = profile.get("ideal_conditions", {})
        cached = profile.get("knowledge_cached", False)

        name = plant.get("name") or "(not set)"
        stage = plant.get("growth_stage", "unknown")

        lines = [
            "Plant Profile",
            f"  Name: {name}",
            f"  Stage: {stage}",
            f"  Planted: {plant.get('planted_date') or 'N/A'}",
        ]

        if plant.get("notes"):
            lines.append(f"  Notes: {plant['notes']}")

        lines.append("")
        lines.append("Ideal Conditions")
        lines.append(
            f"  Temp: {ideal.get('temp_min_c', '?')}"
            f" - {ideal.get('temp_max_c', '?')} C"
        )
        lines.append(
            f"  Humidity: {ideal.get('humidity_min_pct', '?')}"
            f" - {ideal.get('humidity_max_pct', '?')}%"
        )
        lines.append(
            f"  Soil: {ideal.get('soil_moisture_min_pct', '?')}"
            f" - {ideal.get('soil_moisture_max_pct', '?')}%"
        )
        lines.append(
            f"  Light: {ideal.get('light_hours', '?')} hours/day"
        )
        lines.append(
            f"  CO2 min: {ideal.get('co2_min_ppm', '?')} ppm"
        )
        lines.append(
            f"\nKnowledge cached: {'yes' if cached else 'no'}"
        )

        # Show knowledge file excerpt if it exists
        knowledge_path = _data_dir(context) / "plant_knowledge.md"
        if knowledge_path.exists():
            content = knowledge_path.read_text()
            # Show first 500 chars as preview
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"\nResearch notes:\n{content}")

        await _send_long_message(update.message, "\n".join(lines))

    except Exception as exc:
        logger.exception("Error loading profile")
        await update.message.reply_text(f"Error loading profile: {exc}")


@authorized_only
async def history_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /history [n] - show last N decisions."""
    args = context.args or []
    count = 5

    if args:
        try:
            count = int(args[0])
        except ValueError:
            await update.message.reply_text("Usage: /history [n]")
            return

    count = max(1, min(count, 50))
    decisions = _load_recent_decisions(_decisions_path(context), n=count)

    if not decisions:
        await update.message.reply_text("No decision history yet.")
        return

    lines = [f"Last {len(decisions)} decisions:\n"]
    for i, d in enumerate(reversed(decisions), 1):
        ts = d.get("timestamp", d.get("executed_at", "?"))
        action = d.get("action", "?")
        reason = d.get("reason", "")
        mode = "[dry-run] " if d.get("dry_run") else ""
        line = f"{i}. {mode}{action} @ {ts}"
        if reason:
            line += f"\n   {reason}"
        lines.append(line)

    await _send_long_message(update.message, "\n".join(lines))


@authorized_only
async def pause_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /pause - pause automated monitoring."""
    pause = _pause_file(context)
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(datetime.now(timezone.utc).isoformat())
    await update.message.reply_text(
        "Automated monitoring PAUSED.\n"
        "The bot will not run scheduled checks until you /resume."
    )


@authorized_only
async def resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /resume - resume automated monitoring."""
    pause = _pause_file(context)
    if pause.exists():
        pause.unlink()
        await update.message.reply_text(
            "Automated monitoring RESUMED.\n"
            "Scheduled checks are active again."
        )
    else:
        await update.message.reply_text("Monitoring is already active.")


@authorized_only
async def mode_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /mode dry-run|live - switch execution mode."""
    args = context.args or []
    if not args or args[0].lower() not in ("dry-run", "live"):
        current = context.bot_data.get("agent_mode", "dry-run")
        await update.message.reply_text(
            f"Current mode: {current}\n\n"
            "Usage: /mode dry-run  or  /mode live"
        )
        return

    new_mode = args[0].lower()
    context.bot_data["agent_mode"] = new_mode
    await update.message.reply_text(f"Mode switched to: {new_mode}")


# ---------------------------------------------------------------------------
# Callback query handlers
# ---------------------------------------------------------------------------

async def confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline keyboard confirmation/cancellation callbacks.

    Processes callback_data in the format:
    - ``confirm:<action>`` - execute the pending action
    - ``cancel:<action>`` - cancel the pending action
    - ``stage:<stage>`` - set the growth stage (from /setplant flow)
    - ``menu:<action>`` - main menu quick actions
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    # --- Main menu shortcuts -----------------------------------------------
    if data.startswith("menu:"):
        menu_action = data.split(":", 1)[1]
        if menu_action == "status":
            await _inline_status(query, context)
        elif menu_action == "photo":
            await query.edit_message_text("Use /photo to take a plant photo.")
        elif menu_action == "history":
            await _inline_history(query, context)
        elif menu_action == "profile":
            await query.edit_message_text("Use /profile to view plant details.")
        return

    # --- Growth stage selection (from /setplant) ---------------------------
    if data.startswith("stage:"):
        stage = data.split(":", 1)[1]
        plant_name = context.user_data.pop("pending_plant_name", None)
        if not plant_name:
            await query.edit_message_text("No plant name set. Use /setplant first.")
            return

        try:
            profile = load_plant_profile()
            profile.setdefault("plant", {})["name"] = plant_name
            profile["plant"]["growth_stage"] = stage
            profile["plant"]["planted_date"] = (
                profile["plant"].get("planted_date")
                or datetime.now().strftime("%Y-%m-%d")
            )
            profile["knowledge_cached"] = False
            save_plant_profile(profile)

            await query.edit_message_text(
                f"Plant set to: {plant_name} ({stage})\n\n"
                "Researching optimal growing conditions... "
                "This may take ~30 seconds."
            )

            # Trigger async research via Claude API
            await _research_plant(query, context, plant_name, stage)

        except Exception as exc:
            logger.exception("Error in setplant stage callback")
            await query.edit_message_text(f"Error setting plant: {exc}")
        return

    # --- Action confirmation / cancellation --------------------------------
    if data.startswith("cancel:"):
        context.user_data.pop("pending_action", None)
        await query.edit_message_text("Action cancelled.")
        return

    if data.startswith("confirm:"):
        pending = context.user_data.pop("pending_action", None)
        if not pending:
            await query.edit_message_text("No pending action found.")
            return

        await _execute_pending_action(query, context, pending)
        return

    # Fallback for unknown callback data
    await query.edit_message_text(f"Unknown action: {data}")


async def _inline_status(query: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline status button from main menu."""
    try:
        data = read_sensors(_farmctl_path(context))
        text = "Current Sensor Readings\n\n" + _format_sensor_data(data)
    except SensorReadError as exc:
        text = f"Failed to read sensors: {exc}"
    except Exception as exc:
        text = f"Error: {exc}"
    await query.edit_message_text(text)


async def _inline_history(query: Any, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline history button from main menu."""
    decisions = _load_recent_decisions(_decisions_path(context), n=5)
    if not decisions:
        await query.edit_message_text("No decision history yet.")
        return

    lines = ["Last 5 decisions:\n"]
    for i, d in enumerate(reversed(decisions), 1):
        ts = d.get("timestamp", d.get("executed_at", "?"))
        action = d.get("action", "?")
        lines.append(f"{i}. {action} @ {ts}")
    await query.edit_message_text("\n".join(lines))


async def _execute_pending_action(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict[str, Any],
) -> None:
    """Execute a confirmed manual action and report the result."""
    try:
        executor = ActionExecutor(
            _farmctl_path(context), dry_run=_is_dry_run(context)
        )
        result = executor.execute(pending)

        if result.success:
            mode_tag = " [dry-run]" if result.dry_run else ""
            await query.edit_message_text(
                f"Action executed{mode_tag}: {result.action}\n"
                f"{result.output}"
            )
        else:
            await query.edit_message_text(
                f"Action failed: {result.action}\n"
                f"Error: {result.error}"
            )

        # Log the manual action
        _log_decision(
            _decisions_path(context),
            action=result.action,
            reason="Manual command via Telegram",
            dry_run=result.dry_run,
            success=result.success,
        )

    except Exception as exc:
        logger.exception("Error executing confirmed action")
        await query.edit_message_text(f"Error executing action: {exc}")


async def _research_plant(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    plant_name: str,
    stage: str,
) -> None:
    """Use the Anthropic API to research optimal conditions for a plant.

    Results are saved to data/plant_knowledge.md and ideal conditions
    are updated in plant_profile.yaml.
    """
    api_key = _bot_data(context, "anthropic_api_key")
    if not api_key:
        await query.message.reply_text(
            "ANTHROPIC_API_KEY not configured. Cannot research plant."
        )
        return

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        prompt = (
            f"I am growing {plant_name} (currently in the {stage} stage) "
            f"indoors with artificial lighting, temperature control, and "
            f"automated watering.\n\n"
            f"Please provide:\n"
            f"1. Ideal temperature range (min/max in Celsius)\n"
            f"2. Ideal humidity range (min/max percentage)\n"
            f"3. Ideal soil moisture range (min/max percentage)\n"
            f"4. Recommended light hours per day for {stage} stage\n"
            f"5. Minimum CO2 ppm\n"
            f"6. Key care tips for {stage} stage\n"
            f"7. Common problems to watch for\n\n"
            f"Format the numerical values as JSON on a single line at the "
            f"end, like:\n"
            f'IDEAL_JSON: {{"temp_min_c": 20, "temp_max_c": 28, '
            f'"humidity_min_pct": 50, "humidity_max_pct": 70, '
            f'"soil_moisture_min_pct": 40, "soil_moisture_max_pct": 60, '
            f'"light_hours": 16, "co2_min_ppm": 400}}'
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        content = response.content[0].text

        # Save full research as knowledge file
        knowledge_path = _data_dir(context) / "plant_knowledge.md"
        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge_path.write_text(
            f"# {plant_name} - {stage} stage\n\n"
            f"Researched: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{content}\n"
        )

        # Try to extract and save ideal conditions JSON
        if "IDEAL_JSON:" in content:
            json_str = content.split("IDEAL_JSON:", 1)[1].strip()
            # Handle case where there's text after the JSON
            if "\n" in json_str:
                json_str = json_str.split("\n", 1)[0]
            try:
                ideal = json.loads(json_str)
                profile = load_plant_profile()
                profile["ideal_conditions"] = ideal
                profile["knowledge_cached"] = True
                save_plant_profile(profile)
            except json.JSONDecodeError:
                logger.warning("Could not parse ideal conditions JSON")

        await query.message.reply_text(
            f"Research complete for {plant_name} ({stage}).\n"
            f"Use /profile to see the updated conditions."
        )

    except Exception as exc:
        logger.exception("Plant research failed")
        await query.message.reply_text(
            f"Research failed: {exc}\n"
            f"The plant name and stage have been saved. "
            f"You can manually edit config/plant_profile.yaml."
        )


@authorized_only
async def chat_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle free-text messages — natural language chat with the AI."""
    user_message = update.message.text
    if not user_message:
        return

    await update.message.chat.send_action("typing")

    # Read sensors (fallback to mock on error)
    try:
        sensor_data = read_sensors(_farmctl_path(context))
    except (SensorReadError, Exception) as exc:
        logger.warning("Sensor read failed in chat, using mock: %s", exc)
        sensor_data = read_sensors_mock()

    # Load context
    data_dir = str(_data_dir(context))
    profile = load_plant_profile()
    history = load_recent_decisions(10, data_dir)
    plant_log = load_recent_plant_log(20, data_dir)
    actuator_state = load_actuator_state(data_dir)
    plant_knowledge = ensure_plant_knowledge(
        profile, _bot_data(context, "anthropic_api_key") or ""
    )

    # Get AI response
    try:
        response = get_chat_response(
            user_message=user_message,
            sensor_data=sensor_data.to_dict(),
            plant_profile=profile,
            plant_knowledge=plant_knowledge,
            history=history,
            actuator_state=actuator_state,
            plant_log=plant_log,
        )
    except Exception as exc:
        logger.exception("Chat AI call failed")
        await update.message.reply_text(f"Sorry, I had trouble thinking: {exc}")
        return

    # Execute any actions the AI wants to take
    actions = response.get("actions", [])
    action_results: list[str] = []
    executor = ActionExecutor(
        _farmctl_path(context), dry_run=_is_dry_run(context)
    )

    for action_spec in actions:
        action_name = action_spec.get("action", "")
        params = action_spec.get("params", {})
        reason = action_spec.get("reason", "")

        if not validate_action({"action": action_name, "params": params}):
            action_results.append(f"Rejected: {action_name} (failed safety check)")
            continue

        try:
            result = executor.execute({"action": action_name, "params": params})
            if result.success:
                mode_tag = " [dry-run]" if result.dry_run else ""
                action_results.append(f"Executed{mode_tag}: {action_name}")
                update_after_action(action_name, params, data_dir)
            else:
                action_results.append(f"Failed: {action_name} - {result.error}")

            _log_decision(
                _decisions_path(context),
                action=action_name,
                reason=reason or "Chat request",
                dry_run=result.dry_run,
                success=result.success,
            )
        except Exception as exc:
            logger.exception("Action execution failed in chat: %s", action_name)
            action_results.append(f"Error: {action_name} - {exc}")

    # Log observations
    observations = response.get("observations", [])
    if observations:
        log_plant_observations(observations, data_dir, source="chat")

    # Build reply
    reply = response.get("message", "")
    if action_results:
        reply += "\n\n" + "\n".join(action_results)

    if reply.strip():
        await _send_long_message(update.message, reply)
    else:
        await update.message.reply_text("I'm not sure what to say about that.")


def _log_decision(
    decisions_path: Path,
    action: str,
    reason: str,
    dry_run: bool,
    success: bool,
) -> None:
    """Append a decision entry to the JSONL log file."""
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "reason": reason,
        "dry_run": dry_run,
        "success": success,
        "source": "telegram_manual",
    }
    with open(decisions_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
