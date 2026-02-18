"""Tests for src/prompts.py -- prompt building for Claude API calls."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.prompts import (
    build_system_prompt,
    build_user_prompt,
    build_research_prompt,
    _format_sensor_data,
    _format_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_PROFILE = {
    "plant": {
        "name": "basil",
        "variety": "Genovese",
        "growth_stage": "vegetative",
        "planted_date": "2026-01-15",
        "notes": "Started from seed",
    },
    "ideal_conditions": {
        "temp_min_c": 18,
        "temp_max_c": 28,
        "humidity_min_pct": 40,
        "humidity_max_pct": 70,
        "soil_moisture_min_pct": 30,
        "soil_moisture_max_pct": 65,
        "light_hours": 14,
        "co2_min_ppm": 400,
    },
}

SAMPLE_SENSOR_DATA = {
    "temperature_c": 24.5,
    "humidity_pct": 62.0,
    "co2_ppm": 450,
    "light_level": 780,
    "soil_moisture_pct": 45.0,
}

SAMPLE_KNOWLEDGE = """# Growing Guide: Basil (Genovese)

## Ideal Temperature
- Day: 22-28C
- Night: 16-20C

## Ideal Humidity
- 40-70% relative humidity
"""


# ---------------------------------------------------------------------------
# build_system_prompt
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_includes_plant_name(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "basil" in prompt

    def test_includes_variety(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "Genovese" in prompt

    def test_includes_ideal_conditions(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "18" in prompt  # temp_min_c
        assert "28" in prompt  # temp_max_c
        assert "14" in prompt  # light_hours

    def test_includes_growth_stage(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "vegetative" in prompt

    def test_includes_planted_date(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "2026-01-15" in prompt

    def test_includes_plant_knowledge_when_provided(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, SAMPLE_KNOWLEDGE)
        assert "Researched Plant Knowledge" in prompt
        assert "Growing Guide: Basil" in prompt
        assert "Ideal Temperature" in prompt

    def test_no_knowledge_section_when_empty(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "Researched Plant Knowledge" not in prompt

    def test_no_knowledge_section_when_whitespace_only(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "   \n  ")
        assert "Researched Plant Knowledge" not in prompt

    def test_includes_response_schema(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "assessment" in prompt
        assert "action" in prompt
        assert "urgency" in prompt

    def test_includes_action_table(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "water" in prompt
        assert "light_on" in prompt
        assert "heater_on" in prompt
        assert "do_nothing" in prompt

    def test_includes_notes(self):
        prompt = build_system_prompt(SAMPLE_PROFILE, "")
        assert "Started from seed" in prompt

    def test_missing_plant_defaults(self):
        """When plant profile is sparse, defaults are used."""
        sparse_profile = {"plant": {}, "ideal_conditions": {}}
        prompt = build_system_prompt(sparse_profile, "")
        assert "unknown plant" in prompt
        assert "unknown" in prompt  # growth_stage default


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_returns_list_of_content_blocks(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
        )
        assert isinstance(blocks, list)
        assert len(blocks) >= 1

    def test_first_block_is_text_type(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
        )
        assert blocks[0]["type"] == "text"
        assert "text" in blocks[0]

    def test_text_block_contains_sensor_data(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
        )
        text = blocks[0]["text"]
        assert "24.5" in text
        assert "62.0" in text
        assert "450" in text

    def test_text_block_contains_current_time(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
        )
        assert "2026-02-18 10:30:00 UTC" in blocks[0]["text"]

    def test_with_photo_adds_image_block(self, tmp_path):
        """When a photo path is provided and the file exists, image block is added."""
        photo = tmp_path / "plant.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # minimal JPEG header

        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
            photo_path=str(photo),
        )
        # Should have text block + image block + description text block
        assert len(blocks) >= 2
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["type"] == "base64"

    def test_without_photo_no_image_block(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
            photo_path=None,
        )
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert len(image_blocks) == 0

    def test_nonexistent_photo_no_image_block(self):
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=[],
            current_time="2026-02-18 10:30:00 UTC",
            photo_path="/nonexistent/photo.jpg",
        )
        image_blocks = [b for b in blocks if b.get("type") == "image"]
        assert len(image_blocks) == 0

    def test_history_included_in_text(self):
        history = [
            {
                "timestamp": "2026-02-18T09:00:00Z",
                "action": "water",
                "params": {"duration_sec": 10},
                "reason": "Soil dry",
                "urgency": "normal",
            }
        ]
        blocks = build_user_prompt(
            sensor_data=SAMPLE_SENSOR_DATA,
            history=history,
            current_time="2026-02-18 10:30:00 UTC",
        )
        text = blocks[0]["text"]
        assert "water" in text
        assert "Soil dry" in text


# ---------------------------------------------------------------------------
# build_research_prompt
# ---------------------------------------------------------------------------


class TestBuildResearchPrompt:
    def test_includes_plant_name(self):
        prompt = build_research_prompt("basil", "Genovese", "vegetative")
        assert "basil" in prompt

    def test_includes_variety(self):
        prompt = build_research_prompt("basil", "Genovese", "vegetative")
        assert "Genovese" in prompt

    def test_includes_growth_stage(self):
        prompt = build_research_prompt("basil", "Genovese", "vegetative")
        assert "vegetative" in prompt

    def test_empty_variety(self):
        prompt = build_research_prompt("basil", "", "seedling")
        assert "basil" in prompt
        # Should not have empty parentheses
        assert "()" not in prompt

    def test_includes_required_sections(self):
        prompt = build_research_prompt("tomato", "Cherry", "flowering")
        assert "Ideal Temperature" in prompt
        assert "Ideal Humidity" in prompt
        assert "Soil Moisture" in prompt
        assert "Light Requirements" in prompt
        assert "CO2" in prompt
        assert "Air Circulation" in prompt
        assert "Common Issues" in prompt
        assert "Growth Stage Tips" in prompt
        assert "Summary Table" in prompt

    def test_summary_table_format(self):
        prompt = build_research_prompt("basil", "", "vegetative")
        assert "Parameter" in prompt
        assert "Min" in prompt
        assert "Optimal" in prompt
        assert "Max" in prompt


# ---------------------------------------------------------------------------
# _format_sensor_data
# ---------------------------------------------------------------------------


class TestFormatSensorData:
    def test_includes_all_fields(self):
        text = _format_sensor_data(SAMPLE_SENSOR_DATA)
        assert "24.5" in text  # temperature_c
        assert "62.0" in text  # humidity_pct
        assert "450" in text   # co2_ppm
        assert "780" in text   # light_level
        assert "45.0" in text  # soil_moisture_pct

    def test_includes_labels(self):
        text = _format_sensor_data(SAMPLE_SENSOR_DATA)
        assert "Temperature" in text
        assert "Humidity" in text
        assert "CO2" in text
        assert "Light" in text
        assert "Soil" in text

    def test_missing_fields_show_na(self):
        text = _format_sensor_data({})
        assert "N/A" in text


# ---------------------------------------------------------------------------
# _format_history
# ---------------------------------------------------------------------------


class TestFormatHistory:
    def test_empty_history(self):
        text = _format_history([])
        assert "No previous decisions" in text

    def test_with_entries(self):
        history = [
            {
                "timestamp": "2026-02-18T09:00:00Z",
                "action": "water",
                "params": {"duration_sec": 10},
                "reason": "Soil dry",
                "urgency": "normal",
            },
            {
                "timestamp": "2026-02-18T08:00:00Z",
                "action": "do_nothing",
                "params": {},
                "reason": "All OK",
                "urgency": "normal",
            },
        ]
        text = _format_history(history)
        assert "water" in text
        assert "Soil dry" in text
        assert "do_nothing" in text
        assert "All OK" in text
        # Should be numbered
        assert "1." in text
        assert "2." in text

    def test_params_displayed(self):
        history = [
            {
                "timestamp": "2026-02-18T09:00:00Z",
                "action": "water",
                "params": {"duration_sec": 15},
                "reason": "dry",
                "urgency": "attention",
            },
        ]
        text = _format_history(history)
        assert "duration_sec=15" in text
        assert "attention" in text

    def test_missing_fields_handled(self):
        history = [{"action": "water"}]
        text = _format_history(history)
        assert "water" in text
        # Should not crash on missing timestamp/reason
