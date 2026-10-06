from __future__ import annotations

import json
from typing import NoReturn

from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationError,
)

_SCHEMA = "nika-packaged-product-factory-execution-plan-v1"
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 20_000
_MAX_TEXT_BYTES = 16 * 1024
_MAX_REPOSITORIES = 64
_MAX_COMPONENTS = 256
_MAX_PATHS_PER_COMPONENT = 512
_MAX_DEPENDENCIES_PER_COMPONENT = 256
_MAX_COMMANDS_PER_COMPONENT = 128
_MAX_ARGV_ITEMS = 128
_MAX_PERMISSIONS = 256
_MAX_SIGNED_64 = (1 << 63) - 1

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "project_id",
        "expected_spec_version",
        "expected_row_version",
        "graph_version",
        "repositories",
        "components",
        "base_shas",
        "component_goals",
        "permission_ceiling",
    }
)
_REPOSITORY_FIELDS = frozenset(
    {
        "repository_id",
        "provider",
        "locator",
        "default_branch",
        "credential_ref",
        "case_sensitive_paths",
    }
)
_COMPONENT_FIELDS = frozenset(
    {
        "component_id",
        "repository_id",
        "paths",
        "dependencies",
        "build_commands",
        "test_commands",
        "release_identity",
    }
)


class PackagedExecutionPlanAdmissionError(ValueError):
    """Raised when a portable execution-plan claim is not safe to admit."""


def decode_packaged_product_factory_execution_plan(
    payload: bytes,
) -> PackagedProductFactoryExecutionPlan:
    """Decode one bounded explicit execution-plan claim into incumbent PF carriers.

    This is an admission boundary, not execution or review authority. It never
    dispatches work, persists state, resolves repository roots, reads credentials,
    or invents a repository graph/permission ceiling from user text. The caller
    still has to compose the returned plan through the existing trusted Product
    Factory preparation and review authorities.
    """

    root = _decode_json_object(payload)
    _require_exact_fields(root, _TOP_LEVEL_FIELDS, "execution plan")
    if root["schema"] != _SCHEMA:
        raise PackagedExecutionPlanAdmissionError(
            "execution plan schema is not supported"
        )

    repositories_value = _exact_list(root["repositories"], "repositories")
    if not repositories_value or len(repositories_value) > _MAX_REPOSITORIES:
        raise PackagedExecutionPlanAdmissionError(
            f"repositories must contain 1..{_MAX_REPOSITORIES} items"
        )
    repositories = tuple(
        _decode_repository(item, index)
        for index, item in enumerate(repositories_value)
    )

    components_value = _exact_list(root["components"], "components")
    if not components_value or len(components_value) > _MAX_COMPONENTS:
        raise PackagedExecutionPlanAdmissionError(
            f"components must contain 1..{_MAX_COMPONENTS} items"
        )
    components = tuple(
        _decode_component(item, index)
        for index, item in enumerate(components_value)
    )

    permissions_value = _exact_list(
        root["permission_ceiling"],
        "permission_ceiling",
    )
    if not permissions_value or len(permissions_value) > _MAX_PERMISSIONS:
        raise PackagedExecutionPlanAdmissionError(
            f"permission_ceiling must contain 1..{_MAX_PERMISSIONS} items"
        )
    permissions = tuple(
        _canonical_text(value, "permission_ceiling item")
        for value in permissions_value
    )
    if len(permissions) != len(set(permissions)):
        raise PackagedExecutionPlanAdmissionError(
            "permission_ceiling must not contain duplicates"
        )

    try:
        graph = ProductRepositoryGraph(
            project_id=_canonical_text(root["project_id"], "project_id"),
            repositories=repositories,
            components=components,
        )
        return PackagedProductFactoryExecutionPlan(
            project_id=graph.project_id,
            expected_spec_version=_positive_int(
                root["expected_spec_version"],
                "expected_spec_version",
            ),
            expected_row_version=_non_negative_int(
                root["expected_row_version"],
                "expected_row_version",
            ),
            graph=graph,
            graph_version=_positive_int(root["graph_version"], "graph_version"),
            base_shas=_text_mapping(
                root["base_shas"],
                "base_shas",
                max_items=_MAX_REPOSITORIES,
            ),
            component_goals=_text_mapping(
                root["component_goals"],
                "component_goals",
                max_items=_MAX_COMPONENTS,
            ),
            permission_ceiling=frozenset(permissions),
        )
    except (RepositoryGraphError, PackagedProductFactoryPreparationError) as exc:
        raise PackagedExecutionPlanAdmissionError(
            f"execution plan authority claim is invalid: {exc}"
        ) from exc


def _decode_repository(value: object, index: int) -> RepositoryRef:
    label = f"repositories[{index}]"
    item = _exact_object(value, label)
    _require_exact_fields(item, _REPOSITORY_FIELDS, label)

    credential_value = item["credential_ref"]
    credential_ref = (
        None
        if credential_value is None
        else _canonical_text(credential_value, f"{label}.credential_ref")
    )
    if type(item["case_sensitive_paths"]) is not bool:
        raise PackagedExecutionPlanAdmissionError(
            f"{label}.case_sensitive_paths must be an exact boolean"
        )

    try:
        return RepositoryRef(
            repository_id=_canonical_text(
                item["repository_id"],
                f"{label}.repository_id",
            ),
            provider=_canonical_text(item["provider"], f"{label}.provider"),
            locator=_canonical_text(item["locator"], f"{label}.locator"),
            default_branch=_canonical_text(
                item["default_branch"],
                f"{label}.default_branch",
            ),
            credential_ref=credential_ref,
            case_sensitive_paths=item["case_sensitive_paths"],
        )
    except RepositoryGraphError as exc:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} is invalid: {exc}"
        ) from exc


def _decode_component(value: object, index: int) -> ProductComponent:
    label = f"components[{index}]"
    item = _exact_object(value, label)
    _require_exact_fields(item, _COMPONENT_FIELDS, label)

    release_value = item["release_identity"]
    release_identity = (
        None
        if release_value is None
        else _canonical_text(release_value, f"{label}.release_identity")
    )
    try:
        return ProductComponent(
            component_id=_canonical_text(
                item["component_id"],
                f"{label}.component_id",
            ),
            repository_id=_canonical_text(
                item["repository_id"],
                f"{label}.repository_id",
            ),
            paths=_text_sequence(
                item["paths"],
                f"{label}.paths",
                min_items=1,
                max_items=_MAX_PATHS_PER_COMPONENT,
            ),
            dependencies=_text_sequence(
                item["dependencies"],
                f"{label}.dependencies",
                min_items=0,
                max_items=_MAX_DEPENDENCIES_PER_COMPONENT,
            ),
            build_commands=_commands(
                item["build_commands"],
                f"{label}.build_commands",
            ),
            test_commands=_commands(
                item["test_commands"],
                f"{label}.test_commands",
            ),
            release_identity=release_identity,
        )
    except RepositoryGraphError as exc:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} is invalid: {exc}"
        ) from exc


def _commands(
    value: object,
    label: str,
) -> tuple[tuple[str, ...], ...]:
    commands = _exact_list(value, label)
    if len(commands) > _MAX_COMMANDS_PER_COMPONENT:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} exceeds the command-count limit"
        )

    result: list[tuple[str, ...]] = []
    for command_index, command_value in enumerate(commands):
        result.append(
            _text_sequence(
                command_value,
                f"{label}[{command_index}]",
                min_items=1,
                max_items=_MAX_ARGV_ITEMS,
            )
        )
    return tuple(result)


def _text_sequence(
    value: object,
    label: str,
    *,
    min_items: int,
    max_items: int,
) -> tuple[str, ...]:
    items = _exact_list(value, label)
    if len(items) < min_items or len(items) > max_items:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must contain {min_items}..{max_items} items"
        )
    return tuple(
        _canonical_text(item, f"{label} item")
        for item in items
    )


def _text_mapping(
    value: object,
    label: str,
    *,
    max_items: int,
) -> dict[str, str]:
    mapping = _exact_object(value, label)
    if not mapping or len(mapping) > max_items:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must contain 1..{max_items} entries"
        )

    result: dict[str, str] = {}
    for raw_key, raw_value in mapping.items():
        key = _canonical_text(raw_key, f"{label} key")
        result[key] = _canonical_text(raw_value, f"{label}[{key}]")
    return result


def _decode_json_object(payload: object) -> dict[str, object]:
    if type(payload) is not bytes:
        raise TypeError("execution plan payload must be exact bytes")
    if not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise PackagedExecutionPlanAdmissionError(
            f"execution plan payload must contain 1..{_MAX_PAYLOAD_BYTES} bytes"
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackagedExecutionPlanAdmissionError(
            "execution plan payload must be strict UTF-8"
        ) from exc
    if text.startswith("\ufeff"):
        raise PackagedExecutionPlanAdmissionError(
            "execution plan payload must not contain a UTF-8 BOM"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except PackagedExecutionPlanAdmissionError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PackagedExecutionPlanAdmissionError(
            "execution plan payload is not valid bounded JSON"
        ) from exc

    _validate_json_shape(value)
    return _exact_object(value, "execution plan")


def _unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackagedExecutionPlanAdmissionError(
                f"execution plan JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    raise PackagedExecutionPlanAdmissionError(
        f"execution plan JSON contains non-finite number {value}"
    )


def _validate_json_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES:
            raise PackagedExecutionPlanAdmissionError(
                "execution plan JSON exceeds the node limit"
            )
        if depth > _MAX_DEPTH:
            raise PackagedExecutionPlanAdmissionError(
                "execution plan JSON exceeds the depth limit"
            )

        if type(current) is dict:
            for key, child in current.items():
                _canonical_text(key, "JSON object key")
                stack.append((child, depth + 1))
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            _canonical_text(current, "JSON string")
        elif type(current) is int:
            if abs(current) > _MAX_SIGNED_64:
                raise PackagedExecutionPlanAdmissionError(
                    "execution plan JSON integer exceeds signed-64 range"
                )
        elif current is None or type(current) is bool:
            continue
        else:
            raise PackagedExecutionPlanAdmissionError(
                "execution plan JSON contains an unsupported scalar type"
            )


def _require_exact_fields(
    value: dict[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing=" + ",".join(missing))
    if unexpected:
        details.append("unexpected=" + ",".join(unexpected))
    raise PackagedExecutionPlanAdmissionError(
        f"{label} fields are not exact ({'; '.join(details)})"
    )


def _exact_object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be an exact JSON object"
        )
    return value


def _exact_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be an exact JSON array"
        )
    return value


def _canonical_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be canonical non-empty text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be valid UTF-8 text"
        ) from exc
    if len(encoded) > _MAX_TEXT_BYTES:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} exceeds the text byte limit"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    result = _non_negative_int(value, label)
    if result == 0:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be a positive integer"
        )
    return result


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_SIGNED_64:
        raise PackagedExecutionPlanAdmissionError(
            f"{label} must be a non-negative signed-64 integer"
        )
    return value
