"""QA_ONLY adversarial oracle for V0.1 iframe semantic identity.

The fixture is deliberately framework-thin: it drives the canonical Playwright adapter with a
controlled page/frame object model and exact ARIA snapshots. No CSS, XPath, coordinates, nth/first,
or positional target selection is used.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nika_core.interaction import (
    AmbiguousTargetError,
    ControlLocator,
    FrameScope,
    InteractionAction,
    PlaywrightInteractionAdapter,
    StaleSnapshotError,
    TargetNotFoundError,
    resolve_strict,
)

_RUN = ControlLocator(role="button", name="Run")
_RUN_SNAPSHOT = '- button "Run"'


class _BodyLocator:
    def __init__(self, owner: _SemanticRoot) -> None:
        self._owner = owner

    def aria_snapshot(self) -> str:
        return self._owner.snapshot_text


class _SemanticLocator:
    def __init__(self, owner: _SemanticRoot, count: int) -> None:
        self._owner = owner
        self._count = count

    def count(self) -> int:
        return self._count

    def bounding_box(self) -> None:
        return None

    def click(self) -> None:
        if self._count != 1:
            raise AssertionError("test locator click requires one exact semantic match")
        self._owner.effects += 1


class _SemanticRoot:
    def __init__(self, *, name: str, url: str, snapshot_text: str = _RUN_SNAPSHOT) -> None:
        self.name = name
        self.url = url
        self.snapshot_text = snapshot_text
        self.effects = 0
        self.revision = 1

    def evaluate(self, _expression: str) -> int:
        return self.revision

    def locator(self, selector: str) -> _BodyLocator:
        if selector != "body":
            raise AssertionError(f"unexpected test selector: {selector}")
        return _BodyLocator(self)

    def get_by_role(self, role: str, **kwargs: Any) -> _SemanticLocator:
        name = str(kwargs.get("name", ""))
        count = 1 if (role, name) == ("button", "Run") else 0
        return _SemanticLocator(self, count)


class _Page(_SemanticRoot):
    def __init__(self) -> None:
        super().__init__(name="", url="https://fixture.invalid/root")
        self.main_frame = self
        self.frames: list[_SemanticRoot] = [self]

    def is_closed(self) -> bool:
        return False


class _Registry:
    def __init__(self, page: _Page, page_id: str, generation: int = 11) -> None:
        self.page_id = page_id
        self.record = SimpleNamespace(
            page=page,
            page_id=page_id,
            document_generation=generation,
        )

    def get(self, page_id: str) -> Any:
        if page_id != self.page_id:
            raise StaleSnapshotError("test page identity is stale")
        return self.record


class _Session:
    def __init__(self, page: _Page, page_id: str) -> None:
        self.session_id = "session-w36"
        self.context_id = "context-w36"
        self.registry = _Registry(page, page_id)
        self.downloads = SimpleNamespace(saved=[])
        self._page_id = page_id

    def page_ids(self) -> tuple[str, ...]:
        return (self._page_id,)


def _controlled_page() -> tuple[
    _Page,
    _SemanticRoot,
    PlaywrightInteractionAdapter,
    PlaywrightInteractionAdapter,
]:
    page_id = "page-w36"
    page = _Page()
    child = _SemanticRoot(name="runner", url="https://fixture.invalid/frame-v1")
    page.frames.append(child)
    session = _Session(page, page_id)
    root_adapter = PlaywrightInteractionAdapter(session=session, page_id=page_id)
    frame_adapter = PlaywrightInteractionAdapter(
        session=session,
        page_id=page_id,
        frame_scope=FrameScope(name="runner"),
    )
    return page, child, root_adapter, frame_adapter


def test_explicit_frame_scope_targets_child_not_root() -> None:
    page, child, _root_adapter, frame_adapter = _controlled_page()
    snapshot = frame_adapter.observe()
    node = resolve_strict(snapshot, _RUN)

    frame_adapter.act(node, InteractionAction.INVOKE, None)

    assert child.effects == 1
    assert page.effects == 0


def test_root_and_frame_semantic_identity_must_not_collapse() -> None:
    _page, _child, root_adapter, frame_adapter = _controlled_page()
    root_snapshot = root_adapter.observe()
    frame_snapshot = frame_adapter.observe()
    root_node = resolve_strict(root_snapshot, _RUN)
    frame_node = resolve_strict(frame_snapshot, _RUN)

    assert root_snapshot.target != frame_snapshot.target, (
        "root and child-frame observations share one BrowserContextIdentity; "
        "frame identity is missing"
    )
    assert root_node.node_id != frame_node.node_id, (
        "root and child-frame controls with identical role/name collapsed to one semantic node id"
    )


def test_unscoped_same_semantics_must_not_silently_choose_root() -> None:
    page, child, root_adapter, _frame_adapter = _controlled_page()

    with pytest.raises(AmbiguousTargetError):
        snapshot = root_adapter.observe()
        resolve_strict(snapshot, _RUN)

    assert page.effects == 0
    assert child.effects == 0


def test_ambiguous_frame_scope_fails_before_effect() -> None:
    page, first, _root_adapter, _frame_adapter = _controlled_page()
    second = _SemanticRoot(name="runner", url="https://fixture.invalid/frame-v2")
    page.frames.append(second)
    session = _Session(page, "page-w36")
    ambiguous = PlaywrightInteractionAdapter(
        session=session,
        page_id="page-w36",
        frame_scope=FrameScope(name="runner"),
    )

    with pytest.raises(AmbiguousTargetError):
        ambiguous.observe()

    assert page.effects == 0
    assert first.effects == 0
    assert second.effects == 0


def test_detached_frame_fails_typed_without_effect() -> None:
    page, child, _root_adapter, frame_adapter = _controlled_page()
    snapshot = frame_adapter.observe()
    node = resolve_strict(snapshot, _RUN)
    page.frames = [page]

    with pytest.raises((StaleSnapshotError, TargetNotFoundError)):
        frame_adapter.act(node, InteractionAction.INVOKE, None)

    assert page.effects == 0
    assert child.effects == 0


def test_frame_navigation_invalidates_old_semantic_target_and_never_hits_replacement() -> None:
    page, old_child, _root_adapter, frame_adapter = _controlled_page()
    before = frame_adapter.observe()
    old_node = resolve_strict(before, _RUN)

    replacement = _SemanticRoot(name="runner", url="https://fixture.invalid/frame-v2")
    page.frames = [page, replacement]

    try:
        frame_adapter.act(old_node, InteractionAction.INVOKE, None)
    except StaleSnapshotError:
        pass
    else:
        pytest.fail("old iframe semantic target remained actionable after child-frame navigation")

    assert old_child.effects == 0
    assert replacement.effects == 0, "stale target acted on the replacement frame document"
