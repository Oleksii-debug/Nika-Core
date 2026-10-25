from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.interaction.domain import (
    AmbiguousTargetError,
    InteractionAction,
    StaleSnapshotError,
    UnsupportedInteractionError,
)
from nika_core.interaction.windows_uia_adapter import (
    PywinautoUIABackend,
    UIAControlRecord,
)


@dataclass
class FakeElementInfo:
    runtime_id: tuple[int, ...] | None
    identity: str
    compare_raises: bool = False
    is_control_element: bool = True

    def __eq__(self, other: object) -> bool:
        if self.compare_raises:
            raise RuntimeError("stale COM element")
        if not isinstance(other, FakeElementInfo):
            return NotImplemented
        return self.identity == other.identity


@dataclass
class FakeWrapper:
    element_info: FakeElementInfo


@dataclass
class FakeTreeWrapper(FakeWrapper):
    children: tuple[FakeWrapper, ...] = ()

    def descendants(self) -> tuple[FakeWrapper, ...]:
        return self.children


def _record(runtime_id: tuple[int, ...] | None) -> UIAControlRecord:
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


def _focus_tree(
    monkeypatch: pytest.MonkeyPatch,
    backend: PywinautoUIABackend,
    *,
    target: FakeWrapper,
) -> FakeTreeWrapper:
    root = FakeTreeWrapper(
        FakeElementInfo(None, "window-root"),
        (target,),
    )
    records = {
        id(root): _record(None),
        id(target): _record(target.element_info.runtime_id),
    }
    monkeypatch.setattr(backend, "_window", lambda _hwnd: root)
    monkeypatch.setattr(
        backend,
        "_deduplicate_same_elements",
        lambda wrappers: tuple(wrappers),
    )
    monkeypatch.setattr(backend, "_record", lambda wrapper: records[id(wrapper)])
    return root


def test_provider_native_focus_read_binds_exact_runtime_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    target = FakeWrapper(FakeElementInfo((4, 2), "addressable-control"))
    _focus_tree(monkeypatch, backend, target=target)
    focused = FakeElementInfo((4, 2), "addressable-control")
    monkeypatch.setattr(
        FakeElementInfo,
        "get_active",
        classmethod(lambda _cls: focused),
        raising=False,
    )

    assert backend.focused_identity(100) == ((4, 2), 1)


def test_provider_native_focus_compare_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    target = FakeWrapper(
        FakeElementInfo((4, 2), "addressable-control", compare_raises=True)
    )
    _focus_tree(monkeypatch, backend, target=target)
    focused = FakeElementInfo((4, 2), "addressable-control")
    monkeypatch.setattr(
        FakeElementInfo,
        "get_active",
        classmethod(lambda _cls: focused),
        raising=False,
    )

    with pytest.raises(
        AmbiguousTargetError,
        match="cannot bind provider focused element",
    ):
        backend.focused_identity(100)


def test_native_focus_hwnd_fallback_binds_only_by_compare_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    target = FakeWrapper(FakeElementInfo((4, 2), "addressable-control"))
    _focus_tree(monkeypatch, backend, target=target)
    foreign = FakeElementInfo((8, 8), "foreign-focused-element")
    native_target = FakeElementInfo((4, 2), "addressable-control")
    monkeypatch.setattr(
        FakeElementInfo,
        "get_active",
        classmethod(lambda _cls: foreign),
        raising=False,
    )
    monkeypatch.setattr(
        backend,
        "_native_focused_element_info",
        lambda _hwnd, _element_info_type: native_target,
    )

    assert backend.focused_identity(100) == ((4, 2), 1)


def test_native_focus_hwnd_fallback_rejects_foreign_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    target = FakeWrapper(FakeElementInfo((4, 2), "addressable-control"))
    _focus_tree(monkeypatch, backend, target=target)
    foreign = FakeElementInfo((8, 8), "foreign-focused-element")
    monkeypatch.setattr(
        FakeElementInfo,
        "get_active",
        classmethod(lambda _cls: None),
        raising=False,
    )
    monkeypatch.setattr(
        backend,
        "_native_focused_element_info",
        lambda _hwnd, _element_info_type: foreign,
    )

    assert backend.focused_identity(100) is None


def test_native_focus_hwnd_fallback_compare_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    target = FakeWrapper(
        FakeElementInfo((4, 2), "addressable-control", compare_raises=True)
    )
    _focus_tree(monkeypatch, backend, target=target)
    native_target = FakeElementInfo((4, 2), "addressable-control")
    monkeypatch.setattr(
        FakeElementInfo,
        "get_active",
        classmethod(lambda _cls: None),
        raising=False,
    )
    monkeypatch.setattr(
        backend,
        "_native_focused_element_info",
        lambda _hwnd, _element_info_type: native_target,
    )

    with pytest.raises(
        AmbiguousTargetError,
        match="cannot bind provider focused element",
    ):
        backend.focused_identity(100)


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
    second = backend._assign_generations(100, ((replacement, _record((9, 9))),))
    assert second[0][1].element_generation == 2


def test_absent_runtime_id_drops_stale_wrapper_without_reusing_generation() -> None:
    backend = PywinautoUIABackend()
    old = FakeWrapper(FakeElementInfo((9, 9), "old"))
    first = backend._assign_generations(100, ((old, _record((9, 9))),))
    assert first[0][1].element_generation == 1

    assert backend._assign_generations(100, ()) == ()
    old.element_info.compare_raises = True
    replacement = FakeWrapper(FakeElementInfo((9, 9), "replacement"))
    second = backend._assign_generations(100, ((replacement, _record((9, 9))),))

    assert second[0][1].element_generation == 2
    assert len(backend._tracked[100][(9, 9)]) == 1
    assert backend._tracked[100][(9, 9)][0].wrapper is replacement


def test_generation_tracking_is_bounded_by_current_live_elements() -> None:
    backend = PywinautoUIABackend()

    for expected_generation in range(1, 65):
        runtime_id = (expected_generation,)
        wrapper = FakeWrapper(
            FakeElementInfo(runtime_id, f"element-{expected_generation}")
        )
        observed = backend._assign_generations(
            100,
            ((wrapper, _record(runtime_id)),),
        )
        assert observed[0][1].element_generation == expected_generation
        assert sum(len(group) for group in backend._tracked[100].values()) == 1

    assert backend._next_element_generation == 65


def test_failed_generation_observation_does_not_commit_partial_tracking() -> None:
    backend = PywinautoUIABackend()
    original = FakeWrapper(FakeElementInfo((5,), "original"))
    first = backend._assign_generations(100, ((original, _record((5,))),))
    assert first[0][1].element_generation == 1

    duplicate_a = FakeWrapper(FakeElementInfo((8,), "duplicate"))
    duplicate_b = FakeWrapper(FakeElementInfo((8,), "duplicate"))
    with pytest.raises(AmbiguousTargetError, match="appeared twice"):
        backend._assign_generations(
            100,
            (
                (duplicate_a, _record((8,))),
                (duplicate_b, _record((8,))),
            ),
        )

    original_again = FakeWrapper(FakeElementInfo((5,), "original"))
    repeated = backend._assign_generations(
        100,
        ((original_again, _record((5,))),),
    )
    assert repeated[0][1].element_generation == 1
    assert backend._next_element_generation == 2


def test_pywinauto_backend_omits_unaddressable_elements_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    identityless = FakeWrapper(FakeElementInfo(None, "provider-artifact"))
    identified = FakeWrapper(FakeElementInfo((4, 2), "addressable-control"))
    root = FakeTreeWrapper(
        FakeElementInfo(None, "window-root"),
        (identityless, identified),
    )
    records = {
        id(root): _record(None),
        id(identityless): _record(None),
        id(identified): _record((4, 2)),
    }
    monkeypatch.setattr(backend, "_window", lambda _hwnd: root)
    monkeypatch.setattr(
        backend,
        "_deduplicate_same_elements",
        lambda wrappers: tuple(wrappers),
    )
    monkeypatch.setattr(backend, "_record", lambda wrapper: records[id(wrapper)])
    controls = backend.enumerate_controls(100, "control")
    assert len(controls) == 1
    assert controls[0].runtime_id == (4, 2)
    assert controls[0].element_generation == 1
    assert backend.last_unaddressable_count == 2


class _InvokePattern:
    def __init__(self, wrapper: "_EffectWrapper") -> None:
        self.wrapper = wrapper

    def Invoke(self) -> None:
        self.wrapper.invoked = True


class _EffectWrapper(FakeWrapper):
    def __init__(self, element_info: FakeElementInfo) -> None:
        super().__init__(element_info)
        self.invoked = False
        self.focused = False
        self.iface_invoke = _InvokePattern(self)

    def set_focus(self) -> None:
        self.focused = True


def _effect_record(
    *,
    name: str = "Save",
    enabled: bool = True,
    visible: bool = True,
    patterns: tuple[str, ...] = ("Invoke",),
) -> UIAControlRecord:
    return UIAControlRecord(
        runtime_id=(4, 2),
        automation_id="save",
        role="button",
        name=name,
        enabled=enabled,
        visible=visible,
        focused=False,
        value=None,
        bounds=(0, 0, 100, 30),
        patterns=patterns,
        element_generation=1,
    )


@pytest.mark.parametrize(
    "live",
    (
        _effect_record(name="Delete"),
        _effect_record(enabled=False),
        _effect_record(visible=False),
    ),
)
def test_guarded_action_rejects_semantic_drift_before_same_wrapper_effect(
    monkeypatch: pytest.MonkeyPatch,
    live: UIAControlRecord,
) -> None:
    backend = PywinautoUIABackend()
    wrapper = _EffectWrapper(FakeElementInfo((4, 2), "same-live-element"))
    expected = _effect_record()
    monkeypatch.setattr(
        backend,
        "_pairs",
        lambda _hwnd, _view: ((wrapper, live),),
    )

    with pytest.raises(
        (StaleSnapshotError, UnsupportedInteractionError),
        match="guarded|disabled|hidden",
    ):
        backend.guarded_action(
            100,
            (4, 2),
            1,
            expected,
            InteractionAction.INVOKE,
            None,
        )

    assert wrapper.invoked is False


def test_guarded_action_rejects_pattern_drift_before_same_wrapper_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    wrapper = _EffectWrapper(FakeElementInfo((4, 2), "same-live-element"))
    expected = _effect_record()
    live = _effect_record(patterns=())
    monkeypatch.setattr(
        backend,
        "_pairs",
        lambda _hwnd, _view: ((wrapper, live),),
    )

    with pytest.raises(UnsupportedInteractionError, match="pattern changed"):
        backend.guarded_action(
            100,
            (4, 2),
            1,
            expected,
            InteractionAction.INVOKE,
            None,
        )

    assert wrapper.invoked is False


def test_guarded_focus_rejects_semantic_drift_before_same_wrapper_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = PywinautoUIABackend()
    wrapper = _EffectWrapper(FakeElementInfo((4, 2), "same-live-element"))
    expected = _effect_record()
    live = _effect_record(name="Delete")
    monkeypatch.setattr(
        backend,
        "_pairs",
        lambda _hwnd, _view: ((wrapper, live),),
    )

    with pytest.raises(StaleSnapshotError, match="role/name drift"):
        backend.guarded_focus(100, (4, 2), 1, expected)

    assert wrapper.focused is False
