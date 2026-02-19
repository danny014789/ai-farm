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
  "notes": "Any additional observations, concerns, or recommendations"
}"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_system_prompt(plant_profile: dict[str, Any], plant_knowledge: str) -> str:
    """Build the system prompt that defines Claude's role and constraints.

    Args:
        plant_profile: Parsed plant_profile.yaml dict. Expected keys include
            ``plant`` (name, variety, growth_stage, planted_date, notes) and
            ``ideal_conditions``.
        plant_knowledge: Cached markdown document with researched growing
            conditions for the current plant. May be empty if not yet cached.

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

    return f"""\
You are a plant care expert AI agent responsible for a single plant growing in a controlled indoor environment on a Raspberry Pi automation system.

## Your Plant
- Species: {plant_name}{variety_label}
- Growth stage: {growth_stage}
- Planted: {planted_date}
{"- Notes: " + notes if notes else ""}

## Ideal Growing Conditions
{ideal_block}
{knowledge_section}
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
- Water: duration_sec must be between 1 and 30. Minimum 60 minutes between waterings.
- Circulation: duration_sec must be between 10 and 300.
- Heater: Never turn on if temperature is already above {ideal.get("temp_max_c", 28)}C. Never leave on if above {ideal.get("temp_max_c", 28)}C.
- Light: Respect the plant's light schedule. Do NOT turn on lights between midnight and 5am unless the plant is severely light-deprived. Maximum {ideal.get("light_hours", 14)} hours per day.
- When in doubt, choose "do_nothing" and set "notify_human" to true.

## Decision Guidelines
1. Be CONSERVATIVE. Overwatering and overheating are worse than brief underwatering or mild cold.
2. Consider the TIME OF DAY. Plants have natural day/night cycles. Avoid unnecessary light or heater activation at night.
3. Check recent decision history to avoid repeating actions too frequently (e.g., do not water again if watered recently).
4. If sensor readings look abnormal or contradictory, choose "do_nothing" and set "notify_human" to true with a note explaining the anomaly.
5. If a photo is provided, examine it for signs of stress, pests, disease, wilting, discoloration, or other visual issues.
6. Prioritize: critical safety > plant health > optimal growth > energy efficiency.

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

    text_block = (
        f"## Current Time\n{current_time}\n\n"
        f"## Current Sensor Readings\n{sensor_text}\n\n"
        f"{actuator_section}"
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
                        "Examine it for visual signs of stress, pests, disease, "
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
# Internal helpers
# ---------------------------------------------------------------------------


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
    return "\n".join(lines)


def _format_actuator_state(state: dict[str, str]) -> str:
    """Format actuator state dict into a readable text block."""
    labels = {
        "light": "Grow light",
        "heater": "Heater",
        "pump": "Water pump",
        "circulation": "Circulation fan",
    }
    lines = [f"- {labels.get(k, k)}: {v}" for k, v in state.items()]
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
        action = entry.get("action", "?")
        reason = entry.get("reason", "")
        params = entry.get("params", {})
        urgency = entry.get("urgency", "normal")

        param_str = ""
        if params:
            param_str = " | " + ", ".join(f"{k}={v}" for k, v in params.items())

        lines.append(
            f"{i}. [{timestamp}] {action}{param_str} (urgency: {urgency}) - {reason}"
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
