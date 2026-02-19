"""Shared pytest fixtures for plant-ops-ai test suite."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sensor_reader import SensorData


# ---------------------------------------------------------------------------
# Path to mock sensor data JSON used across tests
# ---------------------------------------------------------------------------
MOCK_DATA_PATH = Path(__file__).parent / "mock_sensor_data.json"


def _load_mock_json() -> dict:
    """Load the mock_sensor_data.json file."""
    with open(MOCK_DATA_PATH, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sensor_data() -> SensorData:
    """Return a SensorData instance with normal mid-range values."""
    return SensorData(
        temperature_c=24.5,
        humidity_pct=62.0,
        co2_ppm=450,
        light_level=780,
        soil_moisture_pct=45.0,
        timestamp="2026-02-18T10:30:00+00:00",
        water_tank_ok=True,
        light_on=False,
        heater_on=False,
        heater_lockout=False,
        water_pump_on=False,
        circulation_on=False,
        water_pump_remaining_sec=0,
        circulation_remaining_sec=0,
    )


@pytest.fixture
def mock_sensor_data_dict() -> dict:
    """Return the dict version of normal sensor data."""
    return {
        "temperature_c": 24.5,
        "humidity_pct": 62.0,
        "co2_ppm": 450,
        "light_level": 780,
        "soil_moisture_pct": 45.0,
        "timestamp": "2026-02-18T10:30:00+00:00",
        "water_tank_ok": True,
        "light_on": False,
        "heater_on": False,
        "heater_lockout": False,
        "water_pump_on": False,
        "circulation_on": False,
        "water_pump_remaining_sec": 0,
        "circulation_remaining_sec": 0,
    }


@pytest.fixture
def sample_plant_profile() -> dict:
    """Return a realistic plant profile dict matching plant_profile.yaml shape."""
    return {
        "plant": {
            "name": "basil",
            "variety": "Genovese",
            "growth_stage": "vegetative",
            "planted_date": "2026-01-15",
            "notes": "Started from seed, healthy so far",
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
        "knowledge_cached": False,
    }


@pytest.fixture
def sample_safety_limits() -> dict:
    """Return the safety limits dict used to mock _load_limits()."""
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
            "max_duration_sec": 300,
            "min_interval_min": 30,
        },
        "emergency_stop_file": "/tmp/test-plant-agent-stop",
        "max_actions_per_hour": 10,
    }


@pytest.fixture
def tmp_data_dir(tmp_path) -> str:
    """Return a temporary data directory path as a string."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return str(data_dir)
