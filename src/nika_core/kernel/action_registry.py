from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

_MODIFIER_ALIASES = {
    "alt": "alt",
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "win": "win",
    "windows": "win",
    "meta": "win",
    "super": "win",
}
_MODIFIER_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}
_MODIFIER_DISPLAY = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}

_MAX_KEYMAP_IMPORT_BYTES = 1_048_576
_MAX_KEYMAP_BINDING_BYTES = 256


def _require_canonical_action_text(value: str, *, field: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be canonical UTF-8 text") from exc
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must be canonical UTF-8 text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value):
        raise ValueError(f"{field} must be canonical UTF-8 text")
    return value


def _unique_json_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate keymap JSON member")
        result[key] = value
    return result


def _reject_json_noninteger(_value: str) -> object:
    raise ValueError("keymap JSON must not contain floating-point or non-finite numbers")


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    action_id: str
    label: str
    category: str
    default_binding: str | None = None
    scope: str = "app"
    may_be_unbound: bool = True

    def __post_init__(self) -> None:
        if type(self.action_id) is not str:
            raise TypeError("action_id must be text")
        if (
            type(self.label) is not str
            or type(self.category) is not str
            or type(self.scope) is not str
        ):
            raise TypeError("action metadata must be text")
        if self.default_binding is not None and type(self.default_binding) is not str:
            raise TypeError("default binding must be text or null")
        if type(self.may_be_unbound) is not bool:
            raise TypeError("may_be_unbound must be a boolean")
        _require_canonical_action_text(self.action_id, field="action_id")
        _require_canonical_action_text(self.label, field="action label")
        _require_canonical_action_text(self.category, field="action category")
        _require_canonical_action_text(self.scope, field="action scope")
        if not self.action_id.strip() or "." not in self.action_id:
            raise ValueError("action_id must be a stable dotted identifier")
        if not self.label.strip() or not self.category.strip() or not self.scope.strip():
            raise ValueError("action metadata must not be empty")
        if self.default_binding is None and not self.may_be_unbound:
            raise ValueError("required action must have a default binding")
        if self.default_binding is not None:
            _binding_key(self.default_binding)


def _snapshot_action_definition(definition: ActionDefinition) -> ActionDefinition:
    return ActionDefinition(
        action_id=definition.action_id,
        label=definition.label,
        category=definition.category,
        default_binding=definition.default_binding,
        scope=definition.scope,
        may_be_unbound=definition.may_be_unbound,
    )


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {}

    def register(self, definition: ActionDefinition) -> None:
        snapshot = _snapshot_action_definition(definition)
        if snapshot.action_id in self._actions:
            raise ValueError(f"duplicate action_id: {snapshot.action_id}")
        if snapshot.default_binding is not None:
            conflict = self.find_by_binding(snapshot.default_binding, snapshot.scope)
            if conflict is not None:
                raise ValueError(
                    f"default binding conflict: {snapshot.default_binding} already belongs to "
                    f"{conflict.action_id}"
                )
        self._actions[snapshot.action_id] = snapshot

    def get(self, action_id: str) -> ActionDefinition:
        try:
            action = self._actions[action_id]
        except KeyError as exc:
            raise KeyError(f"Unknown action: {action_id}") from exc
        return _snapshot_action_definition(action)

    def all(self) -> tuple[ActionDefinition, ...]:
        return tuple(
            _snapshot_action_definition(self._actions[key])
            for key in sorted(self._actions)
        )

    def find_by_binding(self, binding: str, scope: str) -> ActionDefinition | None:
        wanted = _binding_key(binding)
        for action in self._actions.values():
            if (
                action.scope == scope
                and action.default_binding is not None
                and _binding_key(action.default_binding) == wanted
            ):
                return _snapshot_action_definition(action)
        return None


class Keymap:
    FORMAT_VERSION = 1

    def __init__(self, store: SQLiteStore, actions: ActionRegistry) -> None:
        self._store = store
        self._actions = actions

    def resolve(self, action_id: str) -> str | None:
        action = self._actions.get(action_id)
        with self._store.connection() as conn:
            return self._resolve_with_connection(conn, action)

    def set_binding(self, action_id: str, binding: str | None) -> None:
        action = self._actions.get(action_id)
        cleaned = _clean_binding(binding)
        if cleaned is None and not action.may_be_unbound:
            raise ValueError(f"action {action_id} may not be unbound")
        if cleaned is not None:
            cleaned = _canonical_binding(cleaned)

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._effective_bindings(conn)
            conflict = self._conflict_in_state(action_id, cleaned, state)
            if conflict is not None:
                raise ValueError(f"shortcut conflict with {conflict}")
            conn.execute(
                "INSERT INTO keymap_overrides(action_id, binding, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(action_id) DO UPDATE SET "
                "binding=excluded.binding, updated_at=excluded.updated_at",
                (action_id, cleaned, datetime.now(UTC).isoformat()),
            )

    def restore_default(self, action_id: str) -> None:
        action = self._actions.get(action_id)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._effective_bindings(conn)
            conflict = self._conflict_in_state(action_id, action.default_binding, state)
            if conflict is not None:
                raise ValueError(f"shortcut conflict with {conflict}")
            conn.execute("DELETE FROM keymap_overrides WHERE action_id = ?", (action_id,))

    def conflict(self, action_id: str, binding: str | None) -> str | None:
        self._actions.get(action_id)
        if binding is None:
            return None
        _binding_key(binding)
        with self._store.connection() as conn:
            state = self._effective_bindings(conn)
        return self._conflict_in_state(action_id, binding, state)

    def export_json(self) -> str:
        with self._store.connection() as conn:
            state = self._effective_bindings(conn)
        payload = {
            "format_version": self.FORMAT_VERSION,
            "bindings": {action.action_id: state[action.action_id] for action in self._actions.all()},
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def import_json(self, data: str) -> None:
        # Imported settings are untrusted. Admit them before opening a transaction.
        if type(data) is not str:
            raise TypeError("keymap import must be text")
        if len(data) > _MAX_KEYMAP_IMPORT_BYTES:
            raise ValueError("keymap import exceeds the byte limit")
        try:
            byte_count = len(data.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("keymap import must contain valid UTF-8") from None
        if byte_count > _MAX_KEYMAP_IMPORT_BYTES:
            raise ValueError("keymap import exceeds the byte limit")
        try:
            raw = json.loads(
                data,
                object_pairs_hook=_unique_json_members,
                parse_float=_reject_json_noninteger,
                parse_constant=_reject_json_noninteger,
            )
        except RecursionError:
            raise ValueError("keymap JSON is too deeply nested") from None
        # JSON escapes can produce lone surrogates after raw UTF-8 admission.
        # Validate decoded text, including otherwise ignored document members,
        # before any binding is normalized or a SQLite transaction is opened.
        try:
            json.dumps(raw, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("keymap import must contain valid UTF-8") from None
        except RecursionError:
            raise ValueError("keymap JSON is too deeply nested") from None
        if type(raw) is not dict:
            raise TypeError("keymap document must be an object")
        if (
            type(raw.get("format_version")) is not int
            or raw["format_version"] != self.FORMAT_VERSION
        ):
            raise ValueError("unsupported keymap format version")
        bindings = raw.get("bindings")
        if type(bindings) is not dict:
            raise TypeError("keymap bindings must be an object")
        proposed: dict[str, str | None] = {}
        for action_id, binding in bindings.items():
            action = self._actions.get(action_id)
            if binding is not None and type(binding) is not str:
                raise ValueError(f"invalid binding for {action_id}")
            cleaned = _clean_binding(binding)
            if cleaned is None and not action.may_be_unbound:
                raise ValueError(f"action {action_id} may not be unbound")
            if cleaned is not None:
                cleaned = _canonical_binding(cleaned)
            proposed[action_id] = cleaned

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._effective_bindings(conn)
            state.update(proposed)
            self._validate_state(state)
            now = datetime.now(UTC).isoformat()
            for action_id, binding in proposed.items():
                conn.execute(
                    "INSERT INTO keymap_overrides(action_id, binding, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(action_id) DO UPDATE SET "
                    "binding=excluded.binding, updated_at=excluded.updated_at",
                    (action_id, binding, now),
                )

    def _resolve_with_connection(
        self, conn: sqlite3.Connection, action: ActionDefinition
    ) -> str | None:
        row = conn.execute(
            "SELECT binding FROM keymap_overrides WHERE action_id = ?", (action.action_id,)
        ).fetchone()
        binding = action.default_binding if row is None else row["binding"]
        if binding is None:
            return None
        if type(binding) is not str:
            raise TypeError("stored keymap binding must be text")
        return _canonical_binding(binding)

    def _effective_bindings(self, conn: sqlite3.Connection) -> dict[str, str | None]:
        rows = conn.execute("SELECT action_id, binding FROM keymap_overrides").fetchall()
        overrides: dict[str, str | None] = {}
        for row in rows:
            action_id = row["action_id"]
            if type(action_id) is not str:
                raise TypeError("stored keymap action ID must be text")
            overrides[action_id] = row["binding"]
        state: dict[str, str | None] = {}
        for action in self._actions.all():
            binding = overrides.get(action.action_id, action.default_binding)
            if binding is None:
                state[action.action_id] = None
                continue
            if type(binding) is not str:
                raise TypeError("stored keymap binding must be text")
            state[action.action_id] = _canonical_binding(binding)
        return state

    def _conflict_in_state(
        self,
        action_id: str,
        binding: str | None,
        state: dict[str, str | None],
    ) -> str | None:
        if binding is None:
            return None
        action = self._actions.get(action_id)
        wanted = _binding_key(binding)
        for other in self._actions.all():
            if other.action_id == action_id or other.scope != action.scope:
                continue
            resolved = state[other.action_id]
            if resolved is not None and _binding_key(resolved) == wanted:
                return other.action_id
        return None

    def _validate_state(self, state: dict[str, str | None]) -> None:
        seen: dict[tuple[str, str], str] = {}
        for action in self._actions.all():
            binding = state[action.action_id]
            if binding is None:
                continue
            key = (action.scope, _binding_key(binding))
            other = seen.get(key)
            if other is not None:
                raise ValueError(f"shortcut conflict between {other} and {action.action_id}")
            seen[key] = action.action_id


def _clean_binding(binding: str | None) -> str | None:
    if binding is None:
        return None
    if type(binding) is not str:
        raise TypeError("shortcut binding must be text or null")
    if len(binding) > _MAX_KEYMAP_BINDING_BYTES:
        raise ValueError("shortcut binding exceeds the byte limit")
    try:
        encoded = binding.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("shortcut binding must contain valid UTF-8") from None
    if len(encoded) > _MAX_KEYMAP_BINDING_BYTES:
        raise ValueError("shortcut binding exceeds the byte limit")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in binding):
        raise ValueError("shortcut binding contains unsupported control characters")
    cleaned = "+".join(part.strip() for part in binding.split("+") if part.strip())
    return cleaned or None


def _binding_parts(binding: str) -> tuple[tuple[str, ...], str]:
    cleaned = _clean_binding(binding)
    if cleaned is None:
        raise ValueError("binding must not be empty")

    modifiers: set[str] = set()
    primary_keys: list[str] = []
    for raw_part in cleaned.split("+"):
        part = raw_part.casefold()
        modifier = _MODIFIER_ALIASES.get(part)
        if modifier is None:
            primary_keys.append(raw_part)
            continue
        if modifier in modifiers:
            raise ValueError(f"duplicate shortcut modifier: {raw_part}")
        modifiers.add(modifier)

    if len(primary_keys) != 1:
        raise ValueError("shortcut must contain exactly one primary key")

    ordered_modifiers = tuple(sorted(modifiers, key=_MODIFIER_ORDER.__getitem__))
    return ordered_modifiers, primary_keys[0]


def _canonical_binding(binding: str) -> str:
    modifiers, primary_key = _binding_parts(binding)
    display_modifiers = [_MODIFIER_DISPLAY[modifier] for modifier in modifiers]
    return "+".join((*display_modifiers, primary_key))


def _binding_key(binding: str) -> str:
    modifiers, primary_key = _binding_parts(binding)
    return "+".join((*modifiers, primary_key.casefold()))
