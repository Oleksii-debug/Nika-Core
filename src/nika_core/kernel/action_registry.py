from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore


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
            if action.scope == scope and action.default_binding is not None:
                if _binding_key(action.default_binding) == wanted:
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
            row = conn.execute(
                "SELECT binding FROM keymap_overrides WHERE action_id = ?", (action_id,)
            ).fetchone()
        return action.default_binding if row is None else row["binding"]

    def set_binding(self, action_id: str, binding: str | None) -> None:
        action = self._actions.get(action_id)
        cleaned = _clean_binding(binding)
        if cleaned is None and not action.may_be_unbound:
            raise ValueError(f"action {action_id} may not be unbound")
        conflict = self.conflict(action_id, cleaned)
        if conflict is not None:
            raise ValueError(f"shortcut conflict with {conflict}")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(action_id) DO UPDATE SET binding=excluded.binding, updated_at=excluded.updated_at",
                (action_id, cleaned, datetime.now(UTC).isoformat()),
            )

    def restore_default(self, action_id: str) -> None:
        self._actions.get(action_id)
        with self._store.connection() as conn:
            conn.execute("DELETE FROM keymap_overrides WHERE action_id = ?", (action_id,))

    def conflict(self, action_id: str, binding: str | None) -> str | None:
        if binding is None:
            return None
        action = self._actions.get(action_id)
        wanted = _binding_key(binding)
        for other in self._actions.all():
            if other.action_id == action_id or other.scope != action.scope:
                continue
            resolved = self.resolve(other.action_id)
            if resolved is not None and _binding_key(resolved) == wanted:
                return other.action_id
        return None

    def export_json(self) -> str:
        payload = {
            "format_version": self.FORMAT_VERSION,
            "bindings": {action.action_id: self.resolve(action.action_id) for action in self._actions.all()},
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def import_json(self, data: str) -> None:
        raw = json.loads(data)
        if raw.get("format_version") != self.FORMAT_VERSION:
            raise ValueError("unsupported keymap format version")
        bindings = raw.get("bindings")
        if not isinstance(bindings, dict):
            raise ValueError("keymap bindings must be an object")
        proposed: dict[str, str | None] = {}
        for action_id, binding in bindings.items():
            action = self._actions.get(action_id)
            if binding is not None and not isinstance(binding, str):
                raise ValueError(f"invalid binding for {action_id}")
            cleaned = _clean_binding(binding)
            if cleaned is None and not action.may_be_unbound:
                raise ValueError(f"action {action_id} may not be unbound")
            proposed[action_id] = cleaned
        seen: dict[tuple[str, str], str] = {}
        for action in self._actions.all():
            binding = proposed.get(action.action_id, self.resolve(action.action_id))
            if binding is None:
                continue
            key = (action.scope, _binding_key(binding))
            other = seen.get(key)
            if other is not None:
                raise ValueError(f"shortcut conflict between {other} and {action.action_id}")
            seen[key] = action.action_id
        with self._store.connection() as conn:
            now = datetime.now(UTC).isoformat()
            for action_id, binding in proposed.items():
                conn.execute(
                    "INSERT INTO keymap_overrides(action_id, binding, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(action_id) DO UPDATE SET binding=excluded.binding, updated_at=excluded.updated_at",
                    (action_id, binding, now),
                )


def _clean_binding(binding: str | None) -> str | None:
    if binding is None:
        return None
    cleaned = "+".join(part.strip() for part in binding.split("+") if part.strip())
    return cleaned or None


def _binding_key(binding: str) -> str:
    cleaned = _clean_binding(binding)
    if cleaned is None:
        raise ValueError("binding must not be empty")
    return cleaned.casefold()
