"""Tests for src/logger.py -- JSONL logging of sensors and decisions."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.logger import (
    SENSOR_FILE,
    DECISION_FILE,
    log_sensor_reading,
    log_decision,
    load_recent_decisions,
    load_recent_sensors,
    get_daily_action_counts,
    _read_jsonl,
    _append_jsonl,
)
from src.safety import ValidationResult
from src.sensor_reader import SensorData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sensor_data(**overrides) -> SensorData:
    defaults = dict(
        temperature_c=24.5,
        humidity_pct=62.0,
        co2_ppm=450,
        light_level=780,
        soil_moisture_pct=45.0,
        timestamp="2026-02-18T10:30:00+00:00",
    )
    defaults.update(overrides)
    return SensorData(**defaults)


def _make_validation(valid=True, reason="OK"):
    return ValidationResult(
        valid=valid,
        reason=reason,
        capped_action={"action": "water", "duration_sec": 10},
    )


def _make_decision(action="water", executed=True):
    return {
        "action": action,
        "params": {"duration_sec": 10},
        "reason": "Soil dry",
        "urgency": "normal",
    }


# ---------------------------------------------------------------------------
# log_sensor_reading
# ---------------------------------------------------------------------------


class TestLogSensorReading:
    def test_creates_jsonl_file(self, tmp_data_dir):
        sensor = _make_sensor_data()
        log_sensor_reading(sensor, tmp_data_dir)

        filepath = Path(tmp_data_dir) / SENSOR_FILE
        assert filepath.exists()

    def test_appends_record(self, tmp_data_dir):
        sensor1 = _make_sensor_data(temperature_c=22.0)
        sensor2 = _make_sensor_data(temperature_c=25.0)

        log_sensor_reading(sensor1, tmp_data_dir)
        log_sensor_reading(sensor2, tmp_data_dir)

        filepath = Path(tmp_data_dir) / SENSOR_FILE
        lines = filepath.read_text().strip().split("\n")
        assert len(lines) == 2

        record1 = json.loads(lines[0])
        record2 = json.loads(lines[1])
        assert record1["temperature_c"] == 22.0
        assert record2["temperature_c"] == 25.0

    def test_record_contains_logged_at(self, tmp_data_dir):
        sensor = _make_sensor_data()
        log_sensor_reading(sensor, tmp_data_dir)

        filepath = Path(tmp_data_dir) / SENSOR_FILE
        record = json.loads(filepath.read_text().strip())
        assert "logged_at" in record

    def test_record_contains_sensor_fields(self, tmp_data_dir):
        sensor = _make_sensor_data()
        log_sensor_reading(sensor, tmp_data_dir)

        filepath = Path(tmp_data_dir) / SENSOR_FILE
        record = json.loads(filepath.read_text().strip())
        assert record["temperature_c"] == 24.5
        assert record["humidity_pct"] == 62.0
        assert record["co2_ppm"] == 450
        assert record["light_level"] == 780
        assert record["soil_moisture_pct"] == 45.0

    def test_creates_directory_if_missing(self, tmp_path):
        nested_dir = str(tmp_path / "sub" / "data")
        sensor = _make_sensor_data()
        log_sensor_reading(sensor, nested_dir)

        filepath = Path(nested_dir) / SENSOR_FILE
        assert filepath.exists()


# ---------------------------------------------------------------------------
# log_decision
# ---------------------------------------------------------------------------


class TestLogDecision:
    def test_creates_jsonl_file(self, tmp_data_dir):
        sensor = _make_sensor_data()
        decision = _make_decision()
        validation = _make_validation()

        log_decision(sensor, decision, validation, executed=True, data_dir=tmp_data_dir)

        filepath = Path(tmp_data_dir) / DECISION_FILE
        assert filepath.exists()

    def test_correct_structure(self, tmp_data_dir):
        sensor = _make_sensor_data()
        decision = _make_decision()
        validation = _make_validation()

        log_decision(sensor, decision, validation, executed=True, data_dir=tmp_data_dir)

        filepath = Path(tmp_data_dir) / DECISION_FILE
        record = json.loads(filepath.read_text().strip())

        assert "timestamp" in record
        assert "sensor_data" in record
        assert "decision" in record
        assert "validation" in record
        assert "executed" in record

        assert record["executed"] is True
        assert record["decision"]["action"] == "water"
        assert record["validation"]["valid"] is True
        assert record["validation"]["reason"] == "OK"
        assert record["sensor_data"]["temperature_c"] == 24.5

    def test_executed_false(self, tmp_data_dir):
        sensor = _make_sensor_data()
        decision = _make_decision()
        validation = _make_validation(valid=False, reason="Rate limit")

        log_decision(sensor, decision, validation, executed=False, data_dir=tmp_data_dir)

        filepath = Path(tmp_data_dir) / DECISION_FILE
        record = json.loads(filepath.read_text().strip())
        assert record["executed"] is False
        assert record["validation"]["valid"] is False


# ---------------------------------------------------------------------------
# load_recent_decisions
# ---------------------------------------------------------------------------


class TestLoadRecentDecisions:
    def test_returns_last_n_records(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE

        records = []
        for i in range(5):
            rec = {
                "timestamp": f"2026-02-18T{10+i:02d}:00:00+00:00",
                "decision": {"action": "water"},
                "executed": True,
            }
            records.append(rec)

        with open(filepath, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        result = load_recent_decisions(3, tmp_data_dir)
        assert len(result) == 3
        # Should be the last 3 records
        assert result[0]["timestamp"] == "2026-02-18T12:00:00+00:00"
        assert result[2]["timestamp"] == "2026-02-18T14:00:00+00:00"

    def test_empty_file_returns_empty_list(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE
        filepath.touch()

        result = load_recent_decisions(5, tmp_data_dir)
        assert result == []

    def test_nonexistent_file_returns_empty_list(self, tmp_data_dir):
        result = load_recent_decisions(5, tmp_data_dir)
        assert result == []

    def test_n_larger_than_records(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE

        with open(filepath, "w") as f:
            f.write(json.dumps({"action": "water"}) + "\n")
            f.write(json.dumps({"action": "light_on"}) + "\n")

        result = load_recent_decisions(10, tmp_data_dir)
        assert len(result) == 2

    def test_n_zero_returns_empty(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE
        with open(filepath, "w") as f:
            f.write(json.dumps({"action": "water"}) + "\n")

        result = load_recent_decisions(0, tmp_data_dir)
        assert result == []


# ---------------------------------------------------------------------------
# load_recent_sensors
# ---------------------------------------------------------------------------


class TestLoadRecentSensors:
    def test_returns_last_n_records(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / SENSOR_FILE

        with open(filepath, "w") as f:
            for i in range(5):
                rec = {"temperature_c": 20.0 + i, "timestamp": f"2026-02-18T{10+i:02d}:00:00Z"}
                f.write(json.dumps(rec) + "\n")

        result = load_recent_sensors(2, tmp_data_dir)
        assert len(result) == 2
        assert result[0]["temperature_c"] == 23.0
        assert result[1]["temperature_c"] == 24.0

    def test_empty_file_returns_empty(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / SENSOR_FILE
        filepath.touch()

        result = load_recent_sensors(5, tmp_data_dir)
        assert result == []


# ---------------------------------------------------------------------------
# get_daily_action_counts
# ---------------------------------------------------------------------------


class TestGetDailyActionCounts:
    def test_counts_executed_actions_today(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE
        today = datetime.now(timezone.utc).date().isoformat()

        records = [
            {
                "timestamp": f"{today}T10:00:00+00:00",
                "decision": {"action": "water"},
                "executed": True,
            },
            {
                "timestamp": f"{today}T11:00:00+00:00",
                "decision": {"action": "water"},
                "executed": True,
            },
            {
                "timestamp": f"{today}T12:00:00+00:00",
                "decision": {"action": "light_on"},
                "executed": True,
            },
            {
                "timestamp": f"{today}T13:00:00+00:00",
                "decision": {"action": "water"},
                "executed": False,  # Not executed, should not count
            },
        ]

        with open(filepath, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        counts = get_daily_action_counts(tmp_data_dir)
        assert counts["water"] == 2
        assert counts["light_on"] == 1
        assert "do_nothing" not in counts  # not present

    def test_excludes_yesterday(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE
        today = datetime.now(timezone.utc).date().isoformat()

        records = [
            {
                "timestamp": "2020-01-01T10:00:00+00:00",  # old date
                "decision": {"action": "water"},
                "executed": True,
            },
            {
                "timestamp": f"{today}T10:00:00+00:00",
                "decision": {"action": "water"},
                "executed": True,
            },
        ]

        with open(filepath, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        counts = get_daily_action_counts(tmp_data_dir)
        assert counts.get("water", 0) == 1

    def test_empty_file_returns_empty_dict(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / DECISION_FILE
        filepath.touch()

        counts = get_daily_action_counts(tmp_data_dir)
        assert counts == {}

    def test_nonexistent_file_returns_empty_dict(self, tmp_data_dir):
        counts = get_daily_action_counts(tmp_data_dir)
        assert counts == {}


# ---------------------------------------------------------------------------
# _read_jsonl edge cases
# ---------------------------------------------------------------------------


class TestReadJsonl:
    def test_skips_malformed_lines(self, tmp_path):
        filepath = tmp_path / "test.jsonl"
        content = '{"valid": true}\nnot json at all\n{"also_valid": 1}\n'
        filepath.write_text(content)

        records = _read_jsonl(filepath)
        assert len(records) == 2
        assert records[0]["valid"] is True
        assert records[1]["also_valid"] == 1

    def test_skips_blank_lines(self, tmp_path):
        filepath = tmp_path / "test.jsonl"
        content = '{"a": 1}\n\n\n{"b": 2}\n'
        filepath.write_text(content)

        records = _read_jsonl(filepath)
        assert len(records) == 2

    def test_nonexistent_file_returns_empty(self, tmp_path):
        filepath = tmp_path / "nonexistent.jsonl"
        records = _read_jsonl(filepath)
        assert records == []

    def test_empty_file_returns_empty(self, tmp_path):
        filepath = tmp_path / "empty.jsonl"
        filepath.touch()

        records = _read_jsonl(filepath)
        assert records == []

    def test_mixed_valid_and_invalid(self, tmp_path):
        filepath = tmp_path / "mixed.jsonl"
        content = (
            '{"line": 1}\n'
            '{bad json}\n'
            '{"line": 3}\n'
            'just text\n'
            '{"line": 5}\n'
        )
        filepath.write_text(content)

        records = _read_jsonl(filepath)
        assert len(records) == 3
        assert records[0]["line"] == 1
        assert records[1]["line"] == 3
        assert records[2]["line"] == 5


# ---------------------------------------------------------------------------
# _append_jsonl
# ---------------------------------------------------------------------------


class TestAppendJsonl:
    def test_creates_file_if_not_exists(self, tmp_path):
        filepath = tmp_path / "new.jsonl"
        _append_jsonl(filepath, {"key": "value"})

        assert filepath.exists()
        record = json.loads(filepath.read_text().strip())
        assert record["key"] == "value"

    def test_appends_to_existing(self, tmp_path):
        filepath = tmp_path / "existing.jsonl"
        filepath.write_text('{"first": 1}\n')

        _append_jsonl(filepath, {"second": 2})

        lines = filepath.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["first"] == 1
        assert json.loads(lines[1])["second"] == 2
