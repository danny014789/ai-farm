"""Sensor reader for plant-ops-ai.

Reads sensor data from farmctl.py via subprocess, parses JSON output.
Includes mock mode for local development without hardware.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SensorData:
    """Sensor readings from the Arduino via farmctl.py."""

    temperature_c: float
    humidity_pct: float
    co2_ppm: int
    light_level: int
    soil_moisture_pct: float
    timestamp: str

    def to_dict(self) -> dict:
        """Convert to a plain dict for serialization."""
        return asdict(self)


class SensorReadError(Exception):
    """Raised when sensor reading fails after all retry attempts."""

    pass


def read_sensors(
    farmctl_path: str,
    attempts: int = 3,
    read_seconds: float = 2.0,
) -> SensorData:
    """Read current sensor data by calling farmctl.py status --json.

    Retries on failure (port busy, timeout, parse error). Each attempt
    uses a fresh subprocess call.

    Args:
        farmctl_path: Path to the farmctl.py script.
        attempts: Number of retry attempts before giving up.
        read_seconds: Seconds to wait for farmctl.py to respond.

    Returns:
        Parsed sensor data.

    Raises:
        SensorReadError: If all attempts fail.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                ["python3", farmctl_path, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=read_seconds + 5.0,  # extra buffer beyond read time
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                raise SensorReadError(
                    f"farmctl.py exited with code {result.returncode}: {stderr}"
                )

            raw = result.stdout.strip()
            if not raw:
                raise SensorReadError("farmctl.py returned empty output")

            data = json.loads(raw)
            return _parse_sensor_json(data)

        except subprocess.TimeoutExpired:
            last_error = SensorReadError(
                f"farmctl.py timed out after {read_seconds + 5.0}s"
            )
            logger.warning("Sensor read attempt %d/%d: timeout", attempt, attempts)

        except json.JSONDecodeError as e:
            last_error = SensorReadError(f"Failed to parse farmctl.py JSON output: {e}")
            logger.warning(
                "Sensor read attempt %d/%d: parse error: %s", attempt, attempts, e
            )

        except SensorReadError as e:
            last_error = e
            logger.warning(
                "Sensor read attempt %d/%d: %s", attempt, attempts, e
            )

        except OSError as e:
            # Covers file not found, permission denied, port busy, etc.
            last_error = SensorReadError(f"OS error calling farmctl.py: {e}")
            logger.warning(
                "Sensor read attempt %d/%d: OS error: %s", attempt, attempts, e
            )

    raise SensorReadError(
        f"All {attempts} sensor read attempts failed. Last error: {last_error}"
    )


def _parse_sensor_json(data: dict) -> SensorData:
    """Parse and validate raw JSON dict into SensorData.

    Args:
        data: Raw dict from farmctl.py JSON output.

    Returns:
        Validated SensorData.

    Raises:
        SensorReadError: If required fields are missing or invalid.
    """
    required_fields = [
        "temperature_c",
        "humidity_pct",
        "co2_ppm",
        "light_level",
        "soil_moisture_pct",
    ]

    missing = [f for f in required_fields if f not in data]
    if missing:
        raise SensorReadError(f"Missing sensor fields: {missing}")

    try:
        return SensorData(
            temperature_c=float(data["temperature_c"]),
            humidity_pct=float(data["humidity_pct"]),
            co2_ppm=int(data["co2_ppm"]),
            light_level=int(data["light_level"]),
            soil_moisture_pct=float(data["soil_moisture_pct"]),
            timestamp=data.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except (ValueError, TypeError) as e:
        raise SensorReadError(f"Invalid sensor data types: {e}") from e


def read_sensors_mock() -> SensorData:
    """Return mock sensor data for local development testing.

    Produces realistic mid-range values suitable for testing the
    decision pipeline without real hardware.

    Returns:
        SensorData with plausible mock values.
    """
    return SensorData(
        temperature_c=24.5,
        humidity_pct=62.0,
        co2_ppm=450,
        light_level=780,
        soil_moisture_pct=45.0,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
