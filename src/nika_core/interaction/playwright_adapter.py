"""Persistent strict Playwright adapter for Nika semantic computer interaction.

Playwright objects stay private to this module. Action targets are never selected with positional
``first/last/nth`` escape hatches. Bounds are evidence only and never participate in resolution.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

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
    r'(?:\s+\[(?P<state>[^\]]+)\])?'
    r'(?:\s*:\s*(?P<scalar>.*))?$'
)
_STATE_TOKEN: Final = re.compile(r'(?P<key>[\w-]+)(?:=(?P<value>"(?:[^"\\]|\\.)*"|\S+))?')
_FORM_ROLES: Final = frozenset(
    {"checkbox", "combobox", "listbox", "searchbox", "slider", "spinbutton", "textbox"}
)
_NON_CONTROL_SNAPSHOT_ROLES: Final = frozenset({"text"})


@dataclass(frozen=True, slots=True)
class FrameScope:
    """Exact frame identity; never positional."""

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
    """Unexpected dialogs are dismissed; acceptance requires one exact queued rule."""

    _rules: list[DialogRule] = field(default_factory=list)
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def expect(self, rule: DialogRule) -> None:
        self._rules.append(rule)

    def handle(self, dialog: Any) -> None:
        dtype = str(dialog.type)
        message = str(dialog.message)
        matches = [rule for rule in self._rules if rule.dialog_type == dtype and rule.message == message]
        if len(matches) != 1:
            dialog.dismiss()
            self.events.append((dtype, message, "unexpected-dismiss"))
            return
        rule = matches[0]
        self._rules.remove(rule)
        if rule.response == "accept":
            dialog.accept(rule.prompt_text)
        else:
            dialog.dismiss()
        self.events.append((dtype, message, rule.response))


@dataclass(slots=True)
class DownloadBroker:
    """Persist browser downloads only beneath an explicitly approved artifact root."""

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
class _FrameRecord:
    frame: Any
    frame_id: str
    document_generation: int = 1


@dataclass(slots=True)
class _PageRecord:
    page: Any
    page_id: str
    document_generation: int = 1
    frames: dict[str, _FrameRecord] = field(default_factory=dict)
    frame_by_object: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class PageRegistry:
    """Stable per-session identities for pages, frames, popups and tabs."""

    context: Any
    pages: dict[str, _PageRecord] = field(default_factory=dict)
    _by_object: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for page in tuple(self.context.pages):
            self.register(page)
        # Playwright annotates callback objects internally. Never pass a bound method whose owner
        # is a slotted dataclass; use an ordinary lambda instead.
        self.context.on("page", lambda page: self.register(page))

    @staticmethod
    def _frame_is_main(record: _PageRecord, frame: Any) -> bool:
        return frame == record.page.main_frame

    def _register_frame(self, record: _PageRecord, frame: Any) -> _FrameRecord:
        object_id = id(frame)
        existing_id = record.frame_by_object.get(object_id)
        if existing_id is not None:
            return record.frames[existing_id]
        frame_id = "main" if self._frame_is_main(record, frame) else uuid.uuid4().hex
        if frame_id == "main" and frame_id in record.frames:
            # A replaced main-frame wrapper must never inherit a stale object binding.
            frame_id = uuid.uuid4().hex
        frame_record = _FrameRecord(frame=frame, frame_id=frame_id)
        record.frames[frame_id] = frame_record
        record.frame_by_object[object_id] = frame_id
        return frame_record

    def register(self, page: Any) -> str:
        object_id = id(page)
        existing = self._by_object.get(object_id)
        if existing is not None:
            return existing
        page_id = uuid.uuid4().hex
        record = _PageRecord(page=page, page_id=page_id)
        self.pages[page_id] = record
        self._by_object[object_id] = page_id
        self._register_frame(record, page.main_frame)
        for frame in tuple(page.frames):
            self._register_frame(record, frame)

        def on_frame_attached(frame: Any, *, registered_page_id: str = page_id) -> None:
            current = self.pages.get(registered_page_id)
            if current is not None:
                self._register_frame(current, frame)

        def on_navigation(frame: Any, *, registered_page_id: str = page_id) -> None:
            current = self.pages.get(registered_page_id)
            if current is None:
                return
            frame_record = self._register_frame(current, frame)
            frame_record.document_generation += 1
            if self._frame_is_main(current, frame):
                current.document_generation += 1

        page.on("frameattached", on_frame_attached)
        page.on("framenavigated", on_navigation)
        page.on("close", lambda _page: self.remove(page_id))
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

    def frame_identity(self, page_id: str, frame: Any) -> tuple[str, int]:
        record = self.get(page_id)
        frame_record = self._register_frame(record, frame)
        return frame_record.frame_id, frame_record.document_generation

    def frame_by_identity(
        self,
        page_id: str,
        frame_id: str,
        document_generation: int,
    ) -> Any:
        record = self.get(page_id)
        frame_record = record.frames.get(frame_id)
        if frame_record is None or frame_record.document_generation != document_generation:
            raise StaleSnapshotError("browser frame identity or document generation changed")
        if frame_record.frame not in tuple(record.page.frames):
            raise StaleSnapshotError("browser frame detached after observation")
        is_detached = getattr(frame_record.frame, "is_detached", None)
        if callable(is_detached) and bool(is_detached()):
            raise StaleSnapshotError("browser frame detached after observation")
        return frame_record.frame


@dataclass(slots=True)
class BrowserSession:
    """Explicit Playwright lifetime using an ephemeral, non-personal BrowserContext."""

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
        self.context = self._browser.new_context(accept_downloads=True)
        self.context.set_default_timeout(self.timeout_ms)
        # See PageRegistry: wrappers avoid Playwright mutating bound methods of slotted owners.
        self.context.on("dialog", lambda dialog: self.dialogs.handle(dialog))
        self.context.on("download", lambda download: self.downloads.handle(download))
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
        return self.registry.register(self.context.new_page())

    def page_ids(self) -> tuple[str, ...]:
        return () if self.registry is None else tuple(self.registry.pages)


@dataclass(frozen=True, slots=True)
class _SemanticDescriptor:
    node_id: str
    role: str
    name: str
    text: str | None
    ancestors: tuple[tuple[str, str, str | None], ...]
    page_document_generation: int
    frame_id: str
    frame_document_generation: int
    element_token: str | None


@dataclass(slots=True)
class PlaywrightInteractionAdapter:
    """Synchronous strict-semantic adapter consumed by ``SemanticInteractionCoordinator``."""

    session: BrowserSession
    page_id: str
    frame_scope: FrameScope | None = None
    _node_descriptors: dict[str, _SemanticDescriptor] = field(default_factory=dict, init=False)
    _last_focus: str | None = field(default=None, init=False)
    _pre_action_pages: frozenset[str] = field(default_factory=frozenset, init=False)
    _pre_action_download_count: int = field(default=0, init=False)

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

    def _frame_identity(self, root: Any) -> tuple[str, int]:
        record = self._record()
        if root is record.page:
            return "main", record.document_generation
        registry = self.session.registry
        frame_identity = getattr(registry, "frame_identity", None)
        if callable(frame_identity):
            return frame_identity(self.page_id, root)
        return f"object:{id(root)}", 1

    def _observation_roots(self) -> tuple[tuple[Any, str, int], ...]:
        record = self._record()
        if self.frame_scope is not None:
            root = self._root()
            frame_id, generation = self._frame_identity(root)
            return ((root, frame_id, generation),)

        roots: list[tuple[Any, str, int]] = [
            (record.page, "main", record.document_generation)
        ]
        for frame in tuple(record.page.frames):
            if frame == record.page.main_frame:
                continue
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached) and bool(is_detached()):
                continue
            frame_id, generation = self._frame_identity(frame)
            roots.append((frame, frame_id, generation))
        return tuple(roots)

    def _root_for_descriptor(self, descriptor: _SemanticDescriptor) -> Any:
        record = self._record()
        if record.document_generation != descriptor.page_document_generation:
            raise StaleSnapshotError("browser page document changed after observation")
        if descriptor.frame_id == "main":
            if descriptor.frame_document_generation != record.document_generation:
                raise StaleSnapshotError("browser main document generation changed")
            return record.page

        registry = self.session.registry
        frame_by_identity = getattr(registry, "frame_by_identity", None)
        if callable(frame_by_identity):
            return frame_by_identity(
                self.page_id,
                descriptor.frame_id,
                descriptor.frame_document_generation,
            )

        for frame in tuple(record.page.frames):
            frame_id, generation = self._frame_identity(frame)
            if (
                frame_id == descriptor.frame_id
                and generation == descriptor.frame_document_generation
            ):
                return frame
        raise StaleSnapshotError("browser frame identity changed after observation")

    def load_inline_fixture(self, html: str) -> None:
        """Load local proof/test HTML; this helper is not an agent navigation action surface."""
        record = self._record()
        record.page.set_content(html, wait_until="domcontentloaded")
        record.document_generation += 1

    @staticmethod
    def _decode_aria_name(raw: str | None) -> str:
        return "" if raw is None else str(json.loads(f'"{raw}"'))

    @staticmethod
    def _decode_scalar(raw: str | None) -> str | None:
        if raw is None:
            return None
        value = raw.strip()
        if not value:
            return None
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            try:
                return str(json.loads(value))
            except json.JSONDecodeError:
                return value[1:-1]
        return value

    @staticmethod
    def _state_attributes(raw: str | None) -> tuple[tuple[str, str], ...]:
        if not raw:
            return ()
        attributes: list[tuple[str, str]] = []
        for match in _STATE_TOKEN.finditer(raw):
            key = match.group("key")
            encoded = match.group("value")
            if encoded is None:
                value = "true"
            elif encoded.startswith('"'):
                try:
                    value = str(json.loads(encoded))
                except json.JSONDecodeError:
                    value = encoded[1:-1]
            else:
                value = encoded
            attributes.append((key, value))
        return tuple(attributes)

    def _mutation_counter(self, root: Any) -> int:
        return int(
            root.evaluate(
                """
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
            )
        )

    @staticmethod
    def _semantic_revision(snapshot_text: str, mutation_counter: int) -> int:
        normalized = snapshot_text.replace("[focused]", "")
        digest = hashlib.sha256(f"{mutation_counter}\0{normalized}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    @staticmethod
    def _semantic_locator(scope: Any, role: str, name: str, text: str | None = None) -> Any:
        kwargs: dict[str, Any] = {"exact": True}
        if name:
            kwargs["name"] = name
        locator = scope.get_by_role(role, **kwargs)
        if not name and text is not None:
            locator = locator.filter(has_text=re.compile(rf"^{re.escape(text)}$"))
        return locator

    @staticmethod
    def _element_instance_token(locator: Any) -> str | None:
        if locator.count() != 1:
            return None
        evaluate = getattr(locator, "evaluate", None)
        if not callable(evaluate):
            # Deterministic unit fakes cannot expose DOM object identity. Frame/document identity
            # remains enforced there; real Playwright targets always expose evaluate().
            return "semantic-only"
        token = evaluate(
            """
            el => {
              if (!globalThis.__nikaSemanticNodeIdentityState) {
                Object.defineProperty(globalThis, '__nikaSemanticNodeIdentityState', {
                  value: {next: 1, ids: new WeakMap()},
                  enumerable: false,
                  configurable: false,
                  writable: false
                });
              }
              const state = globalThis.__nikaSemanticNodeIdentityState;
              let value = state.ids.get(el);
              if (value === undefined) {
                value = state.next;
                state.next += 1;
                state.ids.set(el, value);
              }
              return String(value);
            }
            """
        )
        return str(token)

    def _locator_for_descriptor(
        self,
        descriptor: _SemanticDescriptor,
        *,
        root: Any | None = None,
    ) -> Any:
        scope = self._root_for_descriptor(descriptor) if root is None else root
        for role, name, text in descriptor.ancestors:
            candidate = self._semantic_locator(scope, role, name, text)
            if candidate.count() != 1:
                break
            scope = candidate
        return self._semantic_locator(scope, descriptor.role, descriptor.name, descriptor.text)

    def _parse_snapshot(
        self,
        snapshot: str,
        *,
        root: Any,
        page_document_generation: int,
        frame_id: str,
        frame_document_generation: int,
    ) -> tuple[ControlNode, ...]:
        controls: list[ControlNode] = []
        stack: list[tuple[int, str, str, str | None, str]] = []
        occurrences: dict[
            tuple[tuple[tuple[str, str, str | None], ...], str, str, str | None], int
        ] = {}

        for raw_line in snapshot.splitlines():
            match = _ARIA_LINE.match(raw_line)
            if match is None:
                continue
            role = match.group("role").casefold()
            if role in _NON_CONTROL_SNAPSHOT_ROLES:
                continue
            indent = len(match.group("indent").replace("\t", "  "))
            name = self._decode_aria_name(match.group("name"))
            scalar = self._decode_scalar(match.group("scalar"))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            ancestor_semantics = tuple((entry[1], entry[2], entry[3]) for entry in stack)
            descriptor_text = None if role in _FORM_ROLES or name else scalar
            semantic_key = (ancestor_semantics, role, name, descriptor_text)
            ordinal = occurrences.get(semantic_key, 0) + 1
            occurrences[semantic_key] = ordinal

            provisional = _SemanticDescriptor(
                node_id="",
                role=role,
                name=name,
                text=descriptor_text,
                ancestors=ancestor_semantics,
                page_document_generation=page_document_generation,
                frame_id=frame_id,
                frame_document_generation=frame_document_generation,
                element_token=None,
            )
            locator = self._locator_for_descriptor(provisional, root=root)
            element_token = self._element_instance_token(locator)
            node_id = "pw:" + hashlib.sha256(
                repr(
                    (
                        self.page_id,
                        page_document_generation,
                        frame_id,
                        frame_document_generation,
                        semantic_key,
                        ordinal,
                        element_token,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]

            attrs = list(self._state_attributes(match.group("state")))
            state = dict(attrs)
            if stack:
                attrs.append(("ancestor_node_id", stack[-1][4]))
            if role in _FORM_ROLES:
                if name:
                    attrs.append(("label", name))
                if scalar is not None:
                    attrs.append(("value", scalar))
            elif name:
                attrs.append(("text", name))
            elif scalar is not None:
                attrs.append(("text", scalar))

            descriptor = _SemanticDescriptor(
                node_id=node_id,
                role=role,
                name=name,
                text=descriptor_text,
                ancestors=ancestor_semantics,
                page_document_generation=page_document_generation,
                frame_id=frame_id,
                frame_document_generation=frame_document_generation,
                element_token=element_token,
            )
            self._node_descriptors[node_id] = descriptor
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
                    value=scalar if role in _FORM_ROLES else None,
                    bounds=bounds,
                    attributes=tuple(attrs),
                )
            )
            stack.append((indent, role, name, descriptor_text, node_id))

        return tuple(controls)

    def _locator_for_node(self, node: ControlNode) -> Any:
        descriptor = self._node_descriptors.get(node.node_id)
        if descriptor is None:
            raise StaleSnapshotError("semantic node does not belong to current observation")
        locator = self._locator_for_descriptor(descriptor)
        count = locator.count()
        if count == 0:
            raise TargetNotFoundError("semantic target disappeared before action")
        if count != 1:
            raise AmbiguousTargetError(f"semantic target became ambiguous: {count} matches")
        if self._element_instance_token(locator) != descriptor.element_token:
            raise StaleSnapshotError("semantic target DOM identity changed before action")
        is_visible = getattr(locator, "is_visible", None)
        if node.visible and callable(is_visible) and not bool(is_visible()):
            raise StaleSnapshotError("semantic target became hidden before action")
        is_enabled = getattr(locator, "is_enabled", None)
        if node.enabled and callable(is_enabled) and not bool(is_enabled()):
            raise StaleSnapshotError("semantic target became disabled before action")
        return locator

    @staticmethod
    def _text_entry_kind(locator: Any) -> str:
        kind = locator.evaluate(
            """
            el => {
              const tag = el.tagName.toLowerCase();
              if (tag === 'input') return 'input';
              if (tag === 'textarea') return 'textarea';
              if (el.isContentEditable) return 'contenteditable';
              return 'unsupported';
            }
            """
        )
        return str(kind)

    @classmethod
    def _text_entry_value(cls, locator: Any) -> str:
        kind = cls._text_entry_kind(locator)
        if kind in {"input", "textarea"}:
            return str(locator.input_value())
        if kind == "contenteditable":
            text = locator.text_content()
            return "" if text is None else str(text)
        raise UnsupportedInteractionError(
            "SET_VALUE requires input, textarea, or contenteditable semantics"
        )

    def observe(self) -> SemanticSnapshot:
        record = self._record()
        self._node_descriptors = {}
        controls: list[ControlNode] = []
        revision_parts: list[str] = []
        roots = self._observation_roots()

        for root, frame_id, frame_generation in roots:
            mutation_before = self._mutation_counter(root)
            snapshot_text = root.locator("body").aria_snapshot()
            mutation_after = self._mutation_counter(root)
            if mutation_after != mutation_before:
                raise StaleSnapshotError("semantic tree mutated while being observed")
            controls.extend(
                self._parse_snapshot(
                    snapshot_text,
                    root=root,
                    page_document_generation=record.document_generation,
                    frame_id=frame_id,
                    frame_document_generation=frame_generation,
                )
            )
            revision_parts.append(
                f"{frame_id}\0{frame_generation}\0{mutation_after}\0{snapshot_text}"
            )

        if self.frame_scope is None:
            scope_material = repr(tuple((frame_id, generation) for _, frame_id, generation in roots))
            scope_generation = int.from_bytes(
                hashlib.sha256(scope_material.encode("utf-8")).digest()[:8],
                "big",
            )
            frame_id = "*"
            frame_document_generation = scope_generation
        else:
            _, frame_id, frame_document_generation = roots[0]

        browser = BrowserContextIdentity(
            session_id=self.session.session_id,
            context_id=self.session.context_id,
            page_id=self.page_id,
            document_generation=record.document_generation,
            frame_id=frame_id,
            frame_document_generation=frame_document_generation,
        )
        revision_text = "\n".join(revision_parts)
        return SemanticSnapshot(
            target=InteractionTarget(browser=browser),
            generation=record.document_generation,
            revision=self._semantic_revision(revision_text, len(roots)),
            controls=tuple(controls),
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
            self._pre_action_pages = frozenset(self.session.page_ids())
            self._pre_action_download_count = len(self.session.downloads.saved)
            locator.click()
            return
        if action is InteractionAction.SET_VALUE:
            if value is None:
                raise ValueError("SET_VALUE requires a value")
            kind = self._text_entry_kind(locator)
            if kind == "unsupported":
                raise UnsupportedInteractionError(
                    "SET_VALUE requires input, textarea, or contenteditable semantics"
                )
            if not bool(locator.is_editable()):
                raise UnsupportedInteractionError(f"SET_VALUE target is not editable ({kind})")
            # SET_VALUE is intentionally fill/replace semantics. Empty text clears the editor;
            # appending requires a distinct future contract rather than implicit keyboard behavior.
            locator.fill(value)
            return
        if action is InteractionAction.SELECT:
            if value is None:
                raise ValueError("SELECT requires a value")
            count = int(
                locator.evaluate(
                    "(el, label) => Array.from(el.options ?? []).filter(o => o.label === label).length",
                    value,
                )
            )
            if count == 0:
                raise TargetNotFoundError("no select option has requested exact label")
            if count != 1:
                raise AmbiguousTargetError(f"select option label is ambiguous: {count} matches")
            locator.select_option(label=value)
            return
        if action is InteractionAction.TOGGLE:
            if node.role not in {"checkbox", "switch"}:
                raise UnsupportedInteractionError("TOGGLE requires checkbox/switch semantics")
            locator.click()
            return
        if action in {InteractionAction.EXPAND, InteractionAction.COLLAPSE}:
            expected = action is InteractionAction.EXPAND
            current = locator.get_attribute("aria-expanded")
            if current is None:
                raise UnsupportedInteractionError("target does not expose aria-expanded semantics")
            if (current.casefold() == "true") != expected:
                locator.click()
            return
        raise UnsupportedInteractionError(f"unsupported browser action: {action.value}")

    @staticmethod
    def _snapshot_node(snapshot: SemanticSnapshot, node_id: str) -> ControlNode | None:
        matches = tuple(candidate for candidate in snapshot.controls if candidate.node_id == node_id)
        return matches[0] if len(matches) == 1 else None

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool:
        if (
            before.target.browser is not None
            and after.target.browser is not None
            and (
                before.target.browser.document_generation
                != after.target.browser.document_generation
                or before.target.browser.frame_id != after.target.browser.frame_id
                or before.target.browser.frame_document_generation
                != after.target.browser.frame_document_generation
            )
        ):
            return action is InteractionAction.INVOKE
        if action is InteractionAction.INVOKE:
            return (
                after.revision != before.revision
                or frozenset(self.session.page_ids()) != self._pre_action_pages
                or len(self.session.downloads.saved) > self._pre_action_download_count
            )
        descriptor = self._node_descriptors.get(node.node_id)
        if descriptor is None:
            return False
        locator = self._locator_for_descriptor(descriptor)
        if locator.count() != 1:
            return False
        if action is InteractionAction.FOCUS:
            return bool(locator.evaluate("el => el === document.activeElement"))
        if action is InteractionAction.SET_VALUE:
            return value is not None and self._text_entry_value(locator) == value
        if action is InteractionAction.SELECT:
            selected = locator.evaluate(
                "el => el.selectedOptions && el.selectedOptions.length === 1 "
                "? el.selectedOptions[0].label : null"
            )
            return value is not None and selected == value
        if action is InteractionAction.TOGGLE:
            before_node = self._snapshot_node(before, node.node_id)
            after_node = self._snapshot_node(after, node.node_id)
            if before_node is None or after_node is None:
                return False
            return dict(before_node.attributes).get("checked") != dict(after_node.attributes).get(
                "checked"
            )
        if action in {InteractionAction.EXPAND, InteractionAction.COLLAPSE}:
            expected = "true" if action is InteractionAction.EXPAND else "false"
            return locator.get_attribute("aria-expanded") == expected
        return False
