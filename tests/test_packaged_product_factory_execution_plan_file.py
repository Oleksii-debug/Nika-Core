from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import nika_core.product_factory_packaged_execution_plan_file as plan_file
from nika_core.product_factory_packaged_execution_plan_file import (
    PackagedExecutionPlanFileError,
    PackagedExecutionPlanFileResolver,
    read_packaged_execution_plan_file,
)


def _claim(project_id: str = "product-1") -> dict[str, object]:
    return {
        "schema": "nika-packaged-product-factory-execution-plan-v1",
        "project_id": project_id,
        "expected_spec_version": 2,
        "expected_row_version": 3,
        "graph_version": 1,
        "repositories": [
            {
                "repository_id": "repo-1",
                "provider": "github",
                "locator": "Oleksii-debug/Nika-Core",
                "default_branch": "main",
                "credential_ref": "credref:github-product-factory",
                "case_sensitive_paths": True,
            }
        ],
        "components": [
            {
                "component_id": "core",
                "repository_id": "repo-1",
                "paths": ["src/nika_core"],
                "dependencies": [],
                "build_commands": [["python", "-m", "compileall", "src/nika_core"]],
                "test_commands": [["python", "-m", "pytest", "tests/test_core.py", "-q"]],
                "release_identity": None,
            }
        ],
        "base_shas": {"repo-1": "a" * 40},
        "component_goals": {"core": "Implement the explicitly authorized change"},
        "permission_ceiling": ["read_source", "write_source", "run_tests"],
    }


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_plan(tmp_path: Path, *, project_id: str = "product-1") -> Path:
    path = tmp_path / "execution-plan.json"
    path.write_bytes(_encode(_claim(project_id)))
    return path.resolve()


def test_resolver_decodes_one_explicit_selected_file(tmp_path: Path) -> None:
    path = _write_plan(tmp_path)
    selections: list[object] = []

    def select_file() -> object:
        selections.append("called")
        return (str(path),)

    plan = PackagedExecutionPlanFileResolver(select_file)("product-1")

    assert selections == ["called"]
    assert plan.project_id == "product-1"
    assert plan.graph.dependency_order() == ("core",)
    assert plan.base_shas == {"repo-1": "a" * 40}


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (None, "cancelled"),
        ((), "exactly one"),
        (("first.json", "second.json"), "exactly one"),
        (["first.json"], "exactly one"),
        (("",), "invalid"),
    ],
)
def test_resolver_rejects_ambiguous_or_cancelled_selection(
    selection: object,
    message: str,
) -> None:
    resolver = PackagedExecutionPlanFileResolver(lambda: selection)

    with pytest.raises(PackagedExecutionPlanFileError, match=message):
        resolver("product-1")


def test_resolver_rejects_plan_for_another_project(tmp_path: Path) -> None:
    path = _write_plan(tmp_path, project_id="product-other")
    resolver = PackagedExecutionPlanFileResolver(lambda: (str(path),))

    with pytest.raises(PackagedExecutionPlanFileError, match="another ProductProject"):
        resolver("product-1")


def test_reader_requires_absolute_json_regular_single_link(tmp_path: Path) -> None:
    path = _write_plan(tmp_path)

    with pytest.raises(PackagedExecutionPlanFileError, match="absolute"):
        read_packaged_execution_plan_file(Path("execution-plan.json"))

    wrong_suffix = tmp_path / "execution-plan.txt"
    wrong_suffix.write_bytes(path.read_bytes())
    with pytest.raises(PackagedExecutionPlanFileError, match="json"):
        read_packaged_execution_plan_file(wrong_suffix.resolve())

    alias = tmp_path / "execution-plan-alias.json"
    try:
        os.link(path, alias)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(PackagedExecutionPlanFileError, match="single-link regular authority"):
        read_packaged_execution_plan_file(path)


def test_reader_rejects_symlink_or_reparse_alias(tmp_path: Path) -> None:
    path = _write_plan(tmp_path)
    alias = tmp_path / "selected-alias.json"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("symbolic links are unavailable on this filesystem")

    with pytest.raises(PackagedExecutionPlanFileError):
        read_packaged_execution_plan_file(alias.absolute())


def test_reader_detects_path_replacement_while_descriptor_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_plan(tmp_path)
    original = path.read_bytes()
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"x" * len(original))
    real_read = plan_file.os.read
    attempted = False
    replacement_denied = False

    def mutating_read(descriptor: int, count: int) -> bytes:
        nonlocal attempted, replacement_denied
        chunk = real_read(descriptor, count)
        if chunk and not attempted:
            attempted = True
            try:
                os.replace(replacement, path)
            except OSError:
                replacement_denied = True
        return chunk

    monkeypatch.setattr(plan_file.os, "read", mutating_read)

    if os.name == "nt":
        assert read_packaged_execution_plan_file(path) == original
        assert attempted is True
        assert replacement_denied is True
        assert path.read_bytes() == original
    else:
        with pytest.raises(PackagedExecutionPlanFileError, match="changed"):
            read_packaged_execution_plan_file(path)
        assert attempted is True
        assert replacement_denied is False


def test_reader_rejects_oversize_before_decoder(tmp_path: Path) -> None:
    path = (tmp_path / "oversize.json").resolve()
    path.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(PackagedExecutionPlanFileError, match="1..1048576"):
        read_packaged_execution_plan_file(path)


def test_reader_returns_exact_bytes_without_pathname_reread(tmp_path: Path) -> None:
    path = _write_plan(tmp_path)
    expected = path.read_bytes()

    assert read_packaged_execution_plan_file(path) == expected


def test_resolver_redacts_decoder_failure(tmp_path: Path) -> None:
    path = (tmp_path / "execution-plan.json").resolve()
    path.write_bytes(b"{not-json")
    resolver = PackagedExecutionPlanFileResolver(lambda: (str(path),))

    with pytest.raises(
        PackagedExecutionPlanFileError,
        match="failed canonical admission",
    ):
        resolver("product-1")


def test_selector_must_be_callable() -> None:
    with pytest.raises(TypeError, match="selector"):
        PackagedExecutionPlanFileResolver(object())  # type: ignore[arg-type]
