"""Action executor for plant-ops-ai.

Executes validated actions by calling farmctl.py commands via subprocess.
Acts as the bridge between AI decisions and physical hardware (relays,
pump, lights, heater, circulation fan, camera).

Supports dry-run mode for local development and testing without hardware.
"""

import logging
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of executing a single action via farmctl.py.

    Attributes:
        success: Whether the action completed without error.
        action: The action name that was executed (e.g. "water", "light_on").
        command: The full shell command string that was (or would be) run.
        output: Stdout from farmctl.py, or a descriptive message for no-ops.
        error: Error message if the action failed, None on success.
        dry_run: Whether this was a simulated execution.
        timestamp: ISO 8601 timestamp of when the action was executed.
    """

    success: bool
    action: str
    command: str
    output: str
    error: str | None
    dry_run: bool
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for serialization."""
        return asdict(self)


# Maps action names to farmctl.py argument builders.
# Each value is a callable: (params: dict) -> list[str]
# The returned list is appended to ["python3", farmctl_path].
_ACTION_MAP: dict[str, Any] = {
    "water": lambda p: ["pump", "on", "--sec", str(p.get("duration_sec", 5))],
    "light_on": lambda _: ["light", "on"],
    "light_off": lambda _: ["light", "off"],
    "heater_on": lambda _: ["heater", "on"],
    "heater_off": lambda _: ["heater", "off"],
    "circulation": lambda p: [
        "circulation",
        "on",
        "--sec",
        str(p.get("duration_sec", 30)),
    ],
}

# Actions that require no hardware command.
_NOOP_ACTIONS = frozenset({"do_nothing", "notify_human"})


class ActionExecutor:
    """Executes plant-care actions by calling farmctl.py as a subprocess.

    Args:
        farmctl_path: Absolute path to the farmctl.py script on the Pi.
        dry_run: If True, log what would happen but skip subprocess calls.
    """

    def __init__(self, farmctl_path: str, dry_run: bool = False) -> None:
        self._farmctl_path = farmctl_path
        self._dry_run = dry_run

        if dry_run:
            logger.info("ActionExecutor initialised in DRY-RUN mode")
        else:
            logger.info("ActionExecutor initialised with farmctl: %s", farmctl_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, action: dict[str, Any]) -> ExecutionResult:
        """Execute a validated action dict.

        The action dict is produced by the AI decision parser and has
        already passed safety checks before reaching this method.

        Args:
            action: Dict with keys ``"action"`` (str) and ``"params"`` (dict).
                Example::

                    {"action": "water", "params": {"duration_sec": 8}}

        Returns:
            ExecutionResult describing what happened.
        """
        action_name: str = action.get("action", "")
        params: dict[str, Any] = action.get("params", {})
        now = datetime.now(timezone.utc).isoformat()

        # --- No-op actions (do_nothing, notify_human) -----------------
        if action_name in _NOOP_ACTIONS:
            logger.info("Action '%s' requires no hardware command", action_name)
            return ExecutionResult(
                success=True,
                action=action_name,
                command="",
                output=f"{action_name}: no hardware command required",
                error=None,
                dry_run=self._dry_run,
                timestamp=now,
            )

        # --- Look up the farmctl argument builder ---------------------
        builder = _ACTION_MAP.get(action_name)
        if builder is None:
            logger.error("Unknown action: '%s'", action_name)
            return ExecutionResult(
                success=False,
                action=action_name,
                command="",
                output="",
                error=f"Unknown action: '{action_name}'",
                dry_run=self._dry_run,
                timestamp=now,
            )

        farmctl_args: list[str] = builder(params)
        command_str = f"python3 {self._farmctl_path} {' '.join(farmctl_args)}"

        # --- Dry-run mode: log but don't execute ---------------------
        if self._dry_run:
            logger.info("[DRY-RUN] Would execute: %s", command_str)
            return ExecutionResult(
                success=True,
                action=action_name,
                command=command_str,
                output=f"[DRY-RUN] {command_str}",
                error=None,
                dry_run=True,
                timestamp=now,
            )

        # --- Live execution ------------------------------------------
        logger.info("Executing: %s", command_str)
        success, output_or_error = self._run_farmctl(farmctl_args)

        if success:
            logger.info("Action '%s' completed: %s", action_name, output_or_error)
        else:
            logger.error("Action '%s' failed: %s", action_name, output_or_error)

        return ExecutionResult(
            success=success,
            action=action_name,
            command=command_str,
            output=output_or_error if success else "",
            error=output_or_error if not success else None,
            dry_run=False,
            timestamp=now,
        )

    def take_photo(self, output_path: str) -> str | None:
        """Capture a plant photo via farmctl.py camera-snap.

        Args:
            output_path: Filesystem path where the image should be saved.

        Returns:
            The photo path on success, or None on failure.
        """
        args = ["camera-snap", "--out", output_path, "--json"]

        if self._dry_run:
            cmd = f"python3 {self._farmctl_path} {' '.join(args)}"
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            return output_path

        logger.info("Taking photo -> %s", output_path)
        success, output_or_error = self._run_farmctl(args, timeout=30)

        if success:
            logger.info("Photo saved: %s", output_path)
            return output_path

        logger.error("Photo capture failed: %s", output_or_error)
        return None

    def take_photo_with_light(
        self,
        output_path: str,
        data_dir: str | None = None,
        settle_time: float = 2.0,
    ) -> str | None:
        """Turn on light, take photo, turn off light.

        Ensures the plant is illuminated for the photo. Resilient to
        partial failures: if light_on fails, still attempts the photo;
        if the photo fails, still turns the light off.

        Args:
            output_path: Filesystem path where the image should be saved.
            data_dir: If provided, update actuator_state.json after
                light_on and light_off commands.
            settle_time: Seconds to wait after light_on for brightness
                to stabilize.

        Returns:
            The photo path on success, or None on failure.
        """
        import time
        from src.actuator_state import update_after_action

        # --- Step 1: Turn light on ---
        light_on_ok = False
        light_on_args = ["light", "on"]
        if self._dry_run:
            cmd = f"python3 {self._farmctl_path} {' '.join(light_on_args)}"
            logger.info("[DRY-RUN] Would execute: %s", cmd)
            light_on_ok = True
        else:
            logger.info("Turning light on for photo capture")
            success, msg = self._run_farmctl(light_on_args)
            light_on_ok = success
            if success:
                if data_dir:
                    update_after_action("light_on", data_dir)
            else:
                logger.warning("light_on failed before photo, continuing: %s", msg)

        # --- Step 2: Wait for light to stabilize ---
        if light_on_ok and not self._dry_run:
            time.sleep(settle_time)

        # --- Step 3: Take photo ---
        photo_path = self.take_photo(output_path)

        # --- Step 4: Turn light off ---
        light_off_args = ["light", "off"]
        if self._dry_run:
            cmd = f"python3 {self._farmctl_path} {' '.join(light_off_args)}"
            logger.info("[DRY-RUN] Would execute: %s", cmd)
        else:
            logger.info("Turning light off after photo capture")
            success, msg = self._run_farmctl(light_off_args)
            if success:
                if data_dir:
                    update_after_action("light_off", data_dir)
            else:
                logger.warning("light_off failed after photo: %s", msg)

        return photo_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_farmctl(
        self, args: list[str], timeout: int = 30
    ) -> tuple[bool, str]:
        """Call farmctl.py with the given arguments via subprocess.

        Args:
            args: Arguments to pass after ``python3 farmctl.py``.
            timeout: Maximum seconds to wait before killing the process.

        Returns:
            Tuple of (success, output_or_error). On success the second
            element is stdout; on failure it is a human-readable error
            description.
        """
        cmd = ["python3", self._farmctl_path] + args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                return False, f"farmctl.py exited with code {result.returncode}: {stderr}"

            return True, result.stdout.strip()

        except subprocess.TimeoutExpired:
            logger.error("farmctl.py timed out after %ds: %s", timeout, cmd)
            return False, f"farmctl.py timed out after {timeout}s"

        except FileNotFoundError:
            logger.error("farmctl.py not found at: %s", self._farmctl_path)
            return False, f"farmctl.py not found at: {self._farmctl_path}"

        except OSError as exc:
            logger.error("OS error calling farmctl.py: %s", exc)
            return False, f"OS error calling farmctl.py: {exc}"
