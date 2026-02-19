"""Config loader for plant-ops-ai.

Loads YAML configuration files from the config/ directory.
All paths resolve relative to the project root.
"""

from pathlib import Path
from typing import Any

import yaml


# Project root: two levels up from this file (src/config_loader.py -> plant-ops-ai/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict.

    Args:
        path: Absolute path, or relative to project root.

    Returns:
        Parsed YAML contents.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        yaml.YAMLError: If the file contains invalid YAML.
    """
    filepath = Path(path)
    if not filepath.is_absolute():
        filepath = PROJECT_ROOT / filepath

    if not filepath.exists():
        raise FileNotFoundError(f"Config file not found: {filepath}")

    with open(filepath, "r") as f:
        data = yaml.safe_load(f)

    # safe_load returns None for empty files
    return data if data is not None else {}


def load_safety_limits() -> dict[str, Any]:
    """Load config/safety_limits.yaml.

    Returns:
        Safety limits configuration dict.
    """
    return load_yaml(CONFIG_DIR / "safety_limits.yaml")


def load_plant_profile() -> dict[str, Any]:
    """Load config/plant_profile.yaml.

    Returns:
        Plant profile configuration dict.
    """
    return load_yaml(CONFIG_DIR / "plant_profile.yaml")


def load_hardware_profile() -> dict[str, Any]:
    """Load config/hardware_profile.yaml.

    Returns:
        Hardware profile configuration dict.
    """
    return load_yaml(CONFIG_DIR / "hardware_profile.yaml")


def save_plant_profile(profile: dict[str, Any]) -> None:
    """Write plant profile back to config/plant_profile.yaml.

    Args:
        profile: Plant profile dict to save.
    """
    filepath = CONFIG_DIR / "plant_profile.yaml"
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False)


def save_hardware_profile(profile: dict[str, Any]) -> None:
    """Write hardware profile back to config/hardware_profile.yaml.

    Args:
        profile: Hardware profile dict to save.
    """
    filepath = CONFIG_DIR / "hardware_profile.yaml"
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False)
