from __future__ import annotations

from collections.abc import Callable

import pytest

import nika_core.learning_package.manifest as manifest_module
from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningPackageIntegrityError,
    LearningPackageValidationError,
    LearningShard,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
G = "0" * 64
H = "1" * 64
I = "2" * 64
J = "3" * 64


def _package() -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="candidate",
        package_version="v1",
        base_artifact_sha256=G,
        selection_policy_sha256=H,
        verification_sha256=I,
        evaluation_set_sha256=J,
        shards=(
            LearningShard(
                split=LearningDataSplit.TRAINING,
                artifact_sha256=A,
                provenance_sha256=C,
                license_evidence_sha256=E,
                record_count=10,
                byte_count=100,
            ),
            LearningShard(
                split=LearningDataSplit.VALIDATION,
                artifact_sha256=B,
                provenance_sha256=D,
                license_evidence_sha256=F,
                record_count=10,
                byte_count=100,
            ),
        ),
    )


def _export_surfaces(
    package: FrozenLearningPackage,
) -> tuple[Callable[[], object], ...]:
    return (
        lambda: package.candidate_dataset_payload(),
        lambda: package.candidate_dataset_sha256,
        lambda: package.canonical_payload(),
        lambda: package.manifest_sha256,
        lambda: package.to_json(),
    )


def test_export_surfaces_revalidate_package_fields_after_freeze_bypass() -> None:
    package = _package()
    object.__setattr__(package, "evaluation_set_sha256", A)

    for export in _export_surfaces(package):
        with pytest.raises(
            LearningPackageValidationError,
            match="held-out evaluation artifact",
        ):
            export()


def test_export_surfaces_revalidate_internal_shards_after_freeze_bypass() -> None:
    package = _package()
    object.__setattr__(package.shards[0], "record_count", -1)

    for export in _export_surfaces(package):
        with pytest.raises(
            LearningPackageValidationError,
            match="positive signed-64 integer",
        ):
            export()


def test_serialized_ingress_normalizes_json_recursion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("synthetic parser recursion")

    monkeypatch.setattr(manifest_module.json, "loads", raise_recursion)

    with pytest.raises(
        LearningPackageIntegrityError,
        match="nesting is invalid",
    ):
        FrozenLearningPackage.from_json("{}")
