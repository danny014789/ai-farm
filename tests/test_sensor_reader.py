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
