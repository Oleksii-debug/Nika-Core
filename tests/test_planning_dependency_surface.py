from __future__ import annotations

import tomllib
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _project_metadata() -> dict[str, object]:
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_planning_extra_is_exact_permissive_adopted_surface() -> None:
    project = _project_metadata()
    optional = project["optional-dependencies"]
    assert isinstance(optional, dict)

    planning = optional["planning"]
    assert planning == ["unified-planning==1.3.0", "up-aries==0.5.0"]
    assert all("pyperplan" not in requirement.casefold() for requirement in planning)


def test_planning_engine_does_not_bloat_base_install() -> None:
    project = _project_metadata()
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)

    lowered = tuple(requirement.casefold() for requirement in dependencies)
    assert all("unified-planning" not in requirement for requirement in lowered)
    assert all("up-aries" not in requirement for requirement in lowered)
    assert all("pyperplan" not in requirement for requirement in lowered)
