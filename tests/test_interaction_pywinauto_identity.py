from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import AmbiguousTargetError
from nika_core.interaction.windows_uia_adapter import (
    PywinautoUIABackend,
    UIAControlRecord,
)


@dataclass
class FakeElementInfo:
    runtime_id: tuple[int, ...] | None
    identity: str
    compare_raises: bool = False

    def __eq__(self, other: object) -> bool:
        if self.compare_raises:
            raise RuntimeError("stale COM element")
        if not isinstance(other, FakeElementInfo):
            return NotImplemented
        return self.identity == other.identity


@dataclass
class FakeWrapper:
    element_info: FakeElementInfo


def _record(runtime_id: tuple[int, ...]) -> UIAControlRecord:
    return UIAControlRecord(
        runtime_id=runtime_id,
        automation_id="",
        role="button",
        name="Duplicate",
        enabled=True,
        visible=True,
        focused=False,
        value=None,
        bounds=None,
    )


def test_same_automation_element_is_deduplicated_by_compare_elements() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((1, 2, 3), "same-live-element"))
    duplicate = FakeWrapper(FakeElementInfo((1, 2, 3), "same-live-element"))

    assert backend._deduplicate_same_elements((first, duplicate)) == (first,)


def test_distinct_elements_with_same_runtime_id_are_preserved() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((1, 2, 3), "first"))
    second = FakeWrapper(FakeElementInfo((1, 2, 3), "second"))

    assert backend._deduplicate_same_elements((first, second)) == (first, second)


def test_same_name_with_different_runtime_ids_is_never_name_deduplicated() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((1,), "first"))
    second = FakeWrapper(FakeElementInfo((2,), "second"))

    assert backend._deduplicate_same_elements((first, second)) == (first, second)


def test_compare_failure_with_duplicate_runtime_id_fails_closed() -> None:
    backend = PywinautoUIABackend()
    stale = FakeWrapper(FakeElementInfo((1, 2, 3), "same", compare_raises=True))
    candidate = FakeWrapper(FakeElementInfo((1, 2, 3), "same"))

    with pytest.raises(AmbiguousTargetError):
        backend._deduplicate_same_elements((stale, candidate))


def test_distinct_same_runtime_elements_receive_stable_separate_generations() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((7, 7), "first"))
    second = FakeWrapper(FakeElementInfo((7, 7), "second"))

    initial = backend._assign_generations(
        100,
        ((first, _record((7, 7))), (second, _record((7, 7)))),
    )
    assert [record.element_generation for _, record in initial] == [1, 2]
    assert backend.last_duplicate_runtime_ids == ((7, 7),)

    first_again = FakeWrapper(FakeElementInfo((7, 7), "first"))
    second_again = FakeWrapper(FakeElementInfo((7, 7), "second"))
    repeated = backend._assign_generations(
        100,
        ((second_again, _record((7, 7))), (first_again, _record((7, 7)))),
    )
    by_identity = {
        wrapper.element_info.identity: record.element_generation
        for wrapper, record in repeated
    }
    assert by_identity == {"first": 1, "second": 2}


def test_runtime_id_reuse_after_replacement_gets_new_generation() -> None:
    backend = PywinautoUIABackend()
    old = FakeWrapper(FakeElementInfo((9, 9), "old"))
    first = backend._assign_generations(100, ((old, _record((9, 9))),))
    assert first[0][1].element_generation == 1

    replacement = FakeWrapper(FakeElementInfo((9, 9), "replacement"))
    second = backend._assign_generations(
        100,
        ((replacement, _record((9, 9))),),
    )
    assert second[0][1].element_generation == 2
