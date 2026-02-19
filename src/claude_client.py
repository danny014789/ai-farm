"""Anthropic API wrapper for plant care decisions and plant research.

Provides two main capabilities:
1. ``get_plant_decision`` -- sends sensor data (and optional photo) to Claude
   and returns a structured JSON care decision.
2. ``research_plant`` -- asks Claude to produce a comprehensive growing guide
   for a given plant species, used for one-time knowledge caching.

Configuration is driven by environment variables:
- ANTHROPIC_API_KEY (required)
- CLAUDE_MODEL (optional, defaults to claude-sonnet-4-20250514)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

from src.prompts import (
    build_chat_system_prompt,
    build_chat_user_prompt,
    build_research_prompt,
    build_system_prompt,
    build_user_prompt,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_DECISION_TOKENS = 1536
MAX_CHAT_TOKENS = 1536
MAX_RESEARCH_TOKENS = 4096
MAX_RETRIES = 3
RETRY_BASE_DELAY_SEC = 2.0  # exponential backoff: 2s, 4s, 8s

# Approximate pricing per 1M tokens (Sonnet). Used for cost estimation only.
_INPUT_COST_PER_M = 3.0   # USD per 1M input tokens
_OUTPUT_COST_PER_M = 15.0  # USD per 1M output tokens


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------
class TokenUsageTracker:
    """Simple in-memory token/cost tracker for the current process lifetime."""

    def __init__(self) -> None:
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.call_count: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from a single API call."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.call_count += 1

    @property
    def estimated_cost_usd(self) -> float:
        """Rough cost estimate based on public Sonnet pricing."""
        input_cost = (self.total_input_tokens / 1_000_000) * _INPUT_COST_PER_M
        output_cost = (self.total_output_tokens / 1_000_000) * _OUTPUT_COST_PER_M
        return input_cost + output_cost

    def summary(self) -> dict[str, Any]:
        """Return a summary dict suitable for logging."""
        return {
            "calls": self.call_count,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


# Module-level tracker -- persists for the process lifetime.
usage_tracker = TokenUsageTracker()


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _get_client() -> anthropic.Anthropic:
    """Create an Anthropic client using the API key from the environment.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Set it in your .env file or export it in your shell."
        )
    return anthropic.Anthropic(api_key=api_key)


def _get_model() -> str:
    """Return the Claude model to use, from env or default."""
    return os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)


def _call_with_retry(
    fn: Any,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_SEC,
) -> Any:
    """Call *fn* with exponential backoff on transient failures.

    Retries on:
    - ``anthropic.RateLimitError``
    - ``anthropic.APIConnectionError``
    - ``anthropic.InternalServerError``

    All other exceptions propagate immediately.

    Args:
        fn: A zero-argument callable that performs the API call.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds (doubled each retry).

    Returns:
        The return value of *fn* on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Anthropic API error (attempt %d/%d): %s. "
                    "Retrying in %.1fs ...",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Anthropic API error after %d attempts: %s",
                    max_retries + 1,
                    exc,
                )

    # Should not be reached, but satisfies the type checker.
    raise last_exc  # type: ignore[misc]


def _extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from Claude's response text.

    Handles cases where Claude wraps JSON in markdown code fences
    despite being told not to.

    Args:
        text: Raw response text from Claude.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON object can be extracted.
    """
    cleaned = text.strip()

    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_newline + 1:]
        # Remove closing fence
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
        raise ValueError(f"Expected JSON object, got {type(result).__name__}")
    except json.JSONDecodeError as exc:
        # Last resort: try to find a JSON object in the text
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(
            f"Failed to parse JSON from Claude response: {exc}. "
            f"Raw text (first 500 chars): {text[:500]}"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_plant_decision(
    sensor_data: dict[str, Any],
    plant_profile: dict[str, Any],
    plant_knowledge: str,
    history: list[dict[str, Any]],
    photo_path: str | None = None,
    actuator_state: dict[str, str] | None = None,
    plant_log: list[dict[str, Any]] | None = None,
    hardware_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Claude to get a plant care decision based on current conditions.

    Args:
        sensor_data: Current sensor readings dict with keys like
            temp_c, humidity_pct, co2_ppm, light_level, soil_moisture_pct.
        plant_profile: Parsed plant_profile.yaml dict.
        plant_knowledge: Cached plant knowledge markdown string.
        history: Recent decision history (list of dicts, most recent first).
        photo_path: Optional filesystem path to a current plant photo.
        actuator_state: Optional dict of current actuator states.
        plant_log: Optional list of recent plant observation dicts.

    Returns:
        Parsed decision dict with keys: assessment, actions (list),
        urgency, notify_human, notes, message, observations, knowledge_update.

    Raises:
        ValueError: If the response cannot be parsed as valid JSON.
        anthropic.AuthenticationError: If the API key is invalid.
        anthropic.RateLimitError: If rate limits are exceeded after retries.
        anthropic.APIConnectionError: If the API is unreachable after retries.
    """
    client = _get_client()
    model = _get_model()

    current_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    system_prompt = build_system_prompt(plant_profile, plant_knowledge, hardware_profile)
    user_content = build_user_prompt(
        sensor_data=sensor_data,
        history=history,
        current_time=current_time,
        photo_path=photo_path,
        actuator_state=actuator_state,
        plant_log=plant_log,
    )

    logger.info(
        "Requesting plant decision from %s (photo=%s)",
        model,
        "yes" if photo_path else "no",
    )

    def _api_call() -> anthropic.types.Message:
        return client.messages.create(
            model=model,
            max_tokens=MAX_DECISION_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    response = _call_with_retry(_api_call)

    # Track token usage
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    usage_tracker.record(input_tokens, output_tokens)

    logger.info(
        "Decision response: %d input tokens, %d output tokens (cumulative cost: $%.4f)",
        input_tokens,
        output_tokens,
        usage_tracker.estimated_cost_usd,
    )

    # Extract text from response
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    if not raw_text.strip():
        raise ValueError("Claude returned an empty response.")

    decision = _extract_json(raw_text)

    # Normalize old single-action format to multi-action format
    if "action" in decision and "actions" not in decision:
        decision["actions"] = [
            {
                "action": decision.pop("action"),
                "params": decision.pop("params", {}),
                "reason": decision.pop("reason", ""),
            }
        ]

    # Validate required keys are present
    required_keys = {"assessment", "actions", "urgency", "notify_human"}
    missing = required_keys - set(decision.keys())
    if missing:
        logger.warning(
            "Decision missing expected keys: %s. Raw: %s",
            missing,
            raw_text[:300],
        )
        decision.setdefault("assessment", "Unable to assess (incomplete response)")
        decision.setdefault("actions", [{"action": "do_nothing", "params": {}, "reason": "Incomplete AI response -- defaulting to no action"}])
        decision.setdefault("urgency", "attention")
        decision.setdefault("notify_human", True)
        decision.setdefault("notes", f"Missing keys in AI response: {missing}")

    # Fill defaults per action in the actions list
    for act in decision.get("actions", []):
        act.setdefault("action", "do_nothing")
        act.setdefault("params", {})
        act.setdefault("reason", "")

    decision.setdefault("notes", "")
    decision.setdefault("message", "")
    decision.setdefault("observations", [])
    decision.setdefault("knowledge_update", None)
    decision.setdefault("hardware_update", None)

    return decision


def get_chat_response(
    user_message: str,
    sensor_data: dict[str, Any],
    plant_profile: dict[str, Any],
    plant_knowledge: str,
    history: list[dict[str, Any]],
    actuator_state: dict[str, str] | None = None,
    plant_log: list[dict[str, Any]] | None = None,
    hardware_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Claude to respond to a user's natural language message.

    Unlike ``get_plant_decision``, this returns a conversational response
    with an optional actions array for when the user requests something.

    Args:
        user_message: The user's text message from Telegram.
        sensor_data: Current sensor readings dict.
        plant_profile: Parsed plant_profile.yaml dict.
        plant_knowledge: Cached plant knowledge markdown string.
        history: Recent decision history.
        actuator_state: Current actuator states.
        plant_log: Recent plant log entries.

    Returns:
        Dict with keys: message (str), actions (list), observations (list).

    Raises:
        ValueError: If response cannot be parsed.
        anthropic.AuthenticationError: If the API key is invalid.
        anthropic.RateLimitError: If rate limits are exceeded after retries.
        anthropic.APIConnectionError: If the API is unreachable after retries.
    """
    client = _get_client()
    model = _get_model()

    current_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    system_prompt = build_chat_system_prompt(plant_profile, plant_knowledge, hardware_profile)
    user_content = build_chat_user_prompt(
        user_message=user_message,
        sensor_data=sensor_data,
        history=history,
        current_time=current_time,
        actuator_state=actuator_state,
        plant_log=plant_log,
    )

    logger.info("Requesting chat response from %s", model)

    def _api_call() -> anthropic.types.Message:
        return client.messages.create(
            model=model,
            max_tokens=MAX_CHAT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )

    response = _call_with_retry(_api_call)

    # Track token usage
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    usage_tracker.record(input_tokens, output_tokens)

    logger.info(
        "Chat response: %d input, %d output tokens (cost: $%.4f)",
        input_tokens,
        output_tokens,
        usage_tracker.estimated_cost_usd,
    )

    # Extract text from response
    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text

    if not raw_text.strip():
        raise ValueError("Claude returned an empty chat response.")

    result = _extract_json(raw_text)

    # Ensure required keys
    result.setdefault("message", "I'm not sure how to respond to that.")
    result.setdefault("actions", [])
    result.setdefault("observations", [])
    result.setdefault("hardware_update", None)

    # Fill defaults per action
    for act in result.get("actions", []):
        act.setdefault("action", "do_nothing")
        act.setdefault("params", {})
        act.setdefault("reason", "")

    return result


def research_plant(
    plant_name: str,
    variety: str,
    growth_stage: str,
) -> str:
    """Ask Claude to produce a comprehensive plant care guide.

    This function is called ONCE per plant setup. The result is cached as a
    markdown file and used as context in all subsequent decision calls.

    Args:
        plant_name: Common plant name (e.g., "basil").
        variety: Specific variety (e.g., "Genovese"). May be empty.
        growth_stage: Current growth stage
            (seedling/vegetative/flowering/fruiting).

    Returns:
        A markdown string with comprehensive growing information.

    Raises:
        anthropic.AuthenticationError: If the API key is invalid.
        anthropic.RateLimitError: If rate limits are exceeded after retries.
        anthropic.APIConnectionError: If the API is unreachable after retries.
    """
    client = _get_client()
    model = _get_model()

    user_prompt = build_research_prompt(plant_name, variety, growth_stage)

    logger.info(
        "Researching plant: %s %s (stage: %s) using %s",
        plant_name,
        variety,
        growth_stage,
        model,
    )

    def _api_call() -> anthropic.types.Message:
        return client.messages.create(
            model=model,
            max_tokens=MAX_RESEARCH_TOKENS,
            system=(
                "You are a botanist and indoor gardening expert. "
                "Provide detailed, accurate, and practical growing information. "
                "Use specific numeric ranges that an automated system can use. "
                "Format your response in clean Markdown."
            ),
            messages=[{"role": "user", "content": user_prompt}],
        )

    response = _call_with_retry(_api_call)

    # Track token usage
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    usage_tracker.record(input_tokens, output_tokens)

    logger.info(
        "Research response: %d input tokens, %d output tokens (cumulative cost: $%.4f)",
        input_tokens,
        output_tokens,
        usage_tracker.estimated_cost_usd,
    )

    # Extract text content
    result_parts: list[str] = []
    for block in response.content:
        if block.type == "text":
            result_parts.append(block.text)

    result = "\n".join(result_parts).strip()

    if not result:
        raise ValueError("Claude returned an empty research response.")

    # Prepend a header with metadata
    variety_label = f" ({variety})" if variety else ""
    header = (
        f"# Growing Guide: {plant_name}{variety_label}\n\n"
        f"*Growth stage: {growth_stage}*  \n"
        f"*Researched: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*\n\n"
        "---\n\n"
    )

    return header + result
