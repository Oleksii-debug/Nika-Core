from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research.local import local_media_type, resolve_local_file
from nika_core.ui.bridge_models import UIResult

MAX_SOURCE_BYTES = 16 * 1024 * 1024
_SCHEMA_VERSION = 1
_MIGRATIONS = {
    1: (
        (
            "CREATE TABLE v01_source_settings ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
            "revision INTEGER NOT NULL CHECK(revision > 0), selection_json TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE v01_task_source_bindings ("
            "task_id TEXT PRIMARY KEY, selection_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE v01_source_selections ("
            "selection_id TEXT PRIMARY KEY, selection_json TEXT NOT NULL)"
        ),
    ),
}


class SourceSetupError(ValueError):
    """A fixed, user-safe source-configuration error, without filesystem diagnostics."""


class SourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    root: str = Field(min_length=1, max_length=32767)
    source_a: str = Field(min_length=1, max_length=32767)
    source_b: str = Field(min_length=1, max_length=32767)

    @field_validator("root", "source_a", "source_b")
    @classmethod
    def path_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid source path")
        return value

    def resolve(self) -> SourceSelection:
        try:
            root = Path(self.root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("invalid root")
            root = root.resolve(strict=True)
            paths = []
            for raw in (self.source_a, self.source_b):
                candidate = Path(raw)
                if not candidate.is_absolute():
                    candidate = root / candidate
                path = resolve_local_file(candidate, allowed_root=root, max_bytes=MAX_SOURCE_BYTES)
                local_media_type(path)
                paths.append(path)
            if paths[0].samefile(paths[1]):
                raise ValueError("sources are the same file")
            return SourceSelection(root=str(root), source_a=str(paths[0]), source_b=str(paths[1]))
        except (OSError, RuntimeError, ValueError) as exc:
            raise SourceSetupError(
                "Вкажіть наявну папку повним шляхом і два різні підтримувані файли "
                "всередині неї, кожен до 16 МіБ."
            ) from exc

    @classmethod
    def from_stored(cls, value: str) -> SourceSelection:
        try:
            selection = cls.model_validate_json(value)
            root = Path(selection.root)
            paths = (Path(selection.source_a), Path(selection.source_b))
            if not root.is_absolute() or ".." in root.parts or paths[0] == paths[1]:
                raise ValueError("invalid stored root")
            for path in paths:
                if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
                    raise ValueError("invalid stored source")
                local_media_type(path)
            return selection
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SourceSetupError(
                "Збережені налаштування джерел пошкоджені або несумісні."
            ) from exc


class _SetupRequest(SourceSelection):
    revision: int = Field(ge=0)


class V01SourceSettings:
    """Versioned local setup; task bindings preserve the original read authority.

    The private schema extension is migrated transactionally without taking ownership
    of shared research/kernel migrations. Paths are local application data, never audit payloads.
    """

    def __init__(self, store: SQLiteStore, defaults: AppConfig) -> None:
        self._store = store
        self._defaults = defaults.model_copy(deep=True)
        self._audit = AuditLog(store)
        with store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS v01_source_settings_schema ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = (
                conn.execute("SELECT MAX(version) FROM v01_source_settings_schema").fetchone()[0]
                or 0
            )
            if current > _SCHEMA_VERSION:
                raise SourceSetupError("Версія налаштувань джерел новіша за цю програму.")
            for version in range(current + 1, _SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO v01_source_settings_schema VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def _selected(self, conn: sqlite3.Connection) -> SourceSelection:
        row = conn.execute("SELECT * FROM v01_source_settings WHERE singleton = 1").fetchone()
        selection = (
            SourceSelection.from_stored(row["selection_json"]) if row else self._default_selection()
        )
        if selection is None:
            raise SourceSetupError("Спочатку налаштуйте джерела команди.")
        return selection.resolve()

    def prepare_task_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Capture setup before the canonical task queue accepts/schedules the task."""
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                selection = self._selected(conn)
                body = selection.model_dump_json()
                selection_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO v01_source_selections VALUES (?, ?)",
                    (selection_id, body),
                )
                self._selection_by_id(conn, selection_id)
            return {"command": payload["command"], "v01_source_selection": selection_id}
        except (sqlite3.Error, OSError) as exc:
            raise SourceSetupError("Не вдалося підготувати джерела. Завдання не створено.") from exc

    @staticmethod
    def _selection_by_id(conn: sqlite3.Connection, selection_id: Any) -> SourceSelection:
        if not isinstance(selection_id, str) or re.fullmatch(r"[0-9a-f]{64}", selection_id) is None:
            raise SourceSetupError("Збережене посилання на джерела завдання некоректне.")
        row = conn.execute(
            "SELECT selection_json FROM v01_source_selections WHERE selection_id = ?",
            (selection_id,),
        ).fetchone()
        if (
            row is None
            or hashlib.sha256(row["selection_json"].encode("utf-8")).hexdigest() != selection_id
        ):
            raise SourceSetupError("Збережену конфігурацію завдання не вдалося перевірити.")
        return SourceSelection.from_stored(row["selection_json"])

    def _default_selection(self) -> SourceSelection | None:
        values = (
            self._defaults.v01_source_root,
            self._defaults.v01_source_a,
            self._defaults.v01_source_b,
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise SourceSetupError(
                "Спочатку налаштуйте обидва джерела у розділі «Джерела команди»."
            )
        return SourceSelection(
            root=str(values[0]), source_a=str(values[1]), source_b=str(values[2])
        ).resolve()

    def snapshot(self) -> dict[str, Any]:
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM v01_source_settings WHERE singleton = 1"
                ).fetchone()
            selection = (
                SourceSelection.from_stored(row["selection_json"])
                if row
                else self._default_selection()
            )
            return {
                "status": "ready" if selection else "missing",
                "revision": int(row["revision"]) if row else 0,
                "root": selection.root if selection else "",
                "source_a": selection.source_a if selection else "",
                "source_b": selection.source_b if selection else "",
            }
        except (sqlite3.Error, SourceSetupError, ValidationError):
            return {"status": "invalid"}

    def configure(self, payload: Mapping[str, Any]) -> UIResult:
        try:
            request = _SetupRequest.model_validate(dict(payload))
            selection = SourceSelection.model_validate(
                request.model_dump(exclude={"revision"})
            ).resolve()
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM v01_source_settings WHERE singleton = 1"
                ).fetchone()
                if row:
                    SourceSelection.from_stored(row["selection_json"])
                revision = int(row["revision"]) if row else 0
                if revision != request.revision:
                    raise SourceSetupError(
                        "Налаштування вже змінено в іншому вікні. "
                        "Натисніть «Перечитати збережені», перевірте поля й повторіть збереження."
                    )
                conn.execute(
                    "INSERT INTO v01_source_settings VALUES (1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision, "
                    "selection_json=excluded.selection_json",
                    (revision + 1, selection.model_dump_json()),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="v01.sources.configured",
                    entity_type="source_settings",
                    entity_id="default",
                    payload={"revision": revision + 1},
                )
            return UIResult(
                request_id="source-settings",
                status="completed",
                message="Джерела збережено для нових завдань. Можна створити командне завдання.",
                focus_id="command-input",
            )
        except SourceSetupError as exc:
            message = str(exc)
        except (ValidationError, TypeError, ValueError):
            message = "Перевірте папку, обидва файли та версію налаштувань."
        except (sqlite3.Error, OSError):
            message = "Не вдалося зберегти налаштування. Перевірте доступ до даних програми."
        return UIResult(
            request_id="source-settings", status="rejected", message=message, focus_id="source-root"
        )

    def for_task(
        self,
        task_id: str,
        *,
        legacy_sources: tuple[str, str] | None = None,
    ) -> SourceSelection:
        if not isinstance(task_id, str) or not task_id.strip():
            raise SourceSetupError("Немає коректного ідентифікатора завдання.")
        try:
            payload = TaskQueue(self._store).get(task_id).payload
        except KeyError:
            payload = {}
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            accepted = (
                self._selection_by_id(conn, payload["v01_source_selection"])
                if "v01_source_selection" in payload
                else None
            )
            binding = conn.execute(
                "SELECT selection_json FROM v01_task_source_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            if binding:
                selected = SourceSelection.from_stored(binding["selection_json"])
                if accepted is not None and accepted != selected:
                    raise SourceSetupError(
                        "Джерела не збігаються з початковою конфігурацією завдання."
                    )
                return selected
            selection = accepted if accepted is not None else self._selected(conn)
            if legacy_sources is not None and legacy_sources != (
                selection.source_a,
                selection.source_b,
            ):
                raise SourceSetupError(
                    "Для цього попереднього завдання потрібні його початкові джерела."
                )
            conn.execute(
                "INSERT INTO v01_task_source_bindings VALUES (?, ?, ?)",
                (task_id, selection.model_dump_json(), datetime.now(UTC).isoformat()),
            )
            self._audit.append_with_connection(
                conn,
                event_type="v01.sources.bound",
                entity_type="task",
                entity_id=task_id,
                payload={"schema_version": 1},
            )
            return selection
