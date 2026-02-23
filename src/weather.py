"""Outdoor weather fetching using Open-Meteo API (no API key required).

Fetches current outdoor weather conditions for a configured location
to provide environmental context for plant care decisions.

Configuration (via environment variables):
    WEATHER_LAT: Latitude  (e.g. "51.5074")
    WEATHER_LON: Longitude (e.g. "-0.1278")

Both must be set for weather fetching to be active.
If either is missing, fetch_weather() returns None gracefully.
If the HTTP request fails, None is returned and a warning is logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

# WMO Weather Interpretation Codes (subset used by Open-Meteo)
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "icy fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "moderate showers",
    82: "heavy showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}

_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SEC = 5


def fetch_weather() -> dict[str, Any] | None:
    """Fetch current outdoor weather from Open-Meteo.

    Reads WEATHER_LAT and WEATHER_LON from the environment.
    Returns None silently if either variable is unset or if the
    request fails, so callers never need to handle exceptions.

    Returns:
        Dict with keys:
            temperature_c (float | None): Outdoor air temperature in °C.
            humidity_pct (int | None): Relative humidity in %.
            apparent_temperature_c (float | None): Feels-like temperature in °C.
            wind_speed_kmh (float | None): Wind speed at 10 m in km/h.
            condition (str): Human-readable weather condition string.
        Or None if location is not configured or the fetch fails.
    """
    lat = os.environ.get("WEATHER_LAT", "").strip()
    lon = os.environ.get("WEATHER_LON", "").strip()

    if not lat or not lon:
        return None

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "apparent_temperature,"
            "weather_code,"
            "wind_speed_10m"
        ),
        "timezone": "auto",
    }

    try:
        resp = requests.get(_OPEN_METEO_URL, params=params, timeout=_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Weather fetch failed: %s", exc)
        return None

    current = data.get("current", {})
    if not current:
        logger.warning("Weather API returned no 'current' data")
        return None

    weather_code = current.get("weather_code", -1)
    condition = _WMO_DESCRIPTIONS.get(int(weather_code), f"weather code {weather_code}")

    result = {
        "temperature_c": current.get("temperature_2m"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "wind_speed_kmh": current.get("wind_speed_10m"),
        "condition": condition,
    }
    logger.info(
        "Outdoor weather: %.1f°C (%s), humidity %s%%, wind %.1f km/h",
        result["temperature_c"] or 0,
        result["condition"],
        result["humidity_pct"],
        result["wind_speed_kmh"] or 0,
    )
    return result
