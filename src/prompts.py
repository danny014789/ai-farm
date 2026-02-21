"""System and user prompt templates for Claude API calls.

Builds structured prompts that instruct Claude to act as a plant care expert,
analyze sensor data and optional photos, and return actionable JSON decisions.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Available actions and their constraints (referenced in the system prompt)
# ---------------------------------------------------------------------------
VALID_ACTIONS = (
    "water",
    "light_on",
    "light_off",
    "heater_on",
    "heater_off",
    "circulation",
    "do_nothing",
)

VALID_URGENCIES = ("normal", "attention", "critical")

# ---------------------------------------------------------------------------
# JSON schema description embedded in the system prompt so Claude knows
# exactly what structure to return.
# ---------------------------------------------------------------------------
_RESPONSE_SCHEMA = """\
{
  "assessment": "Brief plant health assessment (1-2 sentences)",
  "actions": [
    {
      "action": "water|light_on|light_off|heater_on|heater_off|circulation|do_nothing",
      "params": {"duration_sec": <int, required for water and circulation, omit for others>},
      "reason": "Why this specific action is needed"
    }
  ],
  "urgency": "normal|attention|critical",
  "notify_human": <true|false>,
  "notes": "Any additional observations, concerns, or recommendations",
  "message": "A natural, conversational summary for the human caretaker. 2-4 sentences.",
  "observations": ["noteworthy observations to remember for future checks"],
  "knowledge_update": "significant learning to append to knowledge doc, or null",
  "hardware_update": {"section.key": "new_value"} or null
}"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(
    plant_profile: dict[str, Any],
    plant_knowledge: str,
    hardware_profile: dict[str, Any] | None = None,
) -> str:
    """Build the system prompt that defines Claude's role and constraints.

    Args:
        plant_profile: Parsed plant_profile.yaml dict. Expected keys include
            ``plant`` (name, variety, growth_stage, planted_date, notes) and
            ``ideal_conditions``.
        plant_knowledge: Cached markdown document with researched growing
            conditions for the current plant. May be empty if not yet cached.
        hardware_profile: Parsed hardware_profile.yaml dict describing pump,
            pot, light, heater, fan, and sensor calibration.

    Returns:
        A complete system prompt string.
    """
    plant = plant_profile.get("plant", {})
    ideal = plant_profile.get("ideal_conditions", {})

    plant_name = plant.get("name") or "unknown plant"
    variety = plant.get("variety") or ""
    growth_stage = plant.get("growth_stage") or "unknown"
    planted_date = plant.get("planted_date") or "unknown"
    notes = plant.get("notes") or ""

    variety_label = f" ({variety})" if variety else ""

    # Format ideal conditions as a readable block
    ideal_block = _format_ideal_conditions(ideal)

    # Optionally include plant knowledge research
    knowledge_section = ""
    if plant_knowledge and plant_knowledge.strip():
        knowledge_section = (
            "\n\n## Researched Plant Knowledge\n"
            "The following information was researched specifically for this plant. "
            "Use it as your primary reference for care decisions.\n\n"
            f"{plant_knowledge.strip()}\n"
        )

    # Hardware profile section
    hardware_section = ""
    if hardware_profile:
        hw_block = _format_hardware_profile(hardware_profile)
        pump = hardware_profile.get("pump", {})
        flow = pump.get("flow_rate_ml_per_sec")
        pot = hardware_profile.get("pot", {})
        vol = pot.get("volume_liters")
        hardware_section = (
            "\n\n## Hardware Setup\n"
            f"{hw_block}\n"
        )
        if flow and vol:
            hardware_section += (
                f"\nThis means 1 second of watering delivers ~{flow}ml into a "
                f"{vol}L pot. Use this to choose appropriate watering durations.\n"
            )

    return f"""\
You are a plant care expert AI agent responsible for a single plant growing in a controlled indoor environment on a Raspberry Pi automation system.

## Your Plant
- Species: {plant_name}{variety_label}
- Growth stage: {growth_stage}
- Planted: {planted_date}
{"- Notes: " + notes if notes else ""}

## Ideal Growing Conditions
{ideal_block}
{knowledge_section}{hardware_section}
## Available Actions
You may recommend one or more actions per evaluation. Choose from:

| Action        | Description                        | Parameters               |
|---------------|------------------------------------|--------------------------|
| water         | Activate water pump                | duration_sec (1-30)      |
| light_on      | Turn grow light on                 | none                     |
| light_off     | Turn grow light off                | none                     |
| heater_on     | Turn heater on                     | none                     |
| heater_off    | Turn heater off                    | none                     |
| circulation   | Run circulation fan                | duration_sec (10-300)    |
| do_nothing    | No action needed right now         | none                     |

## Action Constraints
- Water: duration_sec must be between 1 and 30. Minimum 60 minutes between waterings. Do NOT water if the water tank level is LOW — notify the human to refill instead.
- Circulation: duration_sec must be between 10 and 300.
- Heater: Never turn on if temperature is already above {ideal.get("temp_max_c", 28)}C. Never leave on if above {ideal.get("temp_max_c", 28)}C. If heater_lockout is active, the firmware has disabled the heater for safety — do not attempt to turn it on.
- Light: Respect the plant's light schedule. Do NOT turn on lights between midnight and 5am unless the plant is severely light-deprived. Maximum {ideal.get("light_hours", 14)} hours per day.
- When in doubt, choose "do_nothing" and set "notify_human" to true.

## Decision Guidelines
1. Be CONSERVATIVE. Overwatering and overheating are worse than brief underwatering or mild cold.
2. Consider the TIME OF DAY. Plants have natural day/night cycles. Avoid unnecessary light or heater activation at night.
3. Check recent decision history to avoid repeating actions too frequently (e.g., do not water again if watered recently).
4. If sensor readings look abnormal or contradictory, choose "do_nothing" and set "notify_human" to true with a note explaining the anomaly.
5. If a photo is provided, examine it for signs of stress, pests, disease, wilting, discoloration, or other visual issues.
6. Prioritize: critical safety > plant health > optimal growth > energy efficiency.
7. If the water tank level is LOW, mention it in your message so the human knows to refill.
8. The "Current Actuator States" section reflects the actual hardware relay states. Use it to know what is currently on or off — do not guess from sensor values alone.

## Operational Memory
You have a plant log where past observations are recorded. Use it to track patterns \
(watering effectiveness, drying rates), note growth milestones, record calibration \
insights. Write observations in the "observations" array. Use "knowledge_update" for \
significant discoveries worth adding to the knowledge document.

## Hardware Profile Updates
You can update the hardware profile (pump flow rate, pot size, sensor calibration, etc.) \
by including a "hardware_update" dict in your response. Use dot-notation keys like \
"pump.flow_rate_ml_per_sec" or "sensors.soil_raw_wet". Do this when you observe that \
the configured values don't match reality (e.g. watering 5s raised soil moisture more \
than expected given the configured flow rate).

## Human Communication
The "message" field is sent to the plant owner via Telegram. Write a brief, friendly \
status update with context: comparison with recent readings, effect of recent actions, \
growth progress. Keep under 500 characters.

## Response Format
Respond with ONLY a valid JSON object. No markdown fences, no extra text, no explanation outside the JSON.

{_RESPONSE_SCHEMA}

Always use the "actions" array, even for a single action or no action (use an empty array or a single-element array).
Order actions by priority (most important first).

Do NOT include any text before or after the JSON object."""


def build_user_prompt(
    sensor_data: dict[str, Any],
    history: list[dict[str, Any]],
    current_time: str,
    photo_path: str | None = None,
    actuator_state: dict[str, str] | None = None,
    plant_log: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the user message content blocks for a decision request.

    Returns a list of content blocks suitable for the Anthropic messages API.
    The list always contains a text block with sensor data and history, and
    optionally an image block if a photo path is provided.

    Args:
        sensor_data: Current sensor readings dict. Expected keys:
            temp_c, humidity_pct, co2_ppm, light_level, soil_moisture_pct.
        history: List of recent decision dicts (most recent first, up to 10).
        current_time: Human-readable current date/time string
            (e.g., "2026-02-18 14:30:00").
        photo_path: Optional path to a plant photo (JPEG/PNG).
        actuator_state: Optional dict of current actuator states
            (e.g. {"light": "on", "heater": "off", ...}).
        plant_log: Optional list of recent plant observation dicts.

    Returns:
        List of content block dicts for the messages API.
    """
    content_blocks: list[dict[str, Any]] = []

    # --- Sensor data text block ---
    sensor_text = _format_sensor_data(sensor_data)
    history_text = _format_history(history)
    actuator_text = _format_actuator_state(actuator_state) if actuator_state else ""

    actuator_section = ""
    if actuator_text:
        actuator_section = f"## Current Actuator States\n{actuator_text}\n\n"

    plant_log_section = ""
    if plant_log:
        plant_log_section = (
            f"## Your Previous Observations\n{_format_plant_log(plant_log)}\n\n"
        )

    text_block = (
        f"## Current Time\n{current_time}\n\n"
        f"## Current Sensor Readings\n{sensor_text}\n\n"
        f"{actuator_section}"
        f"{plant_log_section}"
        f"## Recent Decision History (last {len(history)} decisions)\n{history_text}\n\n"
        "Analyze the current plant status and return your JSON decision."
    )
    content_blocks.append({"type": "text", "text": text_block})

    # --- Optional image block ---
    if photo_path:
        image_block = _build_image_block(photo_path)
        if image_block is not None:
            content_blocks.append(image_block)
            content_blocks.append(
                {
                    "type": "text",
                    "text": (
                        "A photo of the plant is attached above. "
                        "Note: the photo was taken with the grow light temporarily "
                        "switched on for illumination — the grow light's actual "
                        "current state is shown in 'Current Actuator States' above, "
                        "not inferred from the photo brightness. "
                        "Examine the photo for visual signs of stress, pests, disease, "
                        "wilting, or discoloration and factor your observations "
                        "into the decision."
                    ),
                }
            )

    return content_blocks


def build_research_prompt(
    plant_name: str, variety: str, growth_stage: str
) -> str:
    """Build a prompt for one-time plant research.

    This prompt asks Claude to provide comprehensive growing conditions
    and care information for a specific plant, which will be cached
    locally as a markdown reference document.

    Args:
        plant_name: Common name of the plant (e.g., "basil").
        variety: Specific variety (e.g., "Genovese"). May be empty.
        growth_stage: Current growth stage (seedling/vegetative/flowering/fruiting).

    Returns:
        A user prompt string for the research request.
    """
    variety_label = f" ({variety})" if variety else ""

    return f"""\
I am setting up an automated indoor growing system for **{plant_name}{variety_label}**, \
currently in the **{growth_stage}** stage.

Please provide a comprehensive growing guide in Markdown format covering ALL of the following:

## Required Sections

### 1. Ideal Temperature
- Optimal day and night temperature ranges in Celsius
- Minimum and maximum survival temperatures
- Temperature preferences by growth stage (seedling, vegetative, flowering, fruiting)

### 2. Ideal Humidity
- Optimal relative humidity range by growth stage
- Signs of too-high or too-low humidity

### 3. Soil Moisture
- Optimal soil moisture percentage range
- Watering frequency recommendations by growth stage
- Signs of overwatering vs underwatering
- Preferred watering technique (volume, timing)

### 4. Light Requirements
- Optimal daily light hours by growth stage
- Light intensity preferences (if relevant)
- Recommended light-on/light-off schedule for indoor growing

### 5. CO2 Levels
- Optimal CO2 ppm range
- Whether supplemental CO2 is beneficial for this plant

### 6. Air Circulation
- Importance of airflow for this plant
- Recommended ventilation schedule

### 7. Common Issues
- Most common pests for this plant (and visual signs)
- Most common diseases (and visual signs)
- Nutrient deficiency symptoms
- Environmental stress indicators

### 8. Growth Stage Tips
- Specific care tips for the **{growth_stage}** stage
- Expected timeline to next growth stage
- Key milestones to watch for

### 9. Summary Table
Provide a summary table of ideal numeric ranges:

| Parameter         | Min  | Optimal | Max  | Unit |
|-------------------|------|---------|------|------|
| Temperature (day) |      |         |      | C    |
| Temperature (night)|     |         |      | C    |
| Humidity          |      |         |      | %    |
| Soil moisture     |      |         |      | %    |
| Light hours       |      |         |      | h    |
| CO2               |      |         |      | ppm  |

Be specific and practical. This information will be used by an AI agent to make \
real-time automated care decisions, so numeric ranges are important."""


# ---------------------------------------------------------------------------
# Chat-mode prompts (natural language Telegram conversation)
# ---------------------------------------------------------------------------

_CHAT_RESPONSE_SCHEMA = """\
{
  "message": "Your natural language response to the user.",
  "observations": ["Optional: noteworthy observations worth logging for future reference"],
  "knowledge_update": "Significant new learning about this plant to remember, or null",
  "hardware_update": {"section.key": "new_value"} or null
}"""


def build_chat_system_prompt(
    plant_profile: dict[str, Any],
    plant_knowledge: str,
    hardware_profile: dict[str, Any] | None = None,
) -> str:
    """Build the system prompt for conversational chat mode.

    Similar to the scheduled check prompt but instructs Claude to respond
    conversationally rather than with a pure JSON assessment.

    Args:
        plant_profile: Parsed plant_profile.yaml dict.
        plant_knowledge: Cached plant knowledge markdown string.
        hardware_profile: Parsed hardware_profile.yaml dict.

    Returns:
        System prompt string for chat mode.
    """
    plant = plant_profile.get("plant", {})
    ideal = plant_profile.get("ideal_conditions", {})

    plant_name = plant.get("name") or "unknown plant"
    variety = plant.get("variety") or ""
    growth_stage = plant.get("growth_stage") or "unknown"
    planted_date = plant.get("planted_date") or "unknown"
    notes = plant.get("notes") or ""

    variety_label = f" ({variety})" if variety else ""
    ideal_block = _format_ideal_conditions(ideal)

    knowledge_section = ""
    if plant_knowledge and plant_knowledge.strip():
        knowledge_section = (
            "\n\n## Researched Plant Knowledge\n"
            f"{plant_knowledge.strip()}\n"
        )

    hardware_section = ""
    if hardware_profile:
        hw_block = _format_hardware_profile(hardware_profile)
        pump = hardware_profile.get("pump", {})
        flow = pump.get("flow_rate_ml_per_sec")
        pot = hardware_profile.get("pot", {})
        vol = pot.get("volume_liters")
        hardware_section = (
            "\n\n## Hardware Setup\n"
            f"{hw_block}\n"
        )
        if flow and vol:
            hardware_section += (
                f"\n1 second of watering ≈ {flow}ml into a {vol}L pot.\n"
            )

    return f"""\
You are a friendly, knowledgeable plant care AI assistant. You are responsible for \
a single plant in a controlled indoor environment on a Raspberry Pi automation system.

You are having a conversation with the plant's owner via Telegram. Respond naturally \
and helpfully.

## Your Plant
- Species: {plant_name}{variety_label}
- Growth stage: {growth_stage}
- Planted: {planted_date}
{"- Notes: " + notes if notes else ""}

## Ideal Growing Conditions
{ideal_block}
{knowledge_section}{hardware_section}
## Important: No Hardware Control
You CANNOT directly control hardware. You cannot water, toggle lights, turn the heater \
on/off, or run the circulation fan. If the user asks you to perform a hardware action, \
tell them to use the Telegram slash commands instead: /water, /light, /heater, /circulation.

## What You CAN Do
- Answer questions about the plant's health, sensor data, and conditions
- Provide care advice and recommendations
- Log observations for future reference (via the "observations" array)
- Record significant learnings about this plant (via "knowledge_update")
- Update the hardware profile when the user provides hardware specs (via "hardware_update")

## Guidelines
1. Be conversational and natural. You are chatting with the plant owner, not generating a report.
2. Reference the current sensor data, recent history, and your plant log observations when relevant.
3. If the user asks you to do something physical (water, lights, heater, fan), direct them to \
the appropriate slash command (/water, /light, /heater, /circulation).
4. You can log observations about the conversation in the "observations" array.
5. The user can tell you about their hardware (e.g. "the pump does about 20ml/sec", "I have a 3L pot"). \
When they do, include a "hardware_update" dict with dot-notation keys (e.g. "pump.flow_rate_ml_per_sec": 20). \
Set to null when no update is needed.
6. The "Current Actuator States" section reflects the actual hardware relay states. Use it to accurately answer questions about what is on or off.
7. If the water tank is LOW, proactively mention it so the user knows to refill.
8. If you learn something significant about this specific plant, include it in "knowledge_update". Set to null otherwise.

## Response Format
Respond with ONLY a valid JSON object:

{_CHAT_RESPONSE_SCHEMA}

Do NOT include any text before or after the JSON object."""


def build_chat_user_prompt(
    user_message: str,
    sensor_data: dict[str, Any],
    history: list[dict[str, Any]],
    current_time: str,
    actuator_state: dict[str, str] | None = None,
    plant_log: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the user message for a chat interaction.

    Args:
        user_message: The user's free-text message from Telegram.
        sensor_data: Current sensor readings dict.
        history: Recent decision history.
        current_time: Current timestamp string.
        actuator_state: Current actuator states.
        plant_log: Recent plant log entries.

    Returns:
        List of content block dicts for the messages API.
    """
    sensor_text = _format_sensor_data(sensor_data)
    history_text = _format_history(history)

    context_parts = [
        f"## Current Time\n{current_time}",
        f"## Current Sensor Readings\n{sensor_text}",
    ]

    if actuator_state:
        actuator_text = _format_actuator_state(actuator_state)
        context_parts.append(f"## Current Actuator States\n{actuator_text}")

    if plant_log:
        plant_log_text = _format_plant_log(plant_log)
        context_parts.append(f"## Your Previous Observations\n{plant_log_text}")

    context_parts.append(
        f"## Recent Decision History (last {len(history)} decisions)\n{history_text}"
    )

    context = "\n\n".join(context_parts)

    text_block = (
        f"{context}\n\n"
        f"---\n\n"
        f"## User Message\n{user_message}\n\n"
        f"Respond to the user's message with a JSON object."
    )

    return [{"type": "text", "text": text_block}]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_hardware_profile(hw: dict[str, Any]) -> str:
    """Format hardware profile dict into a readable text block."""
    lines: list[str] = []

    pump = hw.get("pump", {})
    if pump:
        lines.append(f"- Water pump: {pump.get('type', 'unknown')}, "
                      f"~{pump.get('flow_rate_ml_per_sec', '?')} ml/sec")

    pot = hw.get("pot", {})
    if pot:
        parts = [f"{pot.get('volume_liters', '?')}L {pot.get('material', '')}"]
        if pot.get("has_drainage"):
            parts.append("with drainage")
        lines.append(f"- Pot: {' '.join(parts)}")

    light = hw.get("grow_light", {})
    if light:
        lines.append(f"- Grow light: {light.get('type', '?')} "
                      f"{light.get('wattage', '?')}W, "
                      f"{light.get('height_cm', '?')}cm from canopy")

    heater = hw.get("heater", {})
    if heater:
        lines.append(f"- Heater: {heater.get('type', '?')} "
                      f"{heater.get('wattage', '?')}W")

    fan = hw.get("circulation_fan", {})
    if fan:
        lines.append(f"- Circulation fan: {fan.get('type', '?')}")

    return "\n".join(lines) if lines else "No hardware profile configured."


def _format_ideal_conditions(ideal: dict[str, Any]) -> str:
    """Format ideal conditions dict into a readable text block."""
    lines = [
        f"- Temperature: {ideal.get('temp_min_c', '?')}C - {ideal.get('temp_max_c', '?')}C",
        f"- Humidity: {ideal.get('humidity_min_pct', '?')}% - {ideal.get('humidity_max_pct', '?')}%",
        f"- Soil moisture: {ideal.get('soil_moisture_min_pct', '?')}% - {ideal.get('soil_moisture_max_pct', '?')}%",
        f"- Target light hours: {ideal.get('light_hours', '?')} hours/day",
        f"- Minimum CO2: {ideal.get('co2_min_ppm', '?')} ppm",
    ]
    return "\n".join(lines)


def _format_sensor_data(sensor_data: dict[str, Any]) -> str:
    """Format sensor readings into a human-readable text block."""
    lines = [
        f"- Temperature: {sensor_data.get('temperature_c', 'N/A')}C",
        f"- Humidity: {sensor_data.get('humidity_pct', 'N/A')}%",
        f"- CO2: {sensor_data.get('co2_ppm', 'N/A')} ppm",
        f"- Light level: {sensor_data.get('light_level', 'N/A')}",
        f"- Soil moisture: {sensor_data.get('soil_moisture_pct', 'N/A')}%",
    ]
    tank = sensor_data.get("water_tank_ok")
    if tank is not None:
        lines.append(f"- Water tank: {'OK' if tank else 'LOW - needs refill'}")
    return "\n".join(lines)


def _format_actuator_state(state: dict[str, str]) -> str:
    """Format actuator state dict into a readable text block."""
    labels = {
        "light": "Grow light",
        "heater": "Heater",
        "pump": "Water pump",
        "circulation": "Circulation fan",
        "water_tank": "Water tank level",
        "heater_lockout": "Heater safety lockout",
    }
    lines = [f"- {labels.get(k, k)}: {v}" for k, v in state.items()]
    return "\n".join(lines)


def _format_plant_log(log: list[dict[str, Any]]) -> str:
    """Format plant log entries into a readable block.

    Args:
        log: List of plant log dicts with timestamp and observation keys.

    Returns:
        Formatted plant log string, or a note if empty.
    """
    if not log:
        return "No previous observations recorded yet."

    lines: list[str] = []
    for entry in log:
        ts = entry.get("timestamp", "?")[:19]
        obs = entry.get("observation", "")
        lines.append(f"- [{ts}] {obs}")
    return "\n".join(lines)


def _format_history(history: list[dict[str, Any]]) -> str:
    """Format recent decision history into a readable block.

    Args:
        history: List of decision dicts, most recent first.

    Returns:
        Formatted history string, or a note if no history is available.
    """
    if not history:
        return "No previous decisions recorded yet."

    lines: list[str] = []
    for i, entry in enumerate(history, start=1):
        timestamp = entry.get("timestamp", "?")
        # Decision data is nested under the "decision" key in log records.
        decision = entry.get("decision", {})
        action = decision.get("action", "?")
        reason = decision.get("reason", "")
        params = decision.get("params", {})
        urgency = decision.get("urgency", "normal")
        executed = entry.get("executed", True)

        param_str = ""
        if params:
            param_str = " | " + ", ".join(f"{k}={v}" for k, v in params.items())

        exec_str = "" if executed else " [not executed]"
        lines.append(
            f"{i}. [{timestamp}] {action}{param_str} (urgency: {urgency}){exec_str} - {reason}"
        )

    return "\n".join(lines)


def _build_image_block(photo_path: str) -> dict[str, Any] | None:
    """Build an Anthropic-compatible image content block from a file path.

    Args:
        photo_path: Path to a JPEG or PNG image file.

    Returns:
        An image content block dict, or None if the file cannot be read.
    """
    path = Path(photo_path)
    if not path.exists() or not path.is_file():
        return None

    # Determine media type
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        # Default to JPEG for unrecognized types
        mime_type = "image/jpeg"

    try:
        raw_bytes = path.read_bytes()
        b64_data = base64.standard_b64encode(raw_bytes).decode("ascii")
    except OSError:
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": b64_data,
        },
    }
