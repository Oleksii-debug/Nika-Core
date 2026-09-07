from __future__ import annotations

import math

import pytest

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResourcePolicy,
)


def _request(*, timeout_seconds: object) -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="fixture"),),
        timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("value", (True, False, "30", None))
def test_model_request_timeout_rejects_non_numeric_or_boolean(value: object) -> None:
    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        _request(timeout_seconds=value)


@pytest.mark.parametrize(
    "value",
    (
        0,
        -1,
        math.nan,
        math.inf,
        -math.inf,
    ),
)
def test_model_request_timeout_rejects_non_finite_or_non_positive(value: float) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        _request(timeout_seconds=value)


@pytest.mark.parametrize("value", (0.001, 1, 30.0, 3600))
def test_model_request_timeout_accepts_finite_positive_numbers(value: float) -> None:
    request = _request(timeout_seconds=value)

    assert request.timeout_seconds == value


@pytest.mark.parametrize("field", ("max_cpu_percent", "max_memory_percent"))
@pytest.mark.parametrize("value", (True, False, "50"))
def test_resource_percentages_reject_non_numeric_or_boolean(
    field: str,
    value: object,
) -> None:
    with pytest.raises(TypeError, match=f"{field} must be numeric"):
        ModelResourcePolicy(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ("max_cpu_percent", "max_memory_percent"))
@pytest.mark.parametrize(
    "value",
    (
        0,
        -1,
        100.1,
        math.nan,
        math.inf,
        -math.inf,
    ),
)
def test_resource_percentages_reject_non_finite_or_out_of_range(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="finite and in the range"):
        ModelResourcePolicy(**{field: value})


@pytest.mark.parametrize(
    ("cpu", "memory"),
    (
        (1, 1),
        (50.5, 75),
        (100, 100.0),
    ),
)
def test_resource_percentages_accept_finite_bounded_numbers(
    cpu: float,
    memory: float,
) -> None:
    policy = ModelResourcePolicy(
        max_cpu_percent=cpu,
        max_memory_percent=memory,
    )

    assert policy.max_cpu_percent == cpu
    assert policy.max_memory_percent == memory


@pytest.mark.parametrize("value", (True, False, 1.5, "1024"))
def test_min_available_memory_requires_strict_integer(value: object) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        ModelResourcePolicy(min_available_memory_bytes=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", (0, -1))
def test_min_available_memory_rejects_non_positive_integer(value: int) -> None:
    with pytest.raises(ValueError, match="must be greater than zero"):
        ModelResourcePolicy(min_available_memory_bytes=value)


@pytest.mark.parametrize("value", (1, 1024, 16 * 1024 * 1024 * 1024))
def test_min_available_memory_accepts_positive_integer(value: int) -> None:
    policy = ModelResourcePolicy(min_available_memory_bytes=value)

    assert policy.min_available_memory_bytes == value
