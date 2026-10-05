from __future__ import annotations

import pytest

from nika_core.toolsmith.contracts import (
    AcceptanceCommand,
    CapabilityManifestV1,
    ChangedFile,
    ResourceBudget,
    TestEvidence,
)


@pytest.mark.parametrize("field", ("timeout_seconds", "max_output_bytes", "max_changed_files"))
@pytest.mark.parametrize(
    "invalid",
    (True, False, 1.0, 1.5, "1", None, float("nan"), float("inf"), 0, -1),
)
def test_resource_budget_rejects_noncanonical_or_nonpositive_numbers(
    field: str, invalid: object
) -> None:
    values = {"timeout_seconds": 4, "max_output_bytes": 4096, "max_changed_files": 3}
    values[field] = invalid
    with pytest.raises(ValueError, match="positive integers"):
        ResourceBudget(**values)


def test_resource_budget_accepts_exact_positive_integers() -> None:
    assert ResourceBudget(1, 1024, 1).timeout_seconds == 1


@pytest.mark.parametrize(
    "invalid",
    (True, False, 1.0, 1.5, "1", float("nan"), float("inf"), 0, -1),
)
def test_acceptance_timeout_rejects_noncanonical_or_nonpositive_numbers(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        AcceptanceCommand(("pytest",), timeout_seconds=invalid)


def test_acceptance_timeout_preserves_optional_none_and_integer() -> None:
    assert AcceptanceCommand(("pytest",)).timeout_seconds is None
    assert AcceptanceCommand(("pytest",), timeout_seconds=1).timeout_seconds == 1


@pytest.mark.parametrize(
    "invalid",
    (True, False, 0.0, 1.0, 1.5, "1", None, float("nan"), float("inf"), -1),
)
def test_changed_file_size_rejects_noncanonical_or_negative_numbers(
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        ChangedFile("src/module.py", "a" * 64, invalid)


def test_changed_file_accepts_empty_and_nonempty_byte_counts() -> None:
    assert ChangedFile("src/empty.py", "a" * 64, 0).size_bytes == 0
    assert ChangedFile("src/full.py", "b" * 64, 5).size_bytes == 5


@pytest.mark.parametrize(
    "invalid",
    (True, False, 0.0, 1.0, -1.0, "0", None, float("nan"), float("inf")),
)
def test_test_evidence_rejects_ambiguous_exit_codes(invalid: object) -> None:
    with pytest.raises(ValueError, match="exit code must be an integer"):
        TestEvidence(("pytest",), invalid, "a" * 64)


def test_test_evidence_preserves_exact_zero_and_negative_exit_codes() -> None:
    assert TestEvidence(("pytest",), 0, "a" * 64).exit_code == 0
    assert TestEvidence(("pytest",), -9, "a" * 64).exit_code == -9


@pytest.mark.parametrize(
    "invalid",
    (True, False, 1.0, 1.5, "1", None, float("nan"), float("inf"), 0, 2),
)
def test_capability_manifest_rejects_noncanonical_schema_versions(invalid: object) -> None:
    with pytest.raises(ValueError, match="integer capability manifest schema v1"):
        CapabilityManifestV1(
            capability_id="capability",
            version="1.0",
            digest="a" * 64,
            entrypoint="entrypoint",
            permissions=frozenset({"read"}),
            source="test",
            schema_version=invalid,
        )


def test_capability_manifest_preserves_version_one() -> None:
    manifest = CapabilityManifestV1(
        capability_id="capability",
        version="1.0",
        digest="a" * 64,
        entrypoint="entrypoint",
        permissions=frozenset({"read"}),
        source="test",
    )
    assert manifest.schema_version == 1
