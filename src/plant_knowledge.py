"""One-time plant research and knowledge caching.

When a plant is first configured (or changed via Telegram ``/setplant``),
this module calls ``claude_client.research_plant`` to produce a comprehensive
growing guide, saves it to ``data/plant_knowledge.md``, and updates
``config/plant_profile.yaml`` with the researched ideal conditions.

Subsequent runs skip the research step and load the cached markdown directly,
unless ``force=True`` is passed or the plant name has changed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from src.claude_client import research_plant
from src.config_loader import load_plant_profile, save_plant_profile

logger = logging.getLogger(__name__)

KNOWLEDGE_FILENAME = "plant_knowledge.md"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_plant_knowledge(
    plant_profile: dict[str, Any],
    data_dir: str,
    force: bool = False,
) -> str:
    """Return cached plant knowledge, researching if necessary.

    The function checks three conditions to decide whether to use the cache:
    1. ``data/plant_knowledge.md`` exists on disk.
    2. ``plant_profile["knowledge_cached"]`` is ``True``.
    3. The cached plant name matches the current profile name.

    If any condition fails (or *force* is ``True``), a fresh research call is
    made, the result is saved, and the plant profile is updated.

    Args:
        plant_profile: Parsed ``plant_profile.yaml`` dict (will be mutated
            in-place if a research call is made).
        data_dir: Path to the ``data/`` directory where the knowledge file
            is stored.
        force: If ``True``, always re-research regardless of cache state.

    Returns:
        The plant knowledge markdown string.

    Raises:
        ValueError: If the plant name is empty and research is needed.
    """
    data_path = Path(data_dir)
    knowledge_file = data_path / KNOWLEDGE_FILENAME

    plant = plant_profile.get("plant", {})
    plant_name = (plant.get("name") or "").strip()
    variety = (plant.get("variety") or "").strip()
    growth_stage = (plant.get("growth_stage") or "seedling").strip()
    cached_flag = plant_profile.get("knowledge_cached", False)

    # Determine whether the cache is valid
    cache_valid = (
        not force
        and cached_flag
        and knowledge_file.exists()
        and _cached_plant_matches(knowledge_file, plant_name)
    )

    if cache_valid:
        logger.info(
            "Using cached plant knowledge for '%s' from %s",
            plant_name,
            knowledge_file,
        )
        return knowledge_file.read_text(encoding="utf-8")

    # --- Research needed ---
    if not plant_name:
        raise ValueError(
            "Cannot research plant: no plant name is set in the profile. "
            "Use /setplant to configure a plant first."
        )

    logger.info(
        "Researching plant '%s' (variety=%s, stage=%s) ...",
        plant_name,
        variety or "(none)",
        growth_stage,
    )

    knowledge_md = research_plant(plant_name, variety, growth_stage)

    # Save to disk
    data_path.mkdir(parents=True, exist_ok=True)
    knowledge_file.write_text(knowledge_md, encoding="utf-8")
    logger.info("Plant knowledge saved to %s", knowledge_file)

    # Update ideal conditions in the profile from the research summary table
    ideal_updates = _parse_ideal_conditions(knowledge_md)
    if ideal_updates:
        if "ideal_conditions" not in plant_profile:
            plant_profile["ideal_conditions"] = {}
        plant_profile["ideal_conditions"].update(ideal_updates)
        logger.info(
            "Updated ideal_conditions from research: %s",
            ideal_updates,
        )

    # Mark knowledge as cached and persist the profile
    plant_profile["knowledge_cached"] = True
    save_plant_profile(plant_profile)
    logger.info("Plant profile updated with knowledge_cached=true")

    return knowledge_md


def invalidate_knowledge(data_dir: str) -> None:
    """Delete cached knowledge and reset the cached flag in the profile.

    Call this when the user changes the plant via ``/setplant`` to ensure
    a fresh research is triggered on the next agent run.

    Args:
        data_dir: Path to the ``data/`` directory.
    """
    data_path = Path(data_dir)
    knowledge_file = data_path / KNOWLEDGE_FILENAME

    # Remove the file
    if knowledge_file.exists():
        knowledge_file.unlink()
        logger.info("Deleted cached plant knowledge: %s", knowledge_file)
    else:
        logger.debug("No cached knowledge file to delete at %s", knowledge_file)

    # Reset the flag in the profile
    try:
        profile = load_plant_profile()
        profile["knowledge_cached"] = False
        save_plant_profile(profile)
        logger.info("Reset knowledge_cached to false in plant profile")
    except FileNotFoundError:
        logger.warning(
            "Could not update plant profile (file not found). "
            "knowledge_cached flag was not reset."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cached_plant_matches(knowledge_file: Path, plant_name: str) -> bool:
    """Check whether the cached knowledge file is for the current plant.

    Looks for the plant name in the first few lines of the markdown file
    (specifically in the ``# Growing Guide: <name>`` header).

    Args:
        knowledge_file: Path to the cached knowledge markdown file.
        plant_name: The current plant name to match against.

    Returns:
        ``True`` if the cached file appears to be for the same plant.
    """
    if not plant_name:
        return False

    try:
        # Read only the first 500 characters -- the header is at the top
        content_head = knowledge_file.read_text(encoding="utf-8")[:500]
    except OSError:
        return False

    # Case-insensitive check for the plant name in the header
    return plant_name.lower() in content_head.lower()


def _parse_ideal_conditions(knowledge_md: str) -> dict[str, Any]:
    """Attempt to extract ideal numeric conditions from the research markdown.

    Looks for the summary table produced by ``build_research_prompt`` and
    extracts the *Optimal* column values. Returns a dict of updates suitable
    for merging into ``plant_profile["ideal_conditions"]``.

    This is best-effort -- if parsing fails, returns an empty dict and the
    existing profile defaults remain untouched.

    Args:
        knowledge_md: The full markdown research document.

    Returns:
        Dict with keys like ``temp_min_c``, ``temp_max_c``, etc.
        Empty dict if parsing fails.
    """
    updates: dict[str, Any] = {}

    try:
        updates.update(_extract_from_summary_table(knowledge_md))
    except Exception:
        logger.debug(
            "Could not parse summary table from research -- "
            "keeping existing ideal_conditions defaults.",
            exc_info=True,
        )

    return updates


def _extract_from_summary_table(md: str) -> dict[str, Any]:
    """Parse the summary table for numeric ranges.

    Expected table format (from build_research_prompt):
    | Parameter          | Min  | Optimal | Max  | Unit |
    |--------------------|------|---------|------|------|
    | Temperature (day)  | 18   | 22-25   | 30   | C    |
    ...

    Returns:
        Dict with extracted ideal condition values.
    """
    updates: dict[str, Any] = {}

    # Find all table rows (lines starting with |)
    table_rows = re.findall(r"^\|(.+)\|$", md, re.MULTILINE)
    if len(table_rows) < 3:
        return updates

    # Map of parameter name patterns to profile keys
    param_map: dict[str, dict[str, str]] = {
        r"temp.*day": {"min": "temp_min_c", "max": "temp_max_c"},
        r"humid": {"min": "humidity_min_pct", "max": "humidity_max_pct"},
        r"soil.*moist": {"min": "soil_moisture_min_pct", "max": "soil_moisture_max_pct"},
        r"light.*hour": {"opt": "light_hours"},
        r"co2": {"min": "co2_min_ppm"},
    }

    for row in table_rows:
        cells = [c.strip() for c in row.split("|")]
        # cells[0] is empty (before first |), actual data starts at [1]
        # Filter out empty strings from split
        cells = [c for c in cells if c.strip()]
        if len(cells) < 4:
            continue

        param_name = cells[0].lower()

        # Skip header/separator rows
        if param_name.startswith("-") or param_name == "parameter":
            continue

        for pattern, key_map in param_map.items():
            if re.search(pattern, param_name, re.IGNORECASE):
                min_val = _try_parse_number(cells[1]) if len(cells) > 1 else None
                opt_val = cells[2] if len(cells) > 2 else None
                max_val = _try_parse_number(cells[3]) if len(cells) > 3 else None

                if "min" in key_map and min_val is not None:
                    updates[key_map["min"]] = min_val
                if "max" in key_map and max_val is not None:
                    updates[key_map["max"]] = max_val
                if "opt" in key_map and opt_val is not None:
                    # For light hours, try to get a single number
                    parsed = _try_parse_number(opt_val)
                    if parsed is not None:
                        updates[key_map["opt"]] = parsed

                break  # matched this row, move to next

    return updates


def _try_parse_number(text: str) -> float | int | None:
    """Try to extract a number from a string.

    Handles formats like "22", "22.5", "22-25" (takes first number),
    and strings with units like "400 ppm".

    Args:
        text: String that may contain a number.

    Returns:
        The parsed number, or None if no number is found.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Try direct parse first
    try:
        val = float(text)
        return int(val) if val == int(val) else val
    except ValueError:
        pass

    # Extract first number from the string (handles "22-25", "400 ppm", etc.)
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        val = float(match.group(1))
        return int(val) if val == int(val) else val

    return None
