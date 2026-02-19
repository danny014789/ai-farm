"""Tests for src/sensor_reader.py -- sensor reading and parsing."""

import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from src.sensor_reader import (
    SensorData,
    SensorReadError,
    read_sensors,
    read_sensors_mock,
    _parse_sensor_json,
)


# ---------------------------------------------------------------------------
# Valid sensor data dict used across tests
# ---------------------------------------------------------------------------

VALID_SENSOR_DICT = {
    "temperature_c": 24.5,
    "humidity_pct": 62.0,
    "co2_ppm": 450,
    "light_level": 780,
    "soil_moisture_pct": 45.0,
    "timestamp": "2026-02-18T10:30:00Z",
}

# Actual farmctl.py output format (different field names + raw ADC values)
FARMCTL_SENSOR_DICT = {
    "raw": "498,23.62,51.58,53,1023,1,0,0,0,0,0,0,0",
    "fields": ["498", "23.62", "51.58", "53", "1023", "1", "0", "0", "0", "0", "0", "0", "0"],
    "co2_ppm": 498.0,
    "temp_c": 23.62,
    "humidity_pct": 51.58,
    "light_raw": 53.0,
    "soil_raw": 1023.0,
    "water_tank_ok": True,
    "light_on": False,
    "heater_on": False,
    "heater_lockout": False,
    "water_pump_on": False,
    "circulation_on": False,
    "water_pump_remaining_sec": 0,
    "circulation_remaining_sec": 0,
    "source": "serial:r",
}


# ---------------------------------------------------------------------------
# _parse_sensor_json
# ---------------------------------------------------------------------------


class TestParseSensorJson:
    def test_valid_data_returns_sensor_data(self):
        result = _parse_sensor_json(VALID_SENSOR_DICT)
        assert isinstance(result, SensorData)
        assert result.temperature_c == 24.5
        assert result.humidity_pct == 62.0
        assert result.co2_ppm == 450
        assert result.light_level == 780
        assert result.soil_moisture_pct == 45.0
        assert result.timestamp == "2026-02-18T10:30:00Z"

    def test_missing_timestamp_gets_default(self):
        data = dict(VALID_SENSOR_DICT)
        del data["timestamp"]
        result = _parse_sensor_json(data)
        assert isinstance(result, SensorData)
        # Should have an auto-generated timestamp
        assert result.timestamp is not None
        assert len(result.timestamp) > 0

    def test_missing_required_field_raises(self):
        for field in [
            "temperature_c",
            "humidity_pct",
            "co2_ppm",
            "light_level",
            "soil_moisture_pct",
        ]:
            data = dict(VALID_SENSOR_DICT)
            del data[field]
            with pytest.raises(SensorReadError, match="Missing sensor fields"):
                _parse_sensor_json(data)

    def test_multiple_missing_fields_raises(self):
        data = {"timestamp": "2026-02-18T10:30:00Z"}
        with pytest.raises(SensorReadError, match="Missing sensor fields"):
            _parse_sensor_json(data)

    def test_invalid_type_raises(self):
        """Non-numeric values for numeric fields should raise SensorReadError."""
        data = dict(VALID_SENSOR_DICT)
        data["temperature_c"] = "not_a_number"
        with pytest.raises(SensorReadError, match="Invalid sensor data types"):
            _parse_sensor_json(data)

    def test_invalid_int_type_raises(self):
        data = dict(VALID_SENSOR_DICT)
        data["co2_ppm"] = "abc"
        with pytest.raises(SensorReadError, match="Invalid sensor data types"):
            _parse_sensor_json(data)

    def test_none_values_raise(self):
        data = dict(VALID_SENSOR_DICT)
        data["humidity_pct"] = None
        with pytest.raises(SensorReadError, match="Invalid sensor data types"):
            _parse_sensor_json(data)

    def test_numeric_strings_are_accepted(self):
        """Strings like '24.5' should be castable to float."""
        data = dict(VALID_SENSOR_DICT)
        data["temperature_c"] = "24.5"
        result = _parse_sensor_json(data)
        assert result.temperature_c == 24.5


# ---------------------------------------------------------------------------
# farmctl.py field name mapping
# ---------------------------------------------------------------------------


class TestFarmctlFieldMapping:
    """Tests for mapping farmctl.py field names to canonical SensorData fields."""

    def test_farmctl_output_parsed_correctly(self):
        """Real farmctl.py output with different field names should parse."""
        result = _parse_sensor_json(FARMCTL_SENSOR_DICT)
        assert isinstance(result, SensorData)
        assert result.temperature_c == 23.62
        assert result.humidity_pct == 51.58
        assert result.co2_ppm == 498
        assert result.light_level == 53

    def test_temp_c_mapped_to_temperature_c(self):
        data = {"temp_c": 25.0, "humidity_pct": 60.0, "co2_ppm": 400,
                "light_raw": 500, "soil_raw": 600}
        result = _parse_sensor_json(data)
        assert result.temperature_c == 25.0

    def test_light_raw_mapped_to_light_level(self):
        data = {"temp_c": 25.0, "humidity_pct": 60.0, "co2_ppm": 400,
                "light_raw": 512, "soil_raw": 600}
        result = _parse_sensor_json(data)
        assert result.light_level == 512

    def test_soil_raw_converted_to_percentage(self):
        """soil_raw=1023 (dry) should map to ~0%, soil_raw=300 (wet) to ~100%."""
        data = {"temp_c": 25.0, "humidity_pct": 60.0, "co2_ppm": 400,
                "light_raw": 500, "soil_raw": 1023}
        result = _parse_sensor_json(data)
        assert result.soil_moisture_pct == 0.0

    def test_soil_raw_wet_converted(self):
        """soil_raw=300 (wet) should map to 100%."""
        data = {"temp_c": 25.0, "humidity_pct": 60.0, "co2_ppm": 400,
                "light_raw": 500, "soil_raw": 300}
        result = _parse_sensor_json(data)
        assert result.soil_moisture_pct == 100.0

    def test_soil_raw_midpoint(self):
        """soil_raw in the middle should give roughly 50%."""
        midpoint = (1023 + 300) / 2  # 661.5
        data = {"temp_c": 25.0, "humidity_pct": 60.0, "co2_ppm": 400,
                "light_raw": 500, "soil_raw": midpoint}
        result = _parse_sensor_json(data)
        assert 45.0 <= result.soil_moisture_pct <= 55.0

    def test_soil_moisture_pct_passes_through(self):
        """If soil_moisture_pct is already 0-100, it should not be converted."""
        result = _parse_sensor_json(VALID_SENSOR_DICT)
        assert result.soil_moisture_pct == 45.0

    def test_canonical_names_preferred_over_farmctl(self):
        """If both temperature_c and temp_c exist, temperature_c wins."""
        data = {"temperature_c": 30.0, "temp_c": 20.0, "humidity_pct": 60.0,
                "co2_ppm": 400, "light_level": 500, "soil_moisture_pct": 50.0}
        result = _parse_sensor_json(data)
        assert result.temperature_c == 30.0

    def test_hardware_state_parsed_from_farmctl(self):
        """Full farmctl output with relay flags should populate hardware state."""
        result = _parse_sensor_json(FARMCTL_SENSOR_DICT)
        assert result.water_tank_ok is True
        assert result.light_on is False
        assert result.heater_on is False
        assert result.heater_lockout is False
        assert result.water_pump_on is False
        assert result.circulation_on is False
        assert result.water_pump_remaining_sec == 0
        assert result.circulation_remaining_sec == 0

    def test_hardware_state_none_when_missing(self):
        """When farmctl output has no relay data, hardware state fields are None."""
        result = _parse_sensor_json(VALID_SENSOR_DICT)
        assert result.water_tank_ok is None
        assert result.light_on is None
        assert result.heater_on is None
        assert result.heater_lockout is None

    def test_hardware_state_true_values(self):
        """Hardware state booleans should be True when reported as True."""
        data = dict(VALID_SENSOR_DICT)
        data["light_on"] = True
        data["heater_on"] = True
        data["water_tank_ok"] = False
        data["heater_lockout"] = True
        result = _parse_sensor_json(data)
        assert result.light_on is True
        assert result.heater_on is True
        assert result.water_tank_ok is False
        assert result.heater_lockout is True


# ---------------------------------------------------------------------------
# read_sensors_mock
# ---------------------------------------------------------------------------


class TestReadSensorsMock:
    def test_returns_valid_sensor_data(self):
        result = read_sensors_mock()
        assert isinstance(result, SensorData)

    def test_expected_defaults(self):
        result = read_sensors_mock()
        assert result.temperature_c == 24.5
        assert result.humidity_pct == 62.0
        assert result.co2_ppm == 450
        assert result.light_level == 780
        assert result.soil_moisture_pct == 45.0

    def test_timestamp_is_set(self):
        result = read_sensors_mock()
        assert result.timestamp is not None
        assert len(result.timestamp) > 0

    def test_mock_includes_hardware_state(self):
        result = read_sensors_mock()
        assert result.water_tank_ok is True
        assert result.light_on is False
        assert result.heater_on is False
        assert result.heater_lockout is False
        assert result.water_pump_on is False
        assert result.circulation_on is False
        assert result.water_pump_remaining_sec == 0
        assert result.circulation_remaining_sec == 0


# ---------------------------------------------------------------------------
# SensorData.to_dict
# ---------------------------------------------------------------------------


class TestSensorDataToDict:
    def test_returns_plain_dict(self):
        sd = SensorData(
            temperature_c=24.5,
            humidity_pct=62.0,
            co2_ppm=450,
            light_level=780,
            soil_moisture_pct=45.0,
            timestamp="2026-02-18T10:30:00Z",
        )
        d = sd.to_dict()
        assert isinstance(d, dict)
        assert d["temperature_c"] == 24.5
        assert d["humidity_pct"] == 62.0
        assert d["co2_ppm"] == 450
        assert d["light_level"] == 780
        assert d["soil_moisture_pct"] == 45.0
        assert d["timestamp"] == "2026-02-18T10:30:00Z"

    def test_to_dict_keys(self):
        sd = read_sensors_mock()
        d = sd.to_dict()
        expected_keys = {
            "temperature_c",
            "humidity_pct",
            "co2_ppm",
            "light_level",
            "soil_moisture_pct",
            "timestamp",
            "water_tank_ok",
            "light_on",
            "heater_on",
            "heater_lockout",
            "water_pump_on",
            "circulation_on",
            "water_pump_remaining_sec",
            "circulation_remaining_sec",
        }
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# read_sensors with mocked subprocess
# ---------------------------------------------------------------------------


class TestReadSensors:
    def test_success(self):
        """Successful subprocess call returns SensorData."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(VALID_SENSOR_DICT),
            stderr="",
        )
        with patch("src.sensor_reader.subprocess.run", return_value=mock_result):
            result = read_sensors("/fake/farmctl.py")

        assert isinstance(result, SensorData)
        assert result.temperature_c == 24.5
        assert result.co2_ppm == 450

    def test_nonzero_exit_raises(self):
        """Non-zero return code causes SensorReadError after retries."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="device not found",
        )
        with patch("src.sensor_reader.subprocess.run", return_value=mock_result):
            with pytest.raises(SensorReadError, match="All 3 sensor read attempts failed"):
                read_sensors("/fake/farmctl.py")

    def test_timeout_raises(self):
        """Subprocess timeout causes SensorReadError after retries."""
        with patch(
            "src.sensor_reader.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="farmctl", timeout=7),
        ):
            with pytest.raises(SensorReadError, match="All 3 sensor read attempts failed"):
                read_sensors("/fake/farmctl.py")

    def test_invalid_json_raises(self):
        """Malformed JSON output causes SensorReadError after retries."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="not valid json{{{",
            stderr="",
        )
        with patch("src.sensor_reader.subprocess.run", return_value=mock_result):
            with pytest.raises(SensorReadError, match="All 3 sensor read attempts failed"):
                read_sensors("/fake/farmctl.py")

    def test_empty_output_raises(self):
        """Empty stdout causes SensorReadError after retries."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("src.sensor_reader.subprocess.run", return_value=mock_result):
            with pytest.raises(SensorReadError, match="All 3 sensor read attempts failed"):
                read_sensors("/fake/farmctl.py")


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


class TestRetryBehavior:
    def test_fail_twice_succeed_third(self):
        """read_sensors retries and succeeds on the third attempt."""
        good_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(VALID_SENSOR_DICT),
            stderr="",
        )
        bad_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="port busy",
        )

        with patch(
            "src.sensor_reader.subprocess.run",
            side_effect=[bad_result, bad_result, good_result],
        ):
            result = read_sensors("/fake/farmctl.py", attempts=3)

        assert isinstance(result, SensorData)
        assert result.temperature_c == 24.5

    def test_all_retries_exhausted(self):
        """All attempts fail, raises SensorReadError."""
        bad_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="device error",
        )

        with patch(
            "src.sensor_reader.subprocess.run",
            return_value=bad_result,
        ):
            with pytest.raises(SensorReadError, match="All 2 sensor read attempts failed"):
                read_sensors("/fake/farmctl.py", attempts=2)

    def test_timeout_then_success(self):
        """First attempt times out, second succeeds."""
        good_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(VALID_SENSOR_DICT),
            stderr="",
        )

        with patch(
            "src.sensor_reader.subprocess.run",
            side_effect=[
                subprocess.TimeoutExpired(cmd="farmctl", timeout=7),
                good_result,
            ],
        ):
            result = read_sensors("/fake/farmctl.py", attempts=2)

        assert isinstance(result, SensorData)

    def test_custom_attempts_count(self):
        """Respects the attempts parameter."""
        bad_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="fail",
        )

        call_count = 0
        original_run = subprocess.run

        def counting_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return bad_result

        with patch("src.sensor_reader.subprocess.run", side_effect=counting_run):
            with pytest.raises(SensorReadError):
                read_sensors("/fake/farmctl.py", attempts=5)

        assert call_count == 5

    def test_os_error_retries(self):
        """OSError (e.g., file not found) is retried."""
        good_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(VALID_SENSOR_DICT),
            stderr="",
        )

        with patch(
            "src.sensor_reader.subprocess.run",
            side_effect=[
                OSError("Permission denied"),
                good_result,
            ],
        ):
            result = read_sensors("/fake/farmctl.py", attempts=2)

        assert isinstance(result, SensorData)
