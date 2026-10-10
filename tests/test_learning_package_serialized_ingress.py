from __future__ import annotations

import pytest

from nika_core.learning_package import FrozenLearningPackage, LearningPackageIntegrityError


class _HostileString(str):
    def __len__(self) -> int:
        raise AssertionError("serialized string subclass length hook must not run")

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        raise AssertionError("serialized string subclass encode hook must not run")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("serialized string subclass equality hook must not run")


class _HostileBytes(bytes):
    def __len__(self) -> int:
        raise AssertionError("serialized bytes subclass length hook must not run")

    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise AssertionError("serialized bytes subclass decode hook must not run")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("serialized bytes subclass equality hook must not run")


def test_serialized_string_subclass_rejected_before_custom_hooks() -> None:
    raw = _HostileString("{}")

    with pytest.raises(LearningPackageIntegrityError, match="exact str or bytes"):
        FrozenLearningPackage.from_json(raw)


def test_serialized_bytes_subclass_rejected_before_custom_hooks() -> None:
    raw = _HostileBytes(b"{}")

    with pytest.raises(LearningPackageIntegrityError, match="exact str or bytes"):
        FrozenLearningPackage.from_json(raw)
