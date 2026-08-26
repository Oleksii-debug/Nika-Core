from __future__ import annotations

import json
import sqlite3
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


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    action_id: str
    label: str
    category: str
    default_binding: str | None = None
    scope: str = "app"
    may_be_unbound: bool = True

    def __post_init__(self) -> None:
        if not self.action_id.strip() or "." not in self.action_id:
            raise ValueError("action_id must be a stable dotted identifier")
        if not self.label.strip() or not self.category.strip() or not self.scope.strip():
            raise ValueError("action metadata must not be empty")
        if self.default_binding is None and not self.may_be_unbound:
            raise ValueError("required action must have a default binding")
        if self.default_binding is not None:
            _binding_key(self.default_binding)


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {}

    def register(self, definition: ActionDefinition) -> None:
        if definition.action_id in self._actions:
            raise ValueError(f"duplicate action_id: {definition.action_id}")
        if definition.default_binding is not None:
            conflict = self.find_by_binding(definition.default_binding, definition.scope)
            if conflict is not None:
                raise ValueError(
                    f"default binding conflict: {definition.default_binding} already belongs to "
                    f"{conflict.action_id}"
                )
        self._actions[definition.action_id] = definition

    def get(self, action_id: str) -> ActionDefinition:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise KeyError(f"Unknown action: {action_id}") from exc

    def all(self) -> tuple[ActionDefinition, ...]:
        return tuple(self._actions[key] for key in sorted(self._actions))

    def find_by_binding(self, binding: str, scope: str) -> ActionDefinition | None:
        wanted = _binding_key(binding)
        for action in self._actions.values():
            if (
                action.scope == scope
                and action.default_binding is not None
                and _binding_key(action.default_binding) == wanted
            ):
                return action
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
            _binding_key(cleaned)

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._effective_bindings(conn)
            conflict = self._conflict_in_state(action_id, cleaned, state)
            if conflict is not None:
                raise ValueError(f"shortcut conflict with {conflict}")
            conn.execute(
                "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(action_id) DO UPDATE SET binding=excluded.binding, updated_at=excluded.updated_at",
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
        raw = json.loads(data)
        if raw.get("format_version") != self.FORMAT_VERSION:
            raise ValueError("unsupported keymap format version")
        bindings = raw.get("bindings")
        if not isinstance(bindings, dict):
            raise TypeError("keymap bindings must be an object")
        proposed: dict[str, str | None] = {}
        for action_id, binding in bindings.items():
            action = self._actions.get(action_id)
            if binding is not None and not isinstance(binding, str):
                raise ValueError(f"invalid binding for {action_id}")
            cleaned = _clean_binding(binding)
            if cleaned is None and not action.may_be_unbound:
                raise ValueError(f"action {action_id} may not be unbound")
            if cleaned is not None:
                _binding_key(cleaned)
            proposed[action_id] = cleaned

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            state = self._effective_bindings(conn)
            state.update(proposed)
            self._validate_state(state)
            now = datetime.now(UTC).isoformat()
            for action_id, binding in proposed.items():
                conn.execute(
                    "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(action_id) DO UPDATE SET binding=excluded.binding, updated_at=excluded.updated_at",
                    (action_id, binding, now),
                )

    def _resolve_with_connection(
        self, conn: sqlite3.Connection, action: ActionDefinition
    ) -> str | None:
        row = conn.execute(
            "SELECT binding FROM keymap_overrides WHERE action_id = ?", (action.action_id,)
        ).fetchone()
        return action.default_binding if row is None else row["binding"]

    def _effective_bindings(self, conn: sqlite3.Connection) -> dict[str, str | None]:
        rows = conn.execute("SELECT action_id, binding FROM keymap_overrides").fetchall()
        overrides = {str(row["action_id"]): row["binding"] for row in rows}
        return {
            action.action_id: (
                overrides[action.action_id]
                if action.action_id in overrides
                else action.default_binding
            )
            for action in self._actions.all()
        }

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
    cleaned = "+".join(part.strip() for part in binding.split("+") if part.strip())
    return cleaned or None


def _binding_key(binding: str) -> str:
    cleaned = _clean_binding(binding)
    if cleaned is None:
        raise ValueError("binding must not be empty")

    modifiers: set[str] = set()
    primary_keys: list[str] = []
    for raw_part in cleaned.split("+"):
        part = raw_part.casefold()
        modifier = _MODIFIER_ALIASES.get(part)
        if modifier is None:
            primary_keys.append(part)
            continue
        if modifier in modifiers:
            raise ValueError(f"duplicate shortcut modifier: {raw_part}")
        modifiers.add(modifier)

    if len(primary_keys) != 1:
        raise ValueError("shortcut must contain exactly one primary key")

    ordered_modifiers = sorted(modifiers, key=_MODIFIER_ORDER.__getitem__)
    return "+".join((*ordered_modifiers, primary_keys[0]))
