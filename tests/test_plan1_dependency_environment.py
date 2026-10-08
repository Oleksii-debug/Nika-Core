"""Plan 1 §1: environment observations must not be misrepresented as clearance."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.plan1_dependency_environment import observe_dependencies


def fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=75"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "nika-core"\n'
        'dependencies = ["example-lib>=2,<3", "windows-lib>=1; python_version < \'2.0\'"]\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8,<9"]\n',
        encoding="utf-8",
    )
    (tmp_path / "docs" / "PLAN1_DEPENDENCY_AUTHORITY.json").write_text(
        json.dumps(
            {
                "plan": 1,
                "section": 1,
                "project": "nika-core",
                "resolution_state": "DECLARED_RANGES_ONLY_NOT_REPRODUCIBLE",
                "groups": [
                    {"group": "base", "requirements": ["example-lib>=2,<3", "windows-lib>=1; python_version < '2.0'"]},
                    {"group": "dev", "requirements": ["pytest>=8,<9"]},
                    {"group": "build-system", "requirements": ["setuptools>=75"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_installed_distribution_observation_is_not_a_license_or_lock_claim(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    looked_up = []

    def lookup(name: str) -> SimpleNamespace:
        looked_up.append(name)
        return SimpleNamespace(version="2.5", metadata={"License-Expression": "MIT"})

    result = observe_dependencies(root, ("base",), lookup=lookup)
    assert looked_up == ["example-lib"]
    assert result["missing_or_mismatched"] == []
    assert result["license_clearance"] is False
    assert result["reproducible_lock_proven"] is False
    assert result["distributions"][0]["license_expression"] == "MIT"
    assert result["distributions"][1]["specifier_satisfied"] is None
    assert len(result["project_sha256"]) == 64


def test_absent_or_out_of_range_distribution_is_not_qualified(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)

    def missing(_: str) -> object:
        raise PackageNotFoundError("absent")

    report = observe_dependencies(root, ("base",), lookup=missing)
    assert report["missing_or_mismatched"] == ["base:example-lib"]

    report = observe_dependencies(
        root,
        ("base",),
        lookup=lambda _: SimpleNamespace(version="3.1", metadata={}),
    )
    assert report["missing_or_mismatched"] == ["base:example-lib"]


def test_manifest_drift_and_unknown_or_duplicate_groups_fail_closed(tmp_path: Path) -> None:
    root = fixture_root(tmp_path)
    with pytest.raises(ValueError, match="Unknown dependency group"):
        observe_dependencies(root, ("other",))
    with pytest.raises(ValueError, match="unique dependency groups"):
        observe_dependencies(root, ("base", "base"))
    authority = root / "docs" / "PLAN1_DEPENDENCY_AUTHORITY.json"
    payload = json.loads(authority.read_text(encoding="utf-8"))
    payload["groups"][0]["requirements"][0] = "example-lib>=1"
    authority.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="requirements drift"):
        observe_dependencies(root, ("base",))
