from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from nika_core.product_factory_packaged_execution_plan import (
    PackagedExecutionPlanAdmissionError,
    decode_packaged_product_factory_execution_plan,
)


def _claim() -> dict[str, object]:
    return {
        "schema": "nika-packaged-product-factory-execution-plan-v1",
        "project_id": "product-1",
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
        "component_goals": {
            "core": "Implement the explicitly authorized Product Factory change"
        },
        "permission_ceiling": ["read_source", "write_source", "run_tests"],
    }


def _encode(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def test_decode_admits_exact_explicit_execution_plan() -> None:
    plan = decode_packaged_product_factory_execution_plan(_encode(_claim()))

    assert plan.project_id == "product-1"
    assert plan.expected_spec_version == 2
    assert plan.expected_row_version == 3
    assert plan.graph_version == 1
    assert plan.graph.dependency_order() == ("core",)
    assert plan.base_shas == {"repo-1": "a" * 40}
    assert plan.component_goals == {
        "core": "Implement the explicitly authorized Product Factory change"
    }
    assert plan.permission_ceiling == frozenset(
        {"read_source", "write_source", "run_tests"}
    )
    repository = plan.graph.repositories[0]
    assert repository.credential_ref == "credref:github-product-factory"
    assert repository.case_sensitive_paths is True
    component = plan.graph.components[0]
    assert component.build_commands == (
        ("python", "-m", "compileall", "src/nika_core"),
    )
    assert component.test_commands == (
        ("python", "-m", "pytest", "tests/test_core.py", "-q"),
    )


def test_decode_preserves_normalized_multiline_goal() -> None:
    claim = _claim()
    goals = claim["component_goals"]
    assert isinstance(goals, dict)
    goals["core"] = "Implement safely\nPreserve exact authority"

    plan = decode_packaged_product_factory_execution_plan(_encode(claim))

    assert plan.component_goals["core"] == (
        "Implement safely\nPreserve exact authority"
    )


def test_decode_rejects_duplicate_json_members_before_admission() -> None:
    payload = b'{"schema":"first","schema":"second"}'

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="duplicate key",
    ):
        decode_packaged_product_factory_execution_plan(payload)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_decode_rejects_non_finite_json_numbers(constant: str) -> None:
    payload = ('{"schema":' + constant + "}").encode("ascii")

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="non-finite",
    ):
        decode_packaged_product_factory_execution_plan(payload)


def test_decode_requires_strict_utf8_without_bom() -> None:
    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="strict UTF-8",
    ):
        decode_packaged_product_factory_execution_plan(b"\xff")

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="must not contain a UTF-8 BOM",
    ):
        decode_packaged_product_factory_execution_plan(b"\xef\xbb\xbf{}")


def test_decode_requires_exact_bytes_carrier() -> None:
    with pytest.raises(TypeError, match="exact bytes"):
        decode_packaged_product_factory_execution_plan(  # type: ignore[arg-type]
            bytearray(_encode(_claim()))
        )


def test_decode_rejects_unknown_authority_fields() -> None:
    claim = _claim()
    claim["team_plan"] = {"reviewer": "caller-controlled"}

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="unexpected=team_plan",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_boolean_version_alias() -> None:
    claim = _claim()
    claim["expected_spec_version"] = True

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="expected_spec_version",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_signed_64_overflow_before_carrier_build() -> None:
    claim = _claim()
    claim["expected_row_version"] = 1 << 63

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="signed-64",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_inline_credential_material() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["credential_ref"] = "ghp_not-an-opaque-reference"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="credref",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_inline_url_credentials_in_repository_locator() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["locator"] = "https://token-value@github.com/example/repository"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="inline URL credentials",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


@pytest.mark.parametrize(
    "locator",
    [
        "https://github.com/example/repository?token=must-not-survive",
        "https://github.com/example/repository#must-not-survive",
    ],
)
def test_decode_rejects_url_query_or_fragment_in_repository_locator(
    locator: str,
) -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["locator"] = locator

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="URL query or fragment",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


@pytest.mark.parametrize(
    "locator",
    [
        "Oleksii-debug/Nika-Core?token=must-not-survive",
        "Oleksii-debug/Nika-Core?api_key=must-not-survive",
        "Oleksii-debug/Nika-Core?client_secret=must-not-survive",
        "Oleksii-debug/Nika-Core?access%5Ftoken=must-not-survive",
        "Oleksii-debug/Nika-Core?token%253Dmust-not-survive",
        "https%3A%2F%2Ftoken-value%40github.com%2Fexample%2Frepository",
    ],
)
def test_decode_rejects_scheme_less_or_encoded_locator_credentials(
    locator: str,
) -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["locator"] = locator

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="credential",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_excessively_nested_locator_encoding() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    locator = "Oleksii-debug/Nika-Core?token=must-not-survive"
    for _index in range(140):
        locator = quote(locator, safe="/?")
    repository["locator"] = locator

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="decoding exceeds the bounded limit",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


@pytest.mark.parametrize(
    "locator",
    [
        "Oleksii-debug/Nika-Core\nforged-log-line",
        "Oleksii-debug/Nika-Core\rforged-log-line",
        "Oleksii-debug/Nika-Core\x7fforged-log-line",
    ],
)
def test_decode_rejects_control_characters_in_repository_locator(
    locator: str,
) -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["locator"] = locator

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="single-line locator",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


@pytest.mark.parametrize(
    "locator",
    [
        "Oleksii-debug/Nika-Core",
        "Oleksii-debug/C++-tools",
        "example/repository?ref=public-catalog",
    ],
)
def test_decode_preserves_benign_scheme_less_repository_locator(
    locator: str,
) -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["locator"] = locator

    plan = decode_packaged_product_factory_execution_plan(_encode(claim))

    assert plan.graph.repositories[0].locator == locator


@pytest.mark.parametrize(
    ("carrier", "field"),
    [
        ("top", "project_id"),
        ("repository", "repository_id"),
        ("repository", "provider"),
        ("repository", "default_branch"),
        ("component", "component_id"),
        ("component", "repository_id"),
    ],
)
def test_decode_rejects_control_characters_in_identity_fields(
    carrier: str,
    field: str,
) -> None:
    claim = _claim()
    if carrier == "top":
        claim[field] = "identity\nforged"
    elif carrier == "repository":
        repositories = claim["repositories"]
        assert isinstance(repositories, list)
        repository = repositories[0]
        assert isinstance(repository, dict)
        repository[field] = "identity\nforged"
    else:
        components = claim["components"]
        assert isinstance(components, list)
        component = components[0]
        assert isinstance(component, dict)
        component[field] = "identity\nforged"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="single-line identity text",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_control_characters_in_credential_reference() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["credential_ref"] = "credref:line\nbreak"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="opaque single-line reference",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_non_boolean_case_path_policy() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["case_sensitive_paths"] = 1

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="exact boolean",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_nested_repository_field_extension() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    repository = repositories[0]
    assert isinstance(repository, dict)
    repository["access_token"] = "must-never-be-admitted"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="unexpected=access_token",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_noncanonical_command_argv_carrier() -> None:
    claim = _claim()
    components = claim["components"]
    assert isinstance(components, list)
    component = components[0]
    assert isinstance(component, dict)
    component["build_commands"] = [["python", 7]]

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="build_commands",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_permission_set_semantic_collapse() -> None:
    claim = _claim()
    claim["permission_ceiling"] = ["read_source", "read_source"]

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="must not contain duplicates",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_invalid_base_sha_through_incumbent_plan_authority() -> None:
    claim = _claim()
    claim["base_shas"] = {"repo-1": "not-a-commit"}

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="base SHA is invalid",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_graph_component_for_unknown_repository() -> None:
    claim = _claim()
    components = claim["components"]
    assert isinstance(components, list)
    component = components[0]
    assert isinstance(component, dict)
    component["repository_id"] = "repo-missing"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="unknown repository",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_cyclic_component_graph() -> None:
    claim = _claim()
    components = claim["components"]
    assert isinstance(components, list)
    first = components[0]
    assert isinstance(first, dict)
    first["dependencies"] = ["ui"]
    components.append(
        {
            "component_id": "ui",
            "repository_id": "repo-1",
            "paths": ["src/nika_ui"],
            "dependencies": ["core"],
            "build_commands": [],
            "test_commands": [],
            "release_identity": None,
        }
    )
    goals = claim["component_goals"]
    assert isinstance(goals, dict)
    goals["ui"] = "Preserve accessible packaged UI"

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="cycle",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_oversized_repository_collection_before_graph_build() -> None:
    claim = _claim()
    repositories = claim["repositories"]
    assert isinstance(repositories, list)
    template = repositories[0]
    assert isinstance(template, dict)
    claim["repositories"] = [
        {
            **template,
            "repository_id": f"repo-{index}",
            "locator": f"example/repo-{index}",
        }
        for index in range(65)
    ]

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match=r"repositories must contain 1\.\.64 items",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_deep_json_even_when_hidden_in_unknown_field() -> None:
    claim = _claim()
    nested: object = "leaf"
    for _index in range(40):
        nested = [nested]
    claim["hidden"] = nested

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="depth limit",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))


def test_decode_rejects_oversized_text_before_plan_construction() -> None:
    claim = _claim()
    goals = claim["component_goals"]
    assert isinstance(goals, dict)
    goals["core"] = "x" * (16 * 1024 + 1)

    with pytest.raises(
        PackagedExecutionPlanAdmissionError,
        match="text byte limit",
    ):
        decode_packaged_product_factory_execution_plan(_encode(claim))
