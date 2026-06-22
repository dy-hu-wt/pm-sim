from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path | str) -> dict[str, Any]:
    scenario_path = Path(path)
    if not scenario_path.exists():
        raise ScenarioError(f"Scenario file not found: {scenario_path}")

    data = json.loads(scenario_path.read_text())
    _validate_scenario(data, scenario_path)
    return data


def _validate_scenario(data: dict[str, Any], path: Path) -> None:
    required = ["id", "start_time", "people", "projects"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ScenarioError(f"{path} is missing required keys: {', '.join(missing)}")

    if not isinstance(data["people"], list):
        raise ScenarioError("Scenario key 'people' must be a list.")

    if not isinstance(data["projects"], list):
        raise ScenarioError("Scenario key 'projects' must be a list.")
