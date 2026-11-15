"""Installed graph observations preserve extras and every incoming constraint."""

from __future__ import annotations

from email.message import Message
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

from scripts.plan1_dependency_environment import observe_transitive_dependencies


def package(version: str = "1.0", requires: tuple[str, ...] = ()) -> SimpleNamespace:
    metadata = Message()
    metadata["License-Expression"] = "MIT"
    return SimpleNamespace(
        version=version, requires=requires, metadata=metadata, files=(),
        read_text=lambda _: "Metadata-Version: 2.4\nLicense-Expression: MIT\n",
    )


def test_transitive_missing_and_conflicting_diamond_are_not_hidden_by_visited_nodes() -> None:
    packages = {
        "root": package(requires=("left", "right")),
        "left": package(requires=("shared>=2", "missing")),
        "right": package(requires=("shared>=1",)),
        "shared": package(),
    }

    def lookup(name: str) -> SimpleNamespace:
        if name not in packages:
            raise PackageNotFoundError(name)
        return packages[name]

    report = observe_transitive_dependencies(["root"], lookup=lookup)
    assert report["failures"] == ["missing:missing", "version:shared:>=2"]
    # Restoring the environment clears both failures with no stale cached verdict.
    packages["shared"] = package("2.0")
    packages["missing"] = package()
    assert observe_transitive_dependencies(["root"], lookup=lookup)["failures"] == []


def test_extra_paths_cycles_and_inactive_markers_preserve_the_actual_graph() -> None:
    packages = {
        "root": package(requires=("shared", "shared[feature]")),
        "shared": package(requires=(
            "root", 'extra-child; extra == "feature"',
            'never-installed; python_version < "2"',
        )),
        "extra-child": package(),
        "unrelated-private-package": package(),
    }
    looked_up: list[str] = []

    def lookup(name: str) -> SimpleNamespace:
        looked_up.append(name)
        return packages[name]

    report = observe_transitive_dependencies(["root"], lookup=lookup)
    assert report["failures"] == []
    assert {item["name"] for item in report["distributions"]} == {
        "root", "shared", "extra-child",
    }
    assert "never-installed" not in looked_up
    assert "unrelated-private-package" not in looked_up
    shared = next(item for item in report["distributions"] if item["name"] == "shared")
    assert shared["active_requires"] == ['extra-child; extra == "feature"', "root"]
    assert shared["declared_license"] == "MIT"
    assert shared["license_texts"] == []
    assert report["license_clearance"] is False
