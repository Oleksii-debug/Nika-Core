"""Persistent Playwright semantic adapter for Nika computer interaction.

Playwright objects remain private to this module.  The public contracts are the framework-neutral
objects from :mod:`nika_core.interaction.domain` and the synchronous ``InteractionAdapter``
protocol consumed by ``SemanticInteractionCoordinator``.

The adapter deliberately never uses ``first()``, ``last()`` or ``nth()`` to resolve an action
target.  Zero and multiple semantic matches fail closed.  Bounds are captured only as evidence.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from .domain import (
    AmbiguousTargetError,
    BrowserContextIdentity,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
)

_ARIA_LINE: Final = re.compile(
    r'^(?P<indent>\s*)-\s+(?P<role>[A-Za-z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?:\s+\[(?P<state>[^\]]+)\])?\s*:?[\s]*$'
)
_FORM_ROLES: Final = frozenset(
    {"checkbox", "combobox", "listbox", "radio", "searchbox", "slider", "spinbutton", "textbox"}
)


@dataclass(frozen=True, slots=True)
class FrameScope:
    """Framework-neutral exact frame selector.

    Exactly one of ``name`` or ``url`` must be supplied. URL matching is exact; callers that need
    a looser rule must first discover a concrete frame and then bind its exact URL.
    """

    name: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.name is None) == (self.url is None):
            raise ValueError("FrameScope requires exactly one of name or url")
        if self.name is not None and not self.name.strip():
            raise ValueError("frame name must not be empty")
        if self.url is not None and not self.url.strip():
            raise ValueError("frame url must not be empty")


@dataclass(frozen=True, slots=True)
class DialogRule:
    dialog_type: str
    message: str
    response: str = "dismiss"
    prompt_text: str | None = None

    def __post_init__(self) -> None:
        if self.response not in {"accept", "dismiss"}:
            raise ValueError("dialog response must be accept or dismiss")


@dataclass(slots=True)
class DialogBroker:
    """Fail-closed exact dialog broker; unexpected dialogs are dismissed, never accepted."""

    _rules: list[DialogRule] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def expect(self, rule: DialogRule) -> None:
        self._rules.append(rule)

    def handle(self, dialog: Any) -> None:
        dtype = str(dialog.type)
        message = str(dialog.message)
        matching = [rule for rule in self._rules if rule.dialog_type == dtype and rule.message == message]
        if len(matching) != 1:
            dialog.dismiss()
            self.events.append((dtype, message, "unexpected-dismiss"))
            return
        rule = matching[0]
        self._rules.remove(rule)
        if rule.response == "accept":
            dialog.accept(rule.prompt_text)
        else:
            dialog.dismiss()
        self.events.append((dtype, message, rule.response))


@dataclass(slots=True)
class DownloadBroker:
    """Persist downloads only under an explicitly approved root."""

    approved_root: Path
    saved: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.approved_root = self.approved_root.expanduser().resolve()
        self.approved_root.mkdir(parents=True, exist_ok=True)

    def handle(self, download: Any) -> None:
        filename = Path(str(download.suggested_filename)).name
        if not filename or filename in {".", ".."}:
            raise UnsupportedInteractionError("download did not provide a safe filename")
        destination = (self.approved_root / filename).resolve()
        if destination.parent != self.approved_root:
            raise UnsupportedInteractionError("download path escaped approved root")
        download.save_as(str(destination))
        self.saved.append(destination)


@dataclass(slots=True)
class _PageRecord:
    page: Any
    page_id: str
    document_generation: int = 1


@dataclass(slots=True)
class PageRegistry:
    """Stable in-process identities for all pages in one browser context."""

    context: Any
    pages: dict[str, _PageRecord] = field(default_factory=dict)
    _by_object: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for page in tuple(self.context.pages):
            self.register(page)
        self.context.on("page", self.register)

    def register(self, page: Any) -> str:
        key = id(page)
        existing = self._by_object.get(key)
        if existing is not None:
            return existing
        page_id = uuid.uuid4().hex
        record = _PageRecord(page=page, page_id=page_id)
        self.pages[page_id] = record
        self._by_object[key] = page_id

        def on_frame_navigated(frame: Any, *, expected_page_id: str = page_id) -> None:
            current = self.pages.get(expected_page_id)
            if current is not None and frame == current.page.main_frame:
                current.document_generation += 1

        page.on("framenavigated", on_frame_navigated)
        page.on("close", lambda: self.remove(page_id))
        return page_id

    def remove(self, page_id: str) -> None:
        record = self.pages.pop(page_id, None)
        if record is not None:
            self._by_object.pop(id(record.page), None)

    def get(self, page_id: str) -> _PageRecord:
        record = self.pages.get(page_id)
        if record is None or record.page.is_closed():
            raise StaleSnapshotError("browser page is closed or no longer registered")
        return record


@dataclass(slots=True)
class BrowserSession:
    """Explicit Playwright lifetime with an ephemeral persistent-in-process context."""

    download_root: Path
    headless: bool = True
    timeout_ms: float = 10_000
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    context_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    dialogs: DialogBroker = field(default_factory=DialogBroker)
    _playwright: Any = field(init=False, default=None, repr=False)
    _browser: Any = field(init=False, default=None, repr=False)
    context: Any = field(init=False, default=None, repr=False)
    registry: PageRegistry | None = field(init=False, default=None)
    downloads: DownloadBroker = field(init=False)

    def __post_init__(self) -> None:
        self.downloads = DownloadBroker(self.download_root)

    def start(self) -> BrowserSession:
        if self.context is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("Playwright browser component is not installed") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        # Never attach to or copy a personal browser profile. This context persists only for the
        # lifetime of this explicit Nika session.
        self.context = self._browser.new_context(accept_downloads=True)
        self.context.set_default_timeout(self.timeout_ms)
        self.context.on("dialog", self.dialogs.handle)
        self.context.on("download", self.downloads.handle)
        self.registry = PageRegistry(self.context)
        return self

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self.context = None
            self._browser = None
            self._playwright = None
            self.registry = None

    def new_page(self) -> str:
        self.start()
        assert self.context is not None and self.registry is not None
        page = self.context.new_page()
        page_id = self.registry.register(page)
        return page_id

    def page_ids(self) -> tuple[str, ...]:
        if self.registry is None:
            return ()
        return tuple(self.registry.pages)


@dataclass(frozen=True, slots=True)
class _SemanticDescriptor:
    node_id: str
    role: str
    name: str
    ancestors: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class PlaywrightInteractionAdapter:
    """Strict semantic browser adapter backed by a persistent Playwright session."""

    session: BrowserSession
    page_id: str
    frame_scope: FrameScope | None = None
    _node_descriptors: dict[str, _SemanticDescriptor] = field(default_factory=dict, init=False)
    _last_focus: str | None = field(default=None, init=False)

    def _record(self) -> _PageRecord:
        if self.session.registry is None:
            raise StaleSnapshotError("browser session is not started")
        return self.session.registry.get(self.page_id)

    def _root(self) -> Any:
        page = self._record().page
        if self.frame_scope is None:
            return page
        frames = tuple(
            frame
            for frame in page.frames
            if (self.frame_scope.name is not None and frame.name == self.frame_scope.name)
            or (self.frame_scope.url is not None and frame.url == self.frame_scope.url)
        )
        if not frames:
            raise TargetNotFoundError(f"No frame matched {self.frame_scope!r}")
        if len(frames) != 1:
            raise AmbiguousTargetError(f"Frame target is ambiguous: {len(frames)} matches")
        return frames[0]

    def navigate(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise UnsupportedInteractionError("browser navigation permits only http/https URLs")
        page = self._record().page
        page.goto(url, wait_until="domcontentloaded")

    def set_content(self, html: str) -> None:
        self._record().page.set_content(html, wait_until="domcontentloaded")

    def _revision(self, root: Any) -> int:
        script = """
        () => {
          if (!globalThis.__nikaSemanticRevisionState) {
            const state = {revision: 1};
            const observer = new MutationObserver(() => { state.revision += 1; });
            observer.observe(document.documentElement, {
              subtree: true, childList: true, attributes: true, characterData: true
            });
            globalThis.__nikaSemanticRevisionState = state;
          }
          return globalThis.__nikaSemanticRevisionState.revision;
        }
        """
        return int(root.evaluate(script))

    @staticmethod
    def _state_attributes(raw: str | None) -> tuple[tuple[str, str], ...]:
        if not raw:
            return ()
        attrs: list[tuple[str, str]] = []
        for token in raw.split():
            if "=" in token:
                key, value = token.split("=", 1)
                attrs.append((key.strip(), value.strip().strip('"')))
            else:
                attrs.append((token.strip(), "true"))
        return tuple(attrs)

    def _parse_snapshot(self, snapshot: str) -> tuple[ControlNode, ...]:
        controls: list[ControlNode] = []
        stack: list[tuple[int, str, str, str]] = []
        descriptors: dict[str, _SemanticDescriptor] = {}
        duplicate_counter: dict[tuple[tuple[tuple[str, str], ...], str, str], int] = {}
        root = self._root()

        for raw_line in snapshot.splitlines():
            match = _ARIA_LINE.match(raw_line)
            if match is None:
                continue
            indent = len(match.group("indent").replace("\t", "  "))
            role = match.group("role").casefold()
            name = bytes(match.group("name") or "", "utf-8").decode("unicode_escape")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            ancestors = tuple((entry[1], entry[2]) for entry in stack)
            semantic_key = (ancestors, role, name)
            ordinal = duplicate_counter.get(semantic_key, 0) + 1
            duplicate_counter[semantic_key] = ordinal
            digest = hashlib.sha256(
                repr((self.page_id, self._record().document_generation, semantic_key, ordinal)).encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            node_id = f"pw:{digest}"
            state_attrs = list(self._state_attributes(match.group("state")))
            state = dict(state_attrs)
            if stack:
                state_attrs.append(("ancestor_node_id", stack[-1][3]))
            if role in _FORM_ROLES and name:
                state_attrs.append(("label", name))
            elif name:
                state_attrs.append(("text", name))
            descriptor = _SemanticDescriptor(
                node_id=node_id,
                role=role,
                name=name,
                ancestors=ancestors,
            )
            descriptors[node_id] = descriptor
            locator = self._locator_for_descriptor(descriptor, root=root)
            bounds: tuple[int, int, int, int] | None = None
            if locator.count() == 1:
                box = locator.bounding_box()
                if box is not None:
                    bounds = (
                        round(box["x"]),
                        round(box["y"]),
                        round(box["width"]),
                        round(box["height"]),
                    )
            controls.append(
                ControlNode(
                    node_id=node_id,
                    role=role,
                    name=name,
                    enabled="disabled" not in state,
                    visible=True,
                    focused="focused" in state,
                    value=state.get("value"),
                    bounds=bounds,
                    attributes=tuple(state_attrs),
                )
            )
            stack.append((indent, role, name, node_id))

        self._node_descriptors = descriptors
        return tuple(controls)

    @staticmethod
    def _semantic_locator(scope: Any, role: str, name: str) -> Any:
        kwargs: dict[str, Any] = {"exact": True}
        if name:
            kwargs["name"] = name
        return scope.get_by_role(role, **kwargs)

    def _locator_for_descriptor(self, descriptor: _SemanticDescriptor, *, root: Any | None = None) -> Any:
        scope = root if root is not None else self._root()
        for role, name in descriptor.ancestors:
            candidate = self._semantic_locator(scope, role, name)
            count = candidate.count()
            if count != 1:
                # Do not pick a positional ancestor. Leaving the scope unchanged guarantees the
                # final target will either resolve uniquely or fail closed.
                break
            scope = candidate
        return self._semantic_locator(scope, descriptor.role, descriptor.name)

    def _locator_for_node(self, node: ControlNode) -> Any:
        descriptor = self._node_descriptors.get(node.node_id)
        if descriptor is None:
            raise StaleSnapshotError("semantic node does not belong to the current observation")
        locator = self._locator_for_descriptor(descriptor)
        count = locator.count()
        if count == 0:
            raise TargetNotFoundError("semantic target disappeared before action")
        if count != 1:
            raise AmbiguousTargetError(f"semantic target became ambiguous: {count} matches")
        return locator

    def observe(self) -> SemanticSnapshot:
        record = self._record()
        root = self._root()
        revision_before = self._revision(root)
        snapshot_text = root.locator("body").aria_snapshot()
        controls = self._parse_snapshot(snapshot_text)
        revision_after = self._revision(root)
        if revision_after != revision_before:
            raise StaleSnapshotError("semantic tree mutated while it was being observed")
        browser_identity = BrowserContextIdentity(
            session_id=self.session.session_id,
            context_id=self.session.context_id,
            page_id=self.page_id,
            document_generation=record.document_generation,
        )
        return SemanticSnapshot(
            target=InteractionTarget(browser=browser_identity),
            generation=record.document_generation,
            revision=revision_after,
            controls=controls,
        )

    def capture_focus(self) -> str | None:
        if self._last_focus is not None:
            descriptor = self._node_descriptors.get(self._last_focus)
            if descriptor is not None:
                locator = self._locator_for_descriptor(descriptor)
                if locator.count() == 1 and bool(
                    locator.evaluate("el => el === document.activeElement")
                ):
                    return self._last_focus
        for node_id, descriptor in self._node_descriptors.items():
            locator = self._locator_for_descriptor(descriptor)
            if locator.count() == 1 and bool(locator.evaluate("el => el === document.activeElement")):
                self._last_focus = node_id
                return node_id
        self._last_focus = None
        return None

    def focus(self, node: ControlNode) -> None:
        locator = self._locator_for_node(node)
        locator.focus()
        if not bool(locator.evaluate("el => el === document.activeElement")):
            raise StaleSnapshotError("Playwright could not verify focus on semantic target")
        self._last_focus = node.node_id

    def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None:
        locator = self._locator_for_node(node)
        if action is InteractionAction.FOCUS:
            self.focus(node)
            return
        if action is InteractionAction.INVOKE:
            locator.click()
            return
        if action is InteractionAction.SET_VALUE:
            if value is None:
                raise ValueError("SET_VALUE requires a value")
            locator.fill(value)
            return
        if action is InteractionAction.SELECT:
            if value is None:
                raise ValueError("SELECT requires a value")
            locator.select_option(label=value)
            return
        if action is InteractionAction.TOGGLE:
            if node.role not in {"checkbox", "radio", "switch"}:
                raise UnsupportedInteractionError("TOGGLE requires checkbox/radio/switch semantics")
            locator.click()
            return
        if action in {InteractionAction.EXPAND, InteractionAction.COLLAPSE}:
            expected = action is InteractionAction.EXPAND
            before = locator.get_attribute("aria-expanded")
            if before is None:
                raise UnsupportedInteractionError("target does not expose aria-expanded semantics")
            if (before.casefold() == "true") != expected:
                locator.click()
            return
        raise UnsupportedInteractionError(f"unsupported browser action: {action.value}")

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool:
        descriptor = self._node_descriptors.get(node.node_id)
        # A navigation invalidates old node identity by design. For an invocation, a changed
        # document generation is itself a deterministic postcondition.
        if after.target.browser is not None and before.target.browser is not None:
            if after.target.browser.document_generation != before.target.browser.document_generation:
                return action is InteractionAction.INVOKE
        if action is InteractionAction.INVOKE:
            # Require observable semantic state change; a successful low-level click alone is not
            # accepted as proof that the requested external/UI effect occurred.
            return after.revision != before.revision
        if descriptor is None:
            return False
        locator = self._locator_for_descriptor(descriptor)
        if locator.count() != 1:
            return False
        if action is InteractionAction.FOCUS:
            return bool(locator.evaluate("el => el === document.activeElement"))
        if action is InteractionAction.SET_VALUE:
            return value is not None and locator.input_value() == value
        if action is InteractionAction.SELECT:
            return value is not None and locator.locator("option:checked").text_content() == value
        if action is InteractionAction.TOGGLE:
            return after.revision != before.revision
        if action in {InteractionAction.EXPAND, InteractionAction.COLLAPSE}:
            expected = "true" if action is InteractionAction.EXPAND else "false"
            return locator.get_attribute("aria-expanded") == expected
        return False
