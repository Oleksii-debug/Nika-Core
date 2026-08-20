from __future__ import annotations

from dataclasses import dataclass

from nika_core.interaction.windows_uia_adapter import PywinautoUIABackend


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


def test_identical_wrappers_with_same_runtime_id_are_collapsed() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((1, 2, 3), "same-live-element"))
    duplicate = FakeWrapper(FakeElementInfo((1, 2, 3), "same-live-element"))

    assert backend._dedupe_same_elements((first, duplicate)) == (first,)


def test_distinct_elements_with_duplicate_runtime_id_are_preserved_for_fail_closed_layer() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo((1, 2, 3), "first"))
    distinct = FakeWrapper(FakeElementInfo((1, 2, 3), "second"))

    assert backend._dedupe_same_elements((first, distinct)) == (first, distinct)


def test_compare_failure_is_not_treated_as_same_element() -> None:
    backend = PywinautoUIABackend()
    stale = FakeWrapper(FakeElementInfo((1, 2, 3), "same", compare_raises=True))
    candidate = FakeWrapper(FakeElementInfo((1, 2, 3), "same"))

    assert backend._dedupe_same_elements((stale, candidate)) == (stale, candidate)


def test_missing_runtime_ids_are_never_collapsed_positionally() -> None:
    backend = PywinautoUIABackend()
    first = FakeWrapper(FakeElementInfo(None, "same-live-element"))
    duplicate = FakeWrapper(FakeElementInfo(None, "same-live-element"))

    assert backend._dedupe_same_elements((first, duplicate)) == (first, duplicate)
