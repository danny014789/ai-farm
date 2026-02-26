"""Sensor reader for plant-ops-ai.

Reads sensor data from farmctl.py via subprocess, parses JSON output.
Includes mock mode for local development without hardware.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Soil moisture exponential calibration (log-linear fit).
# Fit method: ln(moisture_pct) ~ slope * adc + intercept, then exponentiate.
# Least-squares fit to 9 measured points (soil_moisture_calibration_curve.xlsx).
# R² = 0.916 in original space (vs 0.836 for a plain linear fit).
#   moisture_pct = exp(SOIL_CAL_LOG_SLOPE * adc + SOIL_CAL_LOG_INTERCEPT)
# Result is clamped to [0, 100] % for ADC values outside the physical range.
SOIL_CAL_LOG_SLOPE: float = -0.00258653
SOIL_CAL_LOG_INTERCEPT: float = 4.91733458

# Measured calibration range (from soil_moisture_calibration_curve.xlsx).
# ADC readings outside this range are extrapolated; accuracy degrades noticeably
# below ADC ~390 (wettest measured point, 55.99%) where the curve underestimates
# by up to ~10 percentage points. Values clamped to 100% indicate the sensor is
# saturated or beyond the measurable range.
SOIL_CAL_ADC_MIN: float = 390.0   # wettest calibrated point → 55.99%
SOIL_CAL_ADC_MAX: float = 822.0   # driest calibrated point  → 18.31%


def _soil_adc_to_pct(adc: float) -> float:
    """Convert a raw ADC reading to soil moisture % using the exponential calibration.

    Applies  moisture = exp(SOIL_CAL_LOG_SLOPE * adc + SOIL_CAL_LOG_INTERCEPT),
    then clamps to [0, 100].

    Logs a warning when *adc* is outside the measured calibration range
    (390–822); extrapolated values are less reliable.
    """
    pct = math.exp(SOIL_CAL_LOG_SLOPE * adc + SOIL_CAL_LOG_INTERCEPT)
    if adc < SOIL_CAL_ADC_MIN:
        logger.warning(
            "Soil ADC %g is below the calibration range (min measured: %g → 55.99%%). "
            "Extrapolated moisture %.1f%% may underestimate actual moisture by up to ~10%%.",
            adc, SOIL_CAL_ADC_MIN, min(pct, 100.0),
        )
    elif adc > SOIL_CAL_ADC_MAX:
        logger.warning(
            "Soil ADC %g is above the calibration range (max measured: %g → 18.31%%). "
            "Extrapolated moisture %.1f%% may underestimate actual dryness.",
            adc, SOIL_CAL_ADC_MAX, max(pct, 0.0),
        )
    return max(0.0, min(100.0, pct))


@dataclass
class SensorData:
    """Sensor readings and hardware state from the Arduino via farmctl.py."""

    # --- Sensor readings (always present) ---
    temperature_c: float
    humidity_pct: float
    co2_ppm: int
    light_level: int
    soil_moisture_pct: float
    timestamp: str

    # --- Hardware state (None when unavailable, e.g., mock mode or old firmware) ---
    water_tank_ok: Optional[bool] = None
    light_on: Optional[bool] = None
    heater_on: Optional[bool] = None
    heater_lockout: Optional[bool] = None
    water_pump_on: Optional[bool] = None
    circulation_on: Optional[bool] = None
    water_pump_remaining_sec: Optional[int] = None
    circulation_remaining_sec: Optional[int] = None

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

    Handles field name mapping from farmctl.py output format:
        farmctl.py          ->  SensorData
        temp_c              ->  temperature_c
        humidity_pct        ->  humidity_pct
        co2_ppm             ->  co2_ppm
        light_raw           ->  light_level
        soil_raw (0-1023)   ->  soil_moisture_pct (0-100%)

    Also accepts the canonical SensorData field names directly, so mock
    data and pre-mapped dicts still work.

    Args:
        data: Raw dict from farmctl.py JSON output.

    Returns:
        Validated SensorData.

    Raises:
        SensorReadError: If required fields are missing or invalid.
    """
    # Map farmctl.py field names -> canonical names.
    # Check canonical name first, then fall back to farmctl.py name.
    field_map = {
        "temperature_c": ["temperature_c", "temp_c"],
        "humidity_pct":  ["humidity_pct"],
        "co2_ppm":       ["co2_ppm"],
        "light_level":   ["light_level", "light_raw"],
        "soil_moisture":  ["soil_moisture_pct", "soil_raw"],
    }

    resolved: dict = {}
    missing: list[str] = []

    for canonical, candidates in field_map.items():
        found = False
        for key in candidates:
            if key in data:
                resolved[canonical] = data[key]
                found = True
                break
        if not found:
            missing.append(f"{canonical} (tried: {candidates})")

    if missing:
        raise SensorReadError(f"Missing sensor fields: {missing}")

    try:
        temperature_c = float(resolved["temperature_c"])
        humidity_pct = float(resolved["humidity_pct"])
        co2_ppm = int(float(resolved["co2_ppm"]))
        light_level = int(float(resolved["light_level"]))

        # Convert soil raw ADC to percentage.
        # Track the source field: "soil_raw" always requires ADC→% conversion,
        # even when the ADC happens to be ≤ 100 (very wet soil).
        # "soil_moisture_pct" is already a percentage and passes through as-is.
        soil_came_from_raw = "soil_raw" in data and "soil_moisture_pct" not in data
        soil_value = float(resolved["soil_moisture"])
        if soil_came_from_raw:
            logger.debug("soil_raw=%g → applying ADC calibration", soil_value)
            soil_moisture_pct = round(_soil_adc_to_pct(soil_value), 1)
        elif soil_value > 100:
            # soil_moisture_pct field but value > 100 — treat as raw ADC.
            logger.warning(
                "soil_moisture_pct field has unexpected value %g (> 100); "
                "applying ADC calibration as fallback.",
                soil_value,
            )
            soil_moisture_pct = round(_soil_adc_to_pct(soil_value), 1)
        else:
            soil_moisture_pct = soil_value

        # Optional hardware state fields (present when firmware reports them)
        water_tank_ok = data.get("water_tank_ok")
        light_on = data.get("light_on")
        heater_on = data.get("heater_on")
        heater_lockout = data.get("heater_lockout")
        water_pump_on = data.get("water_pump_on")
        circulation_on = data.get("circulation_on")
        water_pump_remaining_sec = data.get("water_pump_remaining_sec")
        circulation_remaining_sec = data.get("circulation_remaining_sec")

        return SensorData(
            temperature_c=temperature_c,
            humidity_pct=humidity_pct,
            co2_ppm=co2_ppm,
            light_level=light_level,
            soil_moisture_pct=soil_moisture_pct,
            timestamp=data.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),
            water_tank_ok=water_tank_ok,
            light_on=light_on,
            heater_on=heater_on,
            heater_lockout=heater_lockout,
            water_pump_on=water_pump_on,
            circulation_on=circulation_on,
            water_pump_remaining_sec=water_pump_remaining_sec,
            circulation_remaining_sec=circulation_remaining_sec,
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
        water_tank_ok=True,
        light_on=False,
        heater_on=False,
        heater_lockout=False,
        water_pump_on=False,
        circulation_on=False,
        water_pump_remaining_sec=0,
        circulation_remaining_sec=0,
    )
