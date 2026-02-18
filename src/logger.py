"""Structured JSONL logger for plant-ops-ai.

Append-only logging of sensor readings and AI decisions.
Each log file uses JSONL format (one JSON object per line).
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.sensor_reader import SensorData

logger = logging.getLogger(__name__)

SENSOR_FILE = "sensor_history.jsonl"
DECISION_FILE = "decisions.jsonl"


def _ensure_dir(data_dir: str) -> Path:
    """Create the data directory if it doesn't exist.

    Args:
        data_dir: Path to the data directory.

    Returns:
        Path object for the data directory.
    """
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_jsonl(filepath: Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file.

    Args:
        filepath: Path to the JSONL file.
        record: Dict to serialize as one JSON line.
    """
    line = json.dumps(record, default=str)
    with open(filepath, "a") as f:
        f.write(line + "\n")


def _read_jsonl(filepath: Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL file.

    Skips blank lines and lines that fail to parse (logs a warning).

    Args:
        filepath: Path to the JSONL file.

    Returns:
        List of parsed dicts.
    """
    if not filepath.exists():
        return []

    records = []
    with open(filepath, "r") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed JSONL line %d in %s", line_num, filepath
                )
    return records


def log_sensor_reading(data: SensorData, data_dir: str) -> None:
    """Append a sensor reading to sensor_history.jsonl.

    Args:
        data: Current sensor readings.
        data_dir: Path to the data directory.
    """
    dirpath = _ensure_dir(data_dir)
    record = data.to_dict()
    record["logged_at"] = datetime.now(timezone.utc).isoformat()

    _append_jsonl(dirpath / SENSOR_FILE, record)


def log_decision(
    sensor_data: SensorData,
    decision: dict[str, Any],
    validation: Any,  # ValidationResult from safety.py
    executed: bool,
    data_dir: str,
) -> None:
    """Append a decision record to decisions.jsonl.

    Args:
        sensor_data: Sensor readings at decision time.
        decision: The AI's proposed action dict.
        validation: ValidationResult from safety.validate_action().
        executed: Whether the action was actually executed.
        data_dir: Path to the data directory.
    """
    dirpath = _ensure_dir(data_dir)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sensor_data": sensor_data.to_dict(),
        "decision": decision,
        "validation": {
            "valid": validation.valid,
            "reason": validation.reason,
            "capped_action": validation.capped_action,
        },
        "executed": executed,
    }

    _append_jsonl(dirpath / DECISION_FILE, record)


def load_recent_decisions(n: int, data_dir: str) -> list[dict[str, Any]]:
    """Load the last N decision records.

    Args:
        n: Number of recent decisions to return.
        data_dir: Path to the data directory.

    Returns:
        List of the most recent N decision dicts (newest last).
    """
    filepath = Path(data_dir) / DECISION_FILE
    records = _read_jsonl(filepath)
    return records[-n:] if n > 0 else []


def load_recent_sensors(n: int, data_dir: str) -> list[dict[str, Any]]:
    """Load the last N sensor readings.

    Args:
        n: Number of recent readings to return.
        data_dir: Path to the data directory.

    Returns:
        List of the most recent N sensor reading dicts (newest last).
    """
    filepath = Path(data_dir) / SENSOR_FILE
    records = _read_jsonl(filepath)
    return records[-n:] if n > 0 else []


def get_daily_action_counts(data_dir: str) -> dict[str, int]:
    """Count actions by type for today (UTC).

    Args:
        data_dir: Path to the data directory.

    Returns:
        Dict mapping action type strings to their count today.
        Example: {"water": 3, "light": 1, "do_nothing": 5}
    """
    filepath = Path(data_dir) / DECISION_FILE
    records = _read_jsonl(filepath)

    today = datetime.now(timezone.utc).date().isoformat()
    counts: dict[str, int] = {}

    for record in records:
        # Only count executed actions
        if not record.get("executed", False):
            continue

        ts = record.get("timestamp", "")
        if not ts.startswith(today):
            continue

        action_type = record.get("decision", {}).get("action", "unknown")
        counts[action_type] = counts.get(action_type, 0) + 1

    return counts
