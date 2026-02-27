"""Tests for src/safety.py -- safety validation module."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.safety import (
    ALLOWED_ACTIONS,
    ValidationResult,
    check_emergency_stop,
    validate_action,
)
from src.sensor_reader import SensorData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAFETY_LIMITS = {
    "water": {
        "max_duration_sec": 30,
        "min_interval_min": 30,
        "daily_max_count": 12,
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


def _make_sensor_data(**overrides) -> SensorData:
    """Create a SensorData with sensible defaults, overridable per field."""
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


def _mock_load_limits():
    """Patch target for src.safety._load_limits."""
    return SAFETY_LIMITS


def _now_iso(minutes_ago: int = 0) -> str:
    """Return an ISO timestamp for (now - minutes_ago) in UTC."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat()


def _make_entry(action_type: str, minutes_ago: int = 0) -> dict:
    """Create a decision log entry in the format produced by logger.log_decision."""
    return {
        "timestamp": _now_iso(minutes_ago),
        "decision": {"action": action_type},
    }


# All tests patch _load_limits so no real config file is read.
@pytest.fixture(autouse=True)
def _patch_load_limits():
    with patch("src.safety._load_limits", side_effect=_mock_load_limits):
        yield


# ---------------------------------------------------------------------------
# ALLOWED_ACTIONS set
# ---------------------------------------------------------------------------


class TestAllowedActions:
    """Verify the ALLOWED_ACTIONS set contains the expected entries."""

    def test_expected_actions_present(self):
        expected = {
            "water",
            "light_on",
            "light_off",
            "heater_on",
            "heater_off",
            "circulation",
            "do_nothing",
            "notify_human",
        }
        assert ALLOWED_ACTIONS == expected

    def test_no_unexpected_actions(self):
        """Ensure no surprise entries are in the set."""
        assert len(ALLOWED_ACTIONS) == 8


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------


class TestEmergencyStop:
    """Emergency stop blocks all actions when the stop file exists."""

    def test_emergency_stop_blocks_action(self, tmp_path):
        stop_file = tmp_path / "emergency-stop"
        stop_file.touch()

        limits = dict(SAFETY_LIMITS)
        limits["emergency_stop_file"] = str(stop_file)

        with patch("src.safety._load_limits", return_value=limits):
            result = validate_action(
                {"action": "water", "duration_sec": 10},
                _make_sensor_data(),
                [],
            )

        assert result.valid is False
        assert "Emergency stop" in result.reason

    def test_emergency_stop_blocks_do_nothing(self, tmp_path):
        """Even do_nothing is blocked when emergency stop is active."""
        stop_file = tmp_path / "emergency-stop"
        stop_file.touch()

        limits = dict(SAFETY_LIMITS)
        limits["emergency_stop_file"] = str(stop_file)

        with patch("src.safety._load_limits", return_value=limits):
            result = validate_action(
                {"action": "do_nothing"},
                _make_sensor_data(),
                [],
            )

        assert result.valid is False
        assert "Emergency stop" in result.reason

    def test_no_emergency_stop_allows_action(self, tmp_path):
        """When stop file does NOT exist, actions proceed normally."""
        limits = dict(SAFETY_LIMITS)
        limits["emergency_stop_file"] = str(tmp_path / "nonexistent-stop")

        with patch("src.safety._load_limits", return_value=limits):
            result = validate_action(
                {"action": "do_nothing"},
                _make_sensor_data(),
                [],
            )

        assert result.valid is True

    def test_check_emergency_stop_function(self, tmp_path):
        stop_file = tmp_path / "stop"
        stop_file.touch()
        limits = {"emergency_stop_file": str(stop_file)}
        assert check_emergency_stop(limits) is True

    def test_check_emergency_stop_no_file(self, tmp_path):
        limits = {"emergency_stop_file": str(tmp_path / "no-such-file")}
        assert check_emergency_stop(limits) is False


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    def test_unknown_action_rejected(self):
        result = validate_action(
            {"action": "self_destruct"},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False
        assert "not in the allowlist" in result.reason

    def test_empty_action_rejected(self):
        result = validate_action(
            {"action": ""},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False

    def test_missing_action_key_rejected(self):
        result = validate_action(
            {},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False


# ---------------------------------------------------------------------------
# Passthrough actions (do_nothing, notify_human)
# ---------------------------------------------------------------------------


class TestPassthroughActions:
    """do_nothing and notify_human always pass after emergency stop check."""

    def test_do_nothing_valid(self):
        result = validate_action(
            {"action": "do_nothing"},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.reason == "OK"

    def test_notify_human_valid(self):
        result = validate_action(
            {"action": "notify_human"},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.reason == "OK"

    def test_passthrough_not_counted_in_rate_limit(self):
        """do_nothing/notify_human should not count against the hourly rate limit."""
        # Fill history with 10 do_nothing entries
        history = [_make_entry("do_nothing", i) for i in range(10)]
        result = validate_action(
            {"action": "water", "duration_sec": 5},
            _make_sensor_data(),
            history,
        )
        # Should pass because do_nothing doesn't count as a real action
        assert result.valid is True


# ---------------------------------------------------------------------------
# Global rate limit
# ---------------------------------------------------------------------------


class TestGlobalRateLimit:
    def test_rate_limit_exceeded(self):
        """When 10 real actions are in the last hour, the next is rejected."""
        history = [_make_entry("water", i) for i in range(10)]
        result = validate_action(
            {"action": "water", "duration_sec": 5},
            _make_sensor_data(),
            history,
        )
        assert result.valid is False
        assert "rate limit" in result.reason.lower()

    def test_rate_limit_not_exceeded(self):
        """Under the limit, actions are allowed."""
        history = [_make_entry("water", i) for i in range(5)]
        result = validate_action(
            {"action": "water", "duration_sec": 5},
            _make_sensor_data(),
            history,
        )
        # May still be rejected by water-specific min_interval, but not global rate limit
        # We just verify it doesn't fail on global rate limit
        if not result.valid:
            assert "Global rate limit" not in result.reason


# ---------------------------------------------------------------------------
# Water validation
# ---------------------------------------------------------------------------


class TestWaterValidation:
    def test_water_duration_capped(self):
        """Duration above max_duration_sec is capped to 30."""
        result = validate_action(
            {"action": "water", "duration_sec": 60},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 30
        assert result.capped_action.get("_capped") is True

    def test_water_duration_within_limit(self):
        """Duration within limit is not modified."""
        result = validate_action(
            {"action": "water", "duration_sec": 15},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 15

    def test_water_zero_duration_rejected(self):
        result = validate_action(
            {"action": "water", "duration_sec": 0},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False
        assert "positive" in result.reason.lower()

    def test_water_negative_duration_rejected(self):
        result = validate_action(
            {"action": "water", "duration_sec": -5},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False
        assert "positive" in result.reason.lower()

    def test_water_min_interval_check(self):
        """Reject water if last watering was within min_interval_min (30)."""
        history = [_make_entry("water", 15)]  # 15 min ago
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            _make_sensor_data(),
            history,
        )
        assert result.valid is False
        assert "rate limit" in result.reason.lower() or "wait" in result.reason.lower()

    def test_water_not_locked_when_previous_was_blocked(self):
        """A water action that was blocked (executed=False) must not consume the lock."""
        blocked_entry = {**_make_entry("water", 15), "executed": False}
        history = [blocked_entry]
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            _make_sensor_data(),
            history,
        )
        assert result.valid is True

    def test_water_after_interval_ok(self):
        """Allow water when last watering was more than min_interval ago."""
        history = [_make_entry("water", 45)]  # 45 min ago — past the 30-min window
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            _make_sensor_data(),
            history,
        )
        assert result.valid is True

    def test_water_rejected_when_tank_low(self):
        """Watering is blocked when water tank level is LOW."""
        sensor = _make_sensor_data(water_tank_ok=False)
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            sensor,
            [],
        )
        assert result.valid is False
        assert "tank" in result.reason.lower()

    def test_water_allowed_when_tank_ok(self):
        """Watering is allowed when water tank level is OK."""
        sensor = _make_sensor_data(water_tank_ok=True)
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            sensor,
            [],
        )
        assert result.valid is True

    def test_water_allowed_when_tank_unknown(self):
        """Watering is allowed when water tank status is unknown (None)."""
        sensor = _make_sensor_data()  # water_tank_ok defaults to None
        result = validate_action(
            {"action": "water", "duration_sec": 10},
            sensor,
            [],
        )
        assert result.valid is True

    def test_water_daily_max_count(self):
        """Reject water when daily_max_count (12) is reached."""
        # All 12 waterings are from today but > 30 min apart from each other and from now.
        # Use today's midnight as anchor so entries are definitely today (UTC).
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        history = [
            {
                "timestamp": (today + timedelta(minutes=i * 40)).isoformat(),
                "decision": {"action": "water"},
            }
            for i in range(12)
        ]

        result = validate_action(
            {"action": "water", "duration_sec": 10},
            _make_sensor_data(),
            history,
        )
        # Should be rejected either by daily max or min_interval
        assert result.valid is False


# ---------------------------------------------------------------------------
# Heater validation
# ---------------------------------------------------------------------------


class TestHeaterValidation:
    def test_heater_on_rejected_when_temp_above_max(self):
        """Cannot turn heater on when temp >= max_temp_c (30)."""
        sensor = _make_sensor_data(temperature_c=31.0)
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is False
        assert "temp" in result.reason.lower()

    def test_heater_on_rejected_at_exact_max(self):
        """Reject heater_on when temp == max_temp_c."""
        sensor = _make_sensor_data(temperature_c=30.0)
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is False

    def test_heater_on_allowed_below_max(self):
        """Allow heater_on when temp < max_temp_c."""
        sensor = _make_sensor_data(temperature_c=20.0)
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is True

    def test_heater_on_rejected_when_lockout_active(self):
        """Cannot turn heater on when firmware lockout is active."""
        sensor = _make_sensor_data(temperature_c=20.0, heater_lockout=True)
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is False
        assert "lockout" in result.reason.lower()

    def test_heater_on_allowed_when_lockout_inactive(self):
        """Heater on is allowed when lockout is not active."""
        sensor = _make_sensor_data(temperature_c=20.0, heater_lockout=False)
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is True

    def test_heater_on_allowed_when_lockout_unknown(self):
        """Heater on is allowed when lockout status is unknown (None)."""
        sensor = _make_sensor_data(temperature_c=20.0)  # heater_lockout defaults to None
        result = validate_action(
            {"action": "heater_on"},
            sensor,
            [],
        )
        assert result.valid is True

    def test_heater_off_always_allowed(self):
        """heater_off is always allowed regardless of temperature."""
        sensor = _make_sensor_data(temperature_c=10.0)
        result = validate_action(
            {"action": "heater_off"},
            sensor,
            [],
        )
        assert result.valid is True

    def test_heater_off_allowed_even_when_hot(self):
        sensor = _make_sensor_data(temperature_c=35.0)
        result = validate_action(
            {"action": "heater_off"},
            sensor,
            [],
        )
        assert result.valid is True


# ---------------------------------------------------------------------------
# Light validation
# ---------------------------------------------------------------------------


class TestLightValidation:
    def test_light_off_always_allowed(self):
        """light_off is always allowed."""
        result = validate_action(
            {"action": "light_off"},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True

    def test_light_on_rejected_outside_schedule(self):
        """light_on rejected when current time is before schedule_on."""
        # Mock datetime.now to return 04:00 (before 06:00 schedule_on)
        with patch("src.safety.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 18, 4, 0, 0)
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # We need to handle the now(timezone.utc) call in validate_action too
            mock_now_utc = datetime(2026, 2, 18, 4, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.side_effect = lambda tz=None: (
                mock_now_utc if tz else datetime(2026, 2, 18, 4, 0, 0)
            )

            result = validate_action(
                {"action": "light_on"},
                _make_sensor_data(),
                [],
            )

        assert result.valid is False
        assert "early" in result.reason.lower() or "time" in result.reason.lower()

    def test_light_on_allowed_during_schedule(self):
        """light_on allowed when current time is within schedule."""
        # Mock datetime.now to return 12:00 (within 06:00-24:00)
        with patch("src.safety.datetime") as mock_dt:
            mock_now_utc = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.now.side_effect = lambda tz=None: (
                mock_now_utc if tz else datetime(2026, 2, 18, 12, 0, 0)
            )
            mock_dt.fromisoformat = datetime.fromisoformat

            result = validate_action(
                {"action": "light_on"},
                _make_sensor_data(),
                [],
            )

        assert result.valid is True

    def test_light_on_rejected_after_schedule_off(self):
        """light_on rejected when current time is after schedule_off (if not 24:00)."""
        # Use a schedule_off that's not "24:00"
        limits_with_early_off = dict(SAFETY_LIMITS)
        limits_with_early_off["light"] = {
            "max_hours_per_day": 18,
            "schedule_on": "06:00",
            "schedule_off": "20:00",
        }

        with patch("src.safety._load_limits", return_value=limits_with_early_off):
            with patch("src.safety.datetime") as mock_dt:
                mock_now_utc = datetime(2026, 2, 18, 21, 0, 0, tzinfo=timezone.utc)
                mock_dt.now.side_effect = lambda tz=None: (
                    mock_now_utc if tz else datetime(2026, 2, 18, 21, 0, 0)
                )
                mock_dt.fromisoformat = datetime.fromisoformat

                result = validate_action(
                    {"action": "light_on"},
                    _make_sensor_data(),
                    [],
                )

        assert result.valid is False
        assert "late" in result.reason.lower() or "time" in result.reason.lower()


# ---------------------------------------------------------------------------
# Circulation validation
# ---------------------------------------------------------------------------


class TestCirculationValidation:
    def test_circulation_duration_capped(self):
        """Duration above max_duration_sec (300) is capped."""
        result = validate_action(
            {"action": "circulation", "duration_sec": 600},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 300
        assert result.capped_action.get("_capped") is True

    def test_circulation_duration_within_limit(self):
        result = validate_action(
            {"action": "circulation", "duration_sec": 120},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 120

    def test_circulation_zero_duration_rejected(self):
        result = validate_action(
            {"action": "circulation", "duration_sec": 0},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False
        assert "positive" in result.reason.lower()

    def test_circulation_negative_duration_rejected(self):
        result = validate_action(
            {"action": "circulation", "duration_sec": -10},
            _make_sensor_data(),
            [],
        )
        assert result.valid is False

    def test_circulation_no_rate_limit(self):
        """Circulation has no min-interval rate limit; allowed even if run recently."""
        history = [_make_entry("circulation", 15)]  # 15 min ago
        result = validate_action(
            {"action": "circulation", "duration_sec": 60},
            _make_sensor_data(),
            history,
        )
        assert result.valid is True

    def test_circulation_after_interval_ok(self):
        history = [_make_entry("circulation", 45)]  # 45 min ago
        result = validate_action(
            {"action": "circulation", "duration_sec": 60},
            _make_sensor_data(),
            history,
        )
        assert result.valid is True


# ---------------------------------------------------------------------------
# Params flattening
# ---------------------------------------------------------------------------


class TestParamsFlattening:
    """Claude sends {"action": "water", "params": {"duration_sec": 8}}.
    validate_action should flatten params to top level."""

    def test_params_flattened_for_water(self):
        result = validate_action(
            {"action": "water", "params": {"duration_sec": 8}},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 8
        assert result.capped_action["action"] == "water"

    def test_params_flattened_for_circulation(self):
        result = validate_action(
            {"action": "circulation", "params": {"duration_sec": 60}},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 60

    def test_explicit_params_not_overwritten(self):
        """If duration_sec exists at top level AND in params, top-level wins."""
        result = validate_action(
            {
                "action": "water",
                "duration_sec": 10,
                "params": {"duration_sec": 25},
            },
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        # setdefault means top-level key (10) is preserved, not overwritten by params (25)
        assert result.capped_action["duration_sec"] == 10

    def test_params_flattening_with_capping(self):
        """Params are flattened, then duration is capped."""
        result = validate_action(
            {"action": "water", "params": {"duration_sec": 50}},
            _make_sensor_data(),
            [],
        )
        assert result.valid is True
        assert result.capped_action["duration_sec"] == 30  # capped from 50


# ---------------------------------------------------------------------------
# ValidationResult dataclass
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_validation_result_fields(self):
        vr = ValidationResult(valid=True, reason="OK", capped_action={"action": "water"})
        assert vr.valid is True
        assert vr.reason == "OK"
        assert vr.capped_action == {"action": "water"}

    def test_validation_result_invalid(self):
        vr = ValidationResult(valid=False, reason="blocked", capped_action={})
        assert vr.valid is False
        assert vr.reason == "blocked"
