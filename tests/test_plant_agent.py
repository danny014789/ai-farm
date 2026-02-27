"""Tests for src/plant_agent.py -- main orchestrator."""

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from src.plant_agent import (
    FALLBACK_RULES,
    append_knowledge_update,
    _apply_fallback_rules,
    format_summary_text,
    run_check,
)
from src.safety import ValidationResult
from src.sensor_reader import SensorData, SensorReadError


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


SAMPLE_PROFILE = {
    "plant": {
        "name": "basil",
        "variety": "Genovese",
        "growth_stage": "vegetative",
        "planted_date": "2026-01-15",
        "notes": "",
    },
    "ideal_conditions": {
        "temp_min_c": 18,
        "temp_max_c": 28,
    },
    "knowledge_cached": True,
}

SAMPLE_DECISION = {
    "actions": [
        {"action": "water", "params": {"duration_sec": 10}, "reason": "Soil is dry"},
    ],
    "urgency": "normal",
    "notify_human": False,
    "assessment": "Plant needs watering",
    "notes": "",
    "message": "Your basil is a bit thirsty. Giving it a quick drink.",
    "observations": ["Soil moisture dropped from 55% to 45% in 4 hours"],
    "knowledge_update": None,
}


def _make_validation(valid=True, reason="OK", capped=None):
    if capped is None:
        capped = {"action": "water", "duration_sec": 10}
    return ValidationResult(valid=valid, reason=reason, capped_action=capped)


def _make_exec_result(success=True, action="water", command="farmctl pump on", dry_run=True):
    result = MagicMock()
    result.success = success
    result.action = action
    result.command = command
    result.error = None if success else "execution failed"
    result.dry_run = dry_run
    return result


# ---------------------------------------------------------------------------
# Common patches for run_check
# ---------------------------------------------------------------------------

def _common_patches():
    """Return a dict of patches needed for run_check tests."""
    return {
        "read_mock": patch("src.plant_agent.read_sensors_mock", return_value=_make_sensor_data()),
        "read_real": patch("src.plant_agent.read_sensors", return_value=_make_sensor_data()),
        "profile": patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE),
        "knowledge": patch("src.plant_agent.ensure_plant_knowledge", return_value="cached knowledge"),
        "decision": patch("src.plant_agent.get_plant_decision", return_value=SAMPLE_DECISION),
        "validate": patch("src.plant_agent.validate_action", return_value=_make_validation()),
        "log_sensor": patch("src.plant_agent.log_sensor_reading"),
        "log_decision": patch("src.plant_agent.log_decision"),
        "load_history": patch("src.plant_agent.load_recent_decisions", return_value=[]),
        "executor_cls": patch("src.plant_agent.ActionExecutor"),
        "actuator_state": patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}),
        "update_actuator": patch("src.plant_agent.update_after_action"),
        "load_plant_log": patch("src.plant_agent.load_recent_plant_log", return_value=[]),
        "log_observations": patch("src.plant_agent.log_plant_observations"),
        "hardware_profile": patch("src.plant_agent.load_hardware_profile", return_value={}),
    }


# ---------------------------------------------------------------------------
# run_check: basic structure
# ---------------------------------------------------------------------------


class TestRunCheck:
    def test_returns_expected_summary_keys(self):
        """run_check returns dict with required keys."""
        patches = _common_patches()
        with patches["read_mock"], patches["profile"], patches["knowledge"], \
             patches["decision"], patches["validate"], patches["log_sensor"], \
             patches["log_decision"], patches["load_history"], patches["executor_cls"] as mock_exec_cls, \
             patches["actuator_state"], patches["update_actuator"], \
             patches["load_plant_log"], patches["log_observations"], \
             patches["hardware_profile"]:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        expected_keys = {
            "timestamp", "sensor_data", "decision",
            "actions_taken", "executed", "photo_path", "error", "mode",
            "observations", "knowledge_update", "hardware_update",
            "weather_data",
        }
        assert set(summary.keys()) == expected_keys

    def test_summary_has_sensor_data(self):
        patches = _common_patches()
        with patches["read_mock"], patches["profile"], patches["knowledge"], \
             patches["decision"], patches["validate"], patches["log_sensor"], \
             patches["log_decision"], patches["load_history"], patches["executor_cls"] as mock_exec_cls, \
             patches["actuator_state"], patches["update_actuator"], \
             patches["load_plant_log"], patches["log_observations"], \
             patches["hardware_profile"]:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        assert summary["sensor_data"] is not None
        assert summary["sensor_data"]["temperature_c"] == 24.5

    def test_mode_is_dry_run(self):
        patches = _common_patches()
        with patches["read_mock"], patches["profile"], patches["knowledge"], \
             patches["decision"], patches["validate"], patches["log_sensor"], \
             patches["log_decision"], patches["load_history"], patches["executor_cls"] as mock_exec_cls, \
             patches["actuator_state"], patches["update_actuator"], \
             patches["load_plant_log"], patches["log_observations"], \
             patches["hardware_profile"]:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        assert summary["mode"] == "dry-run"

    def test_mode_is_live(self):
        patches = _common_patches()
        with patches["read_mock"], patches["profile"], patches["knowledge"], \
             patches["decision"], patches["validate"], patches["log_sensor"], \
             patches["log_decision"], patches["load_history"], patches["executor_cls"] as mock_exec_cls, \
             patches["actuator_state"], patches["update_actuator"], \
             patches["load_plant_log"], patches["log_observations"], \
             patches["hardware_profile"]:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result(dry_run=False)
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=False,
                use_mock=True,
                include_photo=False,
            )

        assert summary["mode"] == "live"


# ---------------------------------------------------------------------------
# run_check: dry-run
# ---------------------------------------------------------------------------


class TestRunCheckDryRun:
    def test_dry_run_does_not_call_real_subprocess(self):
        """In dry-run mode, ActionExecutor is initialized with dry_run=True."""
        patches = _common_patches()
        with patches["read_mock"], patches["profile"], patches["knowledge"], \
             patches["decision"], patches["validate"], patches["log_sensor"], \
             patches["log_decision"], patches["load_history"], patches["executor_cls"] as mock_exec_cls, \
             patches["actuator_state"], patches["update_actuator"], \
             patches["load_plant_log"], patches["log_observations"], \
             patches["hardware_profile"]:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result()
            mock_exec_cls.return_value = mock_executor

            run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        # ActionExecutor should be constructed with dry_run=True
        mock_exec_cls.assert_called_with("/fake/farmctl.py", dry_run=True)


# ---------------------------------------------------------------------------
# run_check: Claude API failure triggers offline fallback
# ---------------------------------------------------------------------------


class TestRunCheckFallback:
    def test_api_failure_triggers_fallback_soil_critical(self):
        """When Claude API fails and soil < 25, fallback triggers water action."""
        sensor = _make_sensor_data(soil_moisture_pct=20.0)

        with patch("src.plant_agent.read_sensors_mock", return_value=sensor), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE), \
             patch("src.plant_agent.ensure_plant_knowledge", return_value=""), \
             patch("src.plant_agent.get_plant_decision", side_effect=Exception("API down")), \
             patch("src.plant_agent.validate_action", return_value=_make_validation()), \
             patch("src.plant_agent.log_sensor_reading"), \
             patch("src.plant_agent.log_decision"), \
             patch("src.plant_agent.load_recent_decisions", return_value=[]), \
             patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}), \
             patch("src.plant_agent.update_after_action"), \
             patch("src.plant_agent.load_recent_plant_log", return_value=[]), \
             patch("src.plant_agent.log_plant_observations"), \
             patch("src.plant_agent.load_hardware_profile", return_value={}), \
             patch("src.plant_agent.ActionExecutor") as mock_exec_cls:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        first_action = summary["decision"]["actions"][0]
        assert first_action["action"] == "water"
        assert "fallback" in first_action["reason"].lower()

    def test_api_failure_triggers_fallback_temp_cold(self):
        """When Claude API fails and temp < 15, fallback triggers heater_on."""
        sensor = _make_sensor_data(temperature_c=12.0, soil_moisture_pct=50.0)

        with patch("src.plant_agent.read_sensors_mock", return_value=sensor), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE), \
             patch("src.plant_agent.ensure_plant_knowledge", return_value=""), \
             patch("src.plant_agent.get_plant_decision", side_effect=Exception("API down")), \
             patch("src.plant_agent.validate_action", return_value=_make_validation(
                 capped={"action": "heater_on"}
             )), \
             patch("src.plant_agent.log_sensor_reading"), \
             patch("src.plant_agent.log_decision"), \
             patch("src.plant_agent.load_recent_decisions", return_value=[]), \
             patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}), \
             patch("src.plant_agent.update_after_action"), \
             patch("src.plant_agent.load_recent_plant_log", return_value=[]), \
             patch("src.plant_agent.log_plant_observations"), \
             patch("src.plant_agent.load_hardware_profile", return_value={}), \
             patch("src.plant_agent.ActionExecutor") as mock_exec_cls:

            mock_executor = MagicMock()
            mock_executor.execute.return_value = _make_exec_result(action="heater_on")
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        first_action = summary["decision"]["actions"][0]
        assert first_action["action"] == "heater_on"
        assert "fallback" in first_action["reason"].lower()

    def test_api_failure_no_fallback_defaults_do_nothing(self):
        """When Claude API fails and no fallback rule matches, decision is do_nothing."""
        sensor = _make_sensor_data()  # normal conditions

        with patch("src.plant_agent.read_sensors_mock", return_value=sensor), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE), \
             patch("src.plant_agent.ensure_plant_knowledge", return_value=""), \
             patch("src.plant_agent.get_plant_decision", side_effect=Exception("API down")), \
             patch("src.plant_agent.validate_action", return_value=_make_validation(
                 capped={"action": "do_nothing"}
             )), \
             patch("src.plant_agent.log_sensor_reading"), \
             patch("src.plant_agent.log_decision"), \
             patch("src.plant_agent.load_recent_decisions", return_value=[]), \
             patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}), \
             patch("src.plant_agent.update_after_action"), \
             patch("src.plant_agent.load_recent_plant_log", return_value=[]), \
             patch("src.plant_agent.log_plant_observations"), \
             patch("src.plant_agent.load_hardware_profile", return_value={}), \
             patch("src.plant_agent.ActionExecutor") as mock_exec_cls:

            mock_executor = MagicMock()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        first_action = summary["decision"]["actions"][0]
        assert first_action["action"] == "do_nothing"
        assert summary["decision"]["notify_human"] is True


# ---------------------------------------------------------------------------
# _apply_fallback_rules
# ---------------------------------------------------------------------------


class TestApplyFallbackRules:
    def test_soil_moisture_critical(self):
        sensor = _make_sensor_data(soil_moisture_pct=20.0)
        result = _apply_fallback_rules(sensor)
        assert result is not None
        assert result["actions"][0]["action"] == "water"
        assert "soil" in result["actions"][0]["reason"].lower()

    def test_temp_too_cold(self):
        sensor = _make_sensor_data(temperature_c=12.0, soil_moisture_pct=50.0)
        result = _apply_fallback_rules(sensor)
        assert result is not None
        assert result["actions"][0]["action"] == "heater_on"
        assert "temperature" in result["actions"][0]["reason"].lower()

    def test_temp_too_hot(self):
        sensor = _make_sensor_data(temperature_c=35.0, soil_moisture_pct=50.0)
        result = _apply_fallback_rules(sensor)
        assert result is not None
        assert result["actions"][0]["action"] == "heater_off"

    def test_no_fallback_normal_conditions(self):
        sensor = _make_sensor_data()
        result = _apply_fallback_rules(sensor)
        assert result is None

    def test_fallback_priority_soil_first(self):
        """soil_moisture_critical is checked before temp_too_cold."""
        sensor = _make_sensor_data(soil_moisture_pct=20.0, temperature_c=12.0)
        result = _apply_fallback_rules(sensor)
        # soil_moisture_critical comes first in FALLBACK_RULES dict
        assert result["actions"][0]["action"] == "water"

    def test_fallback_contains_urgency_and_notify(self):
        sensor = _make_sensor_data(soil_moisture_pct=20.0)
        result = _apply_fallback_rules(sensor)
        assert result["urgency"] == "attention"
        assert result["notify_human"] is True
        assert "assessment" in result


# ---------------------------------------------------------------------------
# format_summary_text
# ---------------------------------------------------------------------------


class TestFormatSummaryText:
    def test_produces_readable_output(self):
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": {
                "temperature_c": 24.5,
                "humidity_pct": 62.0,
                "co2_ppm": 450,
                "light_level": 780,
                "soil_moisture_pct": 45.0,
            },
            "decision": {
                "actions": [
                    {"action": "water", "reason": "Soil is dry", "params": {}},
                ],
                "urgency": "normal",
                "notes": "Watch for overwatering",
                "message": "Your basil needs a drink!",
            },
            "actions_taken": [{"action": "water", "executed": True}],
            "executed": True,
            "photo_path": None,
            "error": None,
            "mode": "dry-run",
            "observations": ["Soil dropped quickly today"],
        }
        text = format_summary_text(summary)
        assert isinstance(text, str)
        # Concise format: status bar + executed action + message
        assert "24.5" in text
        assert "water" in text
        assert "Your basil needs a drink!" in text
        # Normal urgency = concise, no verbose sections
        assert "Sensors" not in text
        assert "AI Notes" not in text

    def test_emoji_indicators(self):
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": {
                "temperature_c": 24.5,
                "humidity_pct": 62.0,
                "co2_ppm": 450,
                "light_level": 780,
                "soil_moisture_pct": 45.0,
            },
            "decision": {
                "actions": [
                    {"action": "do_nothing", "reason": "All OK", "params": {}},
                ],
                "urgency": "normal",
                "notes": "",
            },
            "actions_taken": [{"action": "do_nothing", "executed": True}],
            "executed": True,
            "photo_path": None,
            "error": None,
            "mode": "live",
        }
        text = format_summary_text(summary)
        # Concise format: status bar has temp and urgency icon
        assert "24.5" in text
        assert "\U0001f7e2" in text  # green circle for normal urgency

    def test_safety_rejected_output(self):
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": None,
            "decision": None,
            "actions_taken": [{"action": "water", "executed": False, "safety_reason": "Rate limit exceeded"}],
            "executed": False,
            "photo_path": None,
            "error": None,
            "mode": "dry-run",
        }
        text = format_summary_text(summary)
        assert "❌" in text
        assert "Rate limit" in text

    def test_error_shown(self):
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": None,
            "decision": None,
            "actions_taken": [],
            "executed": False,
            "photo_path": None,
            "error": "Sensor read failed",
            "mode": "dry-run",
        }
        text = format_summary_text(summary)
        assert "Sensor read failed" in text

    def test_urgency_icons(self):
        for urgency, icon in [("normal", "\U0001f7e2"), ("attention", "\U0001f7e1"), ("critical", "\U0001f534")]:
            summary = {
                "timestamp": "2026-02-18T10:30:00+00:00",
                "sensor_data": None,
                "decision": {
                    "actions": [
                        {"action": "do_nothing", "reason": "test", "params": {}},
                    ],
                    "urgency": urgency,
                    "notes": "",
                },
                "actions_taken": [],
                "executed": False,
                "photo_path": None,
                "error": None,
                "mode": "dry-run",
            }
            text = format_summary_text(summary)
            assert icon in text


# ---------------------------------------------------------------------------
# run_check: sensor failure
# ---------------------------------------------------------------------------


class TestRunCheckSensorFailure:
    def test_sensor_failure_returns_error_summary(self):
        with patch("src.plant_agent.read_sensors_mock", side_effect=SensorReadError("hardware fault")), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE):

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        assert summary["error"] is not None
        assert "Sensor read failed" in summary["error"]
        assert summary["sensor_data"] is None
        assert summary["decision"] is None


# ---------------------------------------------------------------------------
# run_check: safety rejects action
# ---------------------------------------------------------------------------


class TestRunCheckSafetyRejection:
    def test_safety_rejects_action(self):
        rejection = _make_validation(
            valid=False,
            reason="Emergency stop is active",
            capped={"action": "water"},
        )

        with patch("src.plant_agent.read_sensors_mock", return_value=_make_sensor_data()), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE), \
             patch("src.plant_agent.ensure_plant_knowledge", return_value=""), \
             patch("src.plant_agent.get_plant_decision", return_value=SAMPLE_DECISION), \
             patch("src.plant_agent.validate_action", return_value=rejection), \
             patch("src.plant_agent.log_sensor_reading"), \
             patch("src.plant_agent.log_decision"), \
             patch("src.plant_agent.load_recent_decisions", return_value=[]), \
             patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}), \
             patch("src.plant_agent.update_after_action"), \
             patch("src.plant_agent.load_recent_plant_log", return_value=[]), \
             patch("src.plant_agent.log_plant_observations"), \
             patch("src.plant_agent.load_hardware_profile", return_value={}), \
             patch("src.plant_agent.ActionExecutor") as mock_exec_cls:

            mock_executor = MagicMock()
            mock_exec_cls.return_value = mock_executor

            summary = run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        assert summary["actions_taken"][0]["executed"] is False
        assert summary["executed"] is False
        # ActionExecutor.execute should NOT have been called
        mock_executor.execute.assert_not_called()

    def test_safety_rejection_logs_decision(self):
        rejection = _make_validation(
            valid=False,
            reason="Rate limit exceeded",
            capped={"action": "water"},
        )

        with patch("src.plant_agent.read_sensors_mock", return_value=_make_sensor_data()), \
             patch("src.plant_agent.load_plant_profile", return_value=SAMPLE_PROFILE), \
             patch("src.plant_agent.ensure_plant_knowledge", return_value=""), \
             patch("src.plant_agent.get_plant_decision", return_value=SAMPLE_DECISION), \
             patch("src.plant_agent.validate_action", return_value=rejection), \
             patch("src.plant_agent.log_sensor_reading"), \
             patch("src.plant_agent.log_decision") as mock_log, \
             patch("src.plant_agent.load_recent_decisions", return_value=[]), \
             patch("src.plant_agent.reconcile_actuator_state", return_value={"light": "off", "heater": "off", "pump": "idle", "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}), \
             patch("src.plant_agent.update_after_action"), \
             patch("src.plant_agent.load_recent_plant_log", return_value=[]), \
             patch("src.plant_agent.log_plant_observations"), \
             patch("src.plant_agent.ActionExecutor"):

            run_check(
                farmctl_path="/fake/farmctl.py",
                data_dir="/fake/data",
                dry_run=True,
                use_mock=True,
                include_photo=False,
            )

        # log_decision should still be called with executed=False
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        assert call_kwargs[1]["executed"] is False or call_kwargs[0][3] is False


# ---------------------------------------------------------------------------
# append_knowledge_update
# ---------------------------------------------------------------------------


class TestAppendKnowledgeUpdate:
    def test_creates_file_and_appends(self, tmp_path):
        """append_knowledge_update appends timestamped entry to plant_knowledge.md."""
        data_dir = str(tmp_path)
        append_knowledge_update("Basil prefers morning watering", data_dir)

        knowledge_path = tmp_path / "plant_knowledge.md"
        assert knowledge_path.exists()
        content = knowledge_path.read_text()
        assert "Basil prefers morning watering" in content
        assert "AI Update" in content

    def test_appends_multiple(self, tmp_path):
        """Multiple calls append multiple entries."""
        data_dir = str(tmp_path)
        append_knowledge_update("First insight", data_dir)
        append_knowledge_update("Second insight", data_dir)

        content = (tmp_path / "plant_knowledge.md").read_text()
        assert "First insight" in content
        assert "Second insight" in content


# ---------------------------------------------------------------------------
# format_summary_text: message and observations
# ---------------------------------------------------------------------------


class TestFormatSummaryTextNewFields:
    def test_message_included_in_concise(self):
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": {
                "temperature_c": 24.5,
                "humidity_pct": 62.0,
                "co2_ppm": 450,
                "light_level": 780,
                "soil_moisture_pct": 45.0,
            },
            "decision": {
                "actions": [{"action": "do_nothing", "reason": "All OK", "params": {}}],
                "urgency": "normal",
                "notes": "",
                "message": "Everything looks great today!",
            },
            "actions_taken": [{"action": "do_nothing", "executed": True}],
            "executed": True,
            "photo_path": None,
            "error": None,
            "mode": "live",
            "observations": [],
        }
        text = format_summary_text(summary)
        assert "Everything looks great today!" in text
        # Normal urgency = concise, no verbose sensor section
        assert "Sensors" not in text

    def test_verbose_on_attention_urgency(self):
        """Attention/critical urgency triggers full verbose output."""
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": {
                "temperature_c": 24.5,
                "humidity_pct": 62.0,
                "co2_ppm": 450,
                "light_level": 780,
                "soil_moisture_pct": 45.0,
            },
            "decision": {
                "actions": [{"action": "water", "reason": "Soil dry", "params": {}}],
                "urgency": "attention",
                "notes": "Watch closely",
                "message": "Soil is getting dry.",
            },
            "actions_taken": [{"action": "water", "executed": True}],
            "executed": True,
            "photo_path": None,
            "error": None,
            "mode": "live",
            "observations": ["Drying fast"],
        }
        text = format_summary_text(summary)
        assert "Sensors" in text
        assert "AI Notes" in text
        assert "Drying fast" in text

    def test_no_message_field_graceful(self):
        """No crash when message field is missing from decision."""
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": None,
            "decision": {
                "actions": [{"action": "do_nothing", "reason": "OK", "params": {}}],
                "urgency": "normal",
                "notes": "",
            },
            "actions_taken": [],
            "executed": False,
            "photo_path": None,
            "error": None,
            "mode": "dry-run",
            "observations": [],
        }
        text = format_summary_text(summary)
        assert isinstance(text, str)

    def test_no_observations_no_ai_notes(self):
        """AI Notes section omitted when no observations."""
        summary = {
            "timestamp": "2026-02-18T10:30:00+00:00",
            "sensor_data": None,
            "decision": {
                "actions": [],
                "urgency": "normal",
                "notes": "",
                "message": "",
            },
            "actions_taken": [],
            "executed": False,
            "photo_path": None,
            "error": None,
            "mode": "dry-run",
            "observations": [],
        }
        text = format_summary_text(summary)
        assert "AI Notes" not in text
