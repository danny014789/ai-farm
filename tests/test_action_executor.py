"""Tests for src/action_executor.py -- action execution via farmctl.py."""

import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from src.action_executor import (
    ActionExecutor,
    ExecutionResult,
    _ACTION_MAP,
    _NOOP_ACTIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FARMCTL_PATH = "/fake/farmctl.py"


def _make_executor(dry_run: bool = True) -> ActionExecutor:
    """Create an ActionExecutor for testing."""
    return ActionExecutor(FARMCTL_PATH, dry_run=dry_run)


# ---------------------------------------------------------------------------
# No-op actions (do_nothing, notify_human)
# ---------------------------------------------------------------------------


class TestNoopActions:
    def test_do_nothing_returns_success(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "do_nothing"})

        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.action == "do_nothing"
        assert result.command == ""
        assert "no hardware command" in result.output
        assert result.error is None

    def test_notify_human_returns_success(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "notify_human"})

        assert result.success is True
        assert result.action == "notify_human"
        assert result.command == ""
        assert "no hardware command" in result.output

    def test_noop_actions_set(self):
        assert "do_nothing" in _NOOP_ACTIONS
        assert "notify_human" in _NOOP_ACTIONS


# ---------------------------------------------------------------------------
# Unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    def test_unknown_action_returns_failure(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "explode"})

        assert result.success is False
        assert result.action == "explode"
        assert "Unknown action" in result.error
        assert result.command == ""

    def test_empty_action_returns_failure(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": ""})

        assert result.success is False

    def test_missing_action_key_returns_failure(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({})

        assert result.success is False


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_water_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "water", "params": {"duration_sec": 8}})

        assert result.success is True
        assert result.dry_run is True
        assert result.action == "water"
        assert "pump" in result.command
        assert "on" in result.command
        assert "--sec" in result.command
        assert "8" in result.command
        assert FARMCTL_PATH in result.command

    def test_light_on_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "light_on", "params": {}})

        assert result.success is True
        assert result.dry_run is True
        assert "light" in result.command
        assert "on" in result.command

    def test_heater_off_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "heater_off", "params": {}})

        assert result.success is True
        assert "heater" in result.command
        assert "off" in result.command

    def test_circulation_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "circulation", "params": {"duration_sec": 120}})

        assert result.success is True
        assert "circulation" in result.command
        assert "on" in result.command
        assert "--sec" in result.command
        assert "120" in result.command

    def test_light_off_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "light_off", "params": {}})

        assert result.success is True
        assert "light" in result.command
        assert "off" in result.command

    def test_heater_on_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "heater_on", "params": {}})

        assert result.success is True
        assert "heater" in result.command
        assert "on" in result.command

    def test_dry_run_output_prefix(self):
        executor = _make_executor(dry_run=True)
        result = executor.execute({"action": "water", "params": {"duration_sec": 5}})
        assert "[DRY-RUN]" in result.output

    def test_dry_run_does_not_call_subprocess(self):
        executor = _make_executor(dry_run=True)
        with patch("src.action_executor.subprocess.run") as mock_run:
            executor.execute({"action": "water", "params": {"duration_sec": 5}})
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _ACTION_MAP
# ---------------------------------------------------------------------------


class TestActionMap:
    def test_water_builds_correct_args(self):
        builder = _ACTION_MAP["water"]
        args = builder({"duration_sec": 15})
        assert args == ["pump", "on", "--sec", "15"]

    def test_water_default_duration(self):
        builder = _ACTION_MAP["water"]
        args = builder({})
        assert args == ["pump", "on", "--sec", "5"]

    def test_light_on_args(self):
        builder = _ACTION_MAP["light_on"]
        args = builder({})
        assert args == ["light", "on"]

    def test_light_off_args(self):
        builder = _ACTION_MAP["light_off"]
        args = builder({})
        assert args == ["light", "off"]

    def test_heater_on_args(self):
        builder = _ACTION_MAP["heater_on"]
        args = builder({})
        assert args == ["heater", "on"]

    def test_heater_off_args(self):
        builder = _ACTION_MAP["heater_off"]
        args = builder({})
        assert args == ["heater", "off"]

    def test_circulation_builds_correct_args(self):
        builder = _ACTION_MAP["circulation"]
        args = builder({"duration_sec": 120})
        assert args == ["circulation", "on", "--sec", "120"]

    def test_circulation_default_duration(self):
        builder = _ACTION_MAP["circulation"]
        args = builder({})
        assert args == ["circulation", "on", "--sec", "30"]


# ---------------------------------------------------------------------------
# take_photo
# ---------------------------------------------------------------------------


class TestTakePhoto:
    def test_take_photo_dry_run(self):
        executor = _make_executor(dry_run=True)
        result = executor.take_photo("/fake/data/plant_latest.jpg")

        assert result == "/fake/data/plant_latest.jpg"

    def test_take_photo_dry_run_does_not_call_subprocess(self):
        executor = _make_executor(dry_run=True)
        with patch("src.action_executor.subprocess.run") as mock_run:
            executor.take_photo("/fake/data/plant_latest.jpg")
        mock_run.assert_not_called()

    def test_take_photo_live_success(self):
        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="photo saved",
            stderr="",
        )
        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.take_photo("/fake/data/plant_latest.jpg")

        assert result == "/fake/data/plant_latest.jpg"

    def test_take_photo_live_failure(self):
        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="camera not found",
        )
        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.take_photo("/fake/data/plant_latest.jpg")

        assert result is None


# ---------------------------------------------------------------------------
# Live mode execution
# ---------------------------------------------------------------------------


class TestLiveMode:
    def test_live_execution_success(self):
        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="pump activated for 10s",
            stderr="",
        )
        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.execute({"action": "water", "params": {"duration_sec": 10}})

        assert result.success is True
        assert result.dry_run is False
        assert result.action == "water"
        assert "pump" in result.command
        assert result.output == "pump activated for 10s"
        assert result.error is None

    def test_live_execution_failure(self):
        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="relay communication error",
        )
        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.execute({"action": "water", "params": {"duration_sec": 10}})

        assert result.success is False
        assert result.dry_run is False
        assert result.error is not None
        assert "relay communication error" in result.error

    def test_live_execution_timeout(self):
        executor = _make_executor(dry_run=False)
        with patch(
            "src.action_executor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="farmctl", timeout=30),
        ):
            result = executor.execute({"action": "heater_on", "params": {}})

        assert result.success is False
        assert "timed out" in result.error

    def test_live_execution_file_not_found(self):
        executor = _make_executor(dry_run=False)
        with patch(
            "src.action_executor.subprocess.run",
            side_effect=FileNotFoundError("farmctl.py not found"),
        ):
            result = executor.execute({"action": "light_on", "params": {}})

        assert result.success is False
        assert "not found" in result.error


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------


class TestExecutionResult:
    def test_to_dict(self):
        er = ExecutionResult(
            success=True,
            action="water",
            command="farmctl pump on --sec 5",
            output="done",
            error=None,
            dry_run=True,
            timestamp="2026-02-18T10:30:00+00:00",
        )
        d = er.to_dict()
        assert isinstance(d, dict)
        assert d["success"] is True
        assert d["action"] == "water"
        assert d["dry_run"] is True


# ---------------------------------------------------------------------------
# take_photo_with_light — smart light & archival
# ---------------------------------------------------------------------------


class TestTakePhotoWithLight:
    """Tests for the consolidated take_photo_with_light method."""

    def _write_actuator_state(self, data_dir: str, light: str = "off"):
        """Write a minimal actuator_state.json for testing."""
        os.makedirs(data_dir, exist_ok=True)
        state = {"light": light, "heater": "off", "pump": "idle",
                 "circulation": "idle", "water_tank": "ok", "heater_lockout": "normal"}
        with open(os.path.join(data_dir, "actuator_state.json"), "w") as f:
            json.dump(state, f)

    def test_skips_light_toggle_when_already_on(self, tmp_path):
        """When light is already on, should not call light on/off."""
        data_dir = str(tmp_path / "data")
        self._write_actuator_state(data_dir, light="on")
        output_path = str(tmp_path / "photo.jpg")

        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        calls = []
        original_run = subprocess.run

        def tracking_run(cmd, **kwargs):
            calls.append(cmd)
            return mock_result

        with patch("src.action_executor.subprocess.run", side_effect=tracking_run):
            result = executor.take_photo_with_light(
                output_path=output_path,
                data_dir=data_dir,
            )

        assert result == output_path
        # Should only have 1 call: camera-snap (no light on, no light off)
        assert len(calls) == 1
        assert "camera-snap" in calls[0]

    def test_toggles_light_when_off(self, tmp_path):
        """When light is off, should call light on, camera, light off."""
        data_dir = str(tmp_path / "data")
        self._write_actuator_state(data_dir, light="off")
        output_path = str(tmp_path / "photo.jpg")

        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        calls = []

        def tracking_run(cmd, **kwargs):
            calls.append(cmd)
            return mock_result

        with patch("src.action_executor.subprocess.run", side_effect=tracking_run):
            with patch("time.sleep"):
                result = executor.take_photo_with_light(
                    output_path=output_path,
                    data_dir=data_dir,
                )

        assert result == output_path
        # 3 calls: light on, camera-snap, light off
        assert len(calls) == 3
        assert "light" in calls[0] and "on" in calls[0]
        assert "camera-snap" in calls[1]
        assert "light" in calls[2] and "off" in calls[2]

    def test_archives_photo_with_timestamp(self, tmp_path):
        """When photos_dir is set, should copy photo to timestamped archive."""
        data_dir = str(tmp_path / "data")
        self._write_actuator_state(data_dir, light="on")
        photos_dir = str(tmp_path / "data" / "photos")
        output_path = str(tmp_path / "photo.jpg")

        # Create a fake photo file so shutil.copy2 works
        with open(output_path, "wb") as f:
            f.write(b"fake jpeg data")

        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.take_photo_with_light(
                output_path=output_path,
                data_dir=data_dir,
                photos_dir=photos_dir,
            )

        assert result == output_path
        # Check archive directory was created and has a file
        assert os.path.isdir(photos_dir)
        archived = os.listdir(photos_dir)
        assert len(archived) == 1
        assert archived[0].startswith("plant_")
        assert archived[0].endswith(".jpg")

    def test_no_archive_when_photos_dir_not_set(self, tmp_path):
        """When photos_dir is None, no archival should happen."""
        data_dir = str(tmp_path / "data")
        self._write_actuator_state(data_dir, light="on")
        output_path = str(tmp_path / "photo.jpg")

        executor = _make_executor(dry_run=False)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        with patch("src.action_executor.subprocess.run", return_value=mock_result):
            result = executor.take_photo_with_light(
                output_path=output_path,
                data_dir=data_dir,
                photos_dir=None,
            )

        assert result == output_path
        # No photos directory should be created
        assert not os.path.exists(os.path.join(data_dir, "photos"))

    def test_dry_run_skips_light_check(self, tmp_path):
        """In dry-run mode without data_dir, should still toggle light."""
        output_path = str(tmp_path / "photo.jpg")
        executor = _make_executor(dry_run=True)

        result = executor.take_photo_with_light(
            output_path=output_path,
            data_dir=None,
        )

        assert result == output_path
