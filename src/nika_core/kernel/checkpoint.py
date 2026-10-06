from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

from nika_core.data.sqlite import SQLiteStore


_JSON_MAX_BYTES = 1024 * 1024
_JSON_MAX_DEPTH = 64
_JSON_MAX_NODES = 10_000
_JSON_MAX_INTEGER_BITS = 4096
_TEXT_MAX_BYTES = 4096
_CHECKSUM_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    stage: str
    payload: dict[str, object]
    checksum_sha256: str


def _json_string_utf8_size(value: str) -> int:
    size = 2
    for char in value:
        codepoint = ord(char)
        if char in {'"', "\\"} or char in {"\b", "\f", "\n", "\r", "\t"}:
            size += 2
        elif codepoint < 0x20:
            size += 6
        else:
            try:
                size += len(char.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("Checkpoint payload text must be valid UTF-8") from exc
        if size > _JSON_MAX_BYTES:
            raise ValueError("Checkpoint payload exceeds the UTF-8 byte limit")
    return size


def _checked_json_bytes(total: int, amount: int) -> int:
    total += amount
    if total > _JSON_MAX_BYTES:
        raise ValueError("Checkpoint payload exceeds the UTF-8 byte limit")
    return total


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"Checkpoint {field_name} must be str")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Checkpoint {field_name} must be valid UTF-8") from exc
    if len(encoded) > _TEXT_MAX_BYTES:
        raise ValueError(f"Checkpoint {field_name} exceeds the UTF-8 byte limit")
    return value


def _json_key_utf8_size(key: object) -> int:
    if type(key) is str:
        return _json_string_utf8_size(key)
    if type(key) is bool:
        return 6 if key else 7
    if key is None:
        return 6
    if type(key) is int:
        if key.bit_length() > _JSON_MAX_INTEGER_BITS:
            raise ValueError("Checkpoint payload integer key exceeds the bit limit")
        return len(str(key)) + 2
    if type(key) is float:
        if not math.isfinite(key):
            raise ValueError("Checkpoint payload must contain finite key values")
        return len(repr(key)) + 2
    if isinstance(key, (str, int, float)):
        raise ValueError("Checkpoint payload key scalars must be built-in types")
    return 0


def _validate_payload_resources(payload: object) -> None:
    nodes = 0
    json_bytes = 0
    stack: list[tuple[object, int]] = [(payload, 1)]

    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _JSON_MAX_NODES:
            raise ValueError("Checkpoint payload exceeds the JSON node limit")
        if depth > _JSON_MAX_DEPTH:
            raise ValueError("Checkpoint payload exceeds the JSON depth limit")

        if value is None:
            json_bytes = _checked_json_bytes(json_bytes, 4)
            continue
        if type(value) is bool:
            json_bytes = _checked_json_bytes(json_bytes, 4 if value else 5)
            continue
        if type(value) is int:
            if value.bit_length() > _JSON_MAX_INTEGER_BITS:
                raise ValueError("Checkpoint payload integer exceeds the bit limit")
            json_bytes = _checked_json_bytes(json_bytes, len(str(value)))
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("Checkpoint payload must contain finite numbers")
            json_bytes = _checked_json_bytes(json_bytes, len(repr(value)))
            continue
        if type(value) is str:
            json_bytes = _checked_json_bytes(json_bytes, _json_string_utf8_size(value))
            continue
        if isinstance(value, (str, int, float)):
            raise ValueError("Checkpoint payload scalar values must be built-in types")
        if isinstance(value, dict):
            if type(value) is not dict:
                raise ValueError("Checkpoint payload containers must be built-in types")
            if len(value) > _JSON_MAX_NODES - nodes:
                raise ValueError("Checkpoint payload exceeds the JSON node limit")
            punctuation = 2 + len(value) + max(0, len(value) - 1)
            json_bytes = _checked_json_bytes(json_bytes, punctuation)
            for key, item in value.items():
                json_bytes = _checked_json_bytes(json_bytes, _json_key_utf8_size(key))
                stack.append((item, depth + 1))
            continue
        if isinstance(value, (list, tuple)):
            if type(value) not in (list, tuple):
                raise ValueError("Checkpoint payload containers must be built-in types")
            if len(value) > _JSON_MAX_NODES - nodes:
                raise ValueError("Checkpoint payload exceeds the JSON node limit")
            punctuation = 2 + max(0, len(value) - 1)
            json_bytes = _checked_json_bytes(json_bytes, punctuation)
            for item in value:
                stack.append((item, depth + 1))


def _canonical_json(payload: dict[str, object]) -> str:
    _validate_payload_resources(payload)
    try:
        body = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        body_bytes = body.encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("Checkpoint payload must be a JSON object with finite values") from exc
    if len(body_bytes) > _JSON_MAX_BYTES:
        raise ValueError("Checkpoint payload exceeds the UTF-8 byte limit")
    return body


def _reject_non_finite(_value: str) -> NoReturn:
    raise ValueError("Checkpoint payload contains a non-finite number")


def _require_sqlite_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"Checkpoint {field_name} storage must be SQLite TEXT")
    return value


def _decode_persisted_text(
    *,
    storage_type: object,
    byte_length: object,
    blob: object,
    field_name: str,
) -> str:
    if storage_type != "text":
        raise TypeError(f"Checkpoint {field_name} storage must be SQLite TEXT")
    if type(byte_length) is not int:
        raise TypeError(f"Checkpoint {field_name} byte length must be an integer")
    if byte_length > _TEXT_MAX_BYTES:
        raise ValueError(f"Checkpoint {field_name} exceeds the UTF-8 byte limit")
    if type(blob) is not bytes or len(blob) != byte_length:
        raise ValueError(f"Checkpoint {field_name} bytes are incomplete")
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Checkpoint {field_name} is invalid UTF-8") from exc


def _require_checksum(checksum_sha256: object) -> str:
    checksum_text = _require_sqlite_text(checksum_sha256, "checksum")
    if (
        len(checksum_text) != _CHECKSUM_HEX_LENGTH
        or any(char not in _HEX_DIGITS for char in checksum_text)
    ):
        raise ValueError("Checkpoint checksum is invalid")
    return checksum_text


def _decode_payload(payload_json: object, checksum_sha256: object) -> dict[str, object]:
    payload_text = _require_sqlite_text(payload_json, "payload")
    try:
        payload_bytes = payload_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Checkpoint payload is invalid UTF-8") from exc
    if len(payload_bytes) > _JSON_MAX_BYTES:
        raise ValueError("Checkpoint payload exceeds the UTF-8 byte limit")
    checksum_text = _require_checksum(checksum_sha256)
    expected = hashlib.sha256(payload_bytes).hexdigest()
    if expected != checksum_text:
        raise ValueError("Checkpoint checksum mismatch")
    try:
        payload = json.loads(payload_text, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("Checkpoint payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("Checkpoint payload must be a JSON object")
    if _canonical_json(payload) != payload_text:
        raise ValueError("Checkpoint payload is not canonical JSON")
    return payload


def _decode_persisted_payload(
    *,
    payload_storage_type: object,
    payload_byte_length: object,
    payload_blob: object,
    checksum_storage_type: object,
    checksum_byte_length: object,
    checksum_blob: object,
) -> dict[str, object]:
    if payload_storage_type != "text":
        raise TypeError("Checkpoint payload storage must be SQLite TEXT")
    if type(payload_byte_length) is not int:
        raise TypeError("Checkpoint payload byte length must be an integer")
    if payload_byte_length > _JSON_MAX_BYTES:
        raise ValueError("Checkpoint payload exceeds the UTF-8 byte limit")
    if type(payload_blob) is not bytes or len(payload_blob) != payload_byte_length:
        raise ValueError("Checkpoint payload bytes are incomplete")
    try:
        payload_text = payload_blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Checkpoint payload is invalid UTF-8") from exc

    if checksum_storage_type != "text":
        raise TypeError("Checkpoint checksum storage must be SQLite TEXT")
    if checksum_byte_length != _CHECKSUM_HEX_LENGTH:
        raise ValueError("Checkpoint checksum is invalid")
    if type(checksum_blob) is not bytes or len(checksum_blob) != _CHECKSUM_HEX_LENGTH:
        raise ValueError("Checkpoint checksum bytes are incomplete")
    try:
        checksum_text = checksum_blob.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Checkpoint checksum is invalid") from exc

    return _decode_payload(payload_text, checksum_text)


class CheckpointService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def save(self, *, task_id: str, stage: str, payload: dict[str, object]) -> Checkpoint:
        prepared = self._prepare_checkpoint(
            task_id=task_id,
            stage=stage,
            payload=payload,
        )
        with self.store.connection() as conn:
            return self._insert_checkpoint(conn, prepared)

    def save_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        stage: str,
        payload: dict[str, object],
    ) -> Checkpoint:
        """Save inside a caller-owned transaction without committing it."""

        prepared = self._prepare_checkpoint(
            task_id=task_id,
            stage=stage,
            payload=payload,
        )
        return self._insert_checkpoint(conn, prepared)

    @staticmethod
    def _prepare_checkpoint(
        *,
        task_id: str,
        stage: str,
        payload: dict[str, object],
    ) -> tuple[Checkpoint, str, str]:
        task_id = _require_text(task_id, "task_id")
        stage = _require_text(stage, "stage")
        body = _canonical_json(payload)
        checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
        public_payload = _decode_payload(body, checksum)
        checkpoint = Checkpoint(
            str(uuid.uuid4()),
            task_id,
            stage,
            public_payload,
            checksum,
        )
        return checkpoint, body, datetime.now(UTC).isoformat()

    @staticmethod
    def _insert_checkpoint(
        conn: sqlite3.Connection,
        prepared: tuple[Checkpoint, str, str],
    ) -> Checkpoint:
        checkpoint, body, created_at = prepared
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?",
            (checkpoint.task_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(f"Unknown task: {checkpoint.task_id}")
        conn.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.task_id,
                checkpoint.stage,
                body,
                checkpoint.checksum_sha256,
                created_at,
            ),
        )
        return checkpoint

    def latest(self, task_id: str) -> Checkpoint | None:
        task_id = _require_text(task_id, "task_id")
        with self.store.connection() as conn:
            row = self._select_latest_row(
                conn,
                task_id=task_id,
                stage=None,
            )
        if row is None:
            return None
        return self._checkpoint_from_row(
            row,
            expected_task_id=task_id,
        )

    def latest_for_stage_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        stage: str,
    ) -> Checkpoint | None:
        """Read one stage from the caller's current SQLite snapshot."""

        task_id = _require_text(task_id, "task_id")
        stage = _require_text(stage, "stage")
        row = self._select_latest_row(
            conn,
            task_id=task_id,
            stage=stage,
        )
        if row is None:
            return None
        return self._checkpoint_from_row(
            row,
            expected_task_id=task_id,
            expected_stage=stage,
        )

    @staticmethod
    def _select_latest_row(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        stage: str | None,
    ) -> sqlite3.Row | None:
        task_id_bytes = task_id.encode("utf-8")
        stage_bytes = b"" if stage is None else stage.encode("utf-8")
        return conn.execute(
            """
            SELECT typeof(checkpoint_id) AS checkpoint_id_storage_type,
                   length(CAST(checkpoint_id AS BLOB)) AS checkpoint_id_byte_length,
                   substr(CAST(checkpoint_id AS BLOB), 1, ?) AS checkpoint_id_blob,
                   typeof(task_id) AS task_id_storage_type,
                   length(CAST(task_id AS BLOB)) AS task_id_byte_length,
                   substr(CAST(task_id AS BLOB), 1, ?) AS task_id_blob,
                   typeof(stage) AS stage_storage_type,
                   length(CAST(stage AS BLOB)) AS stage_byte_length,
                   substr(CAST(stage AS BLOB), 1, ?) AS stage_blob,
                   typeof(payload_json) AS payload_storage_type,
                   length(CAST(payload_json AS BLOB)) AS payload_byte_length,
                   substr(CAST(payload_json AS BLOB), 1, ?) AS payload_blob,
                   typeof(checksum_sha256) AS checksum_storage_type,
                   length(CAST(checksum_sha256 AS BLOB)) AS checksum_byte_length,
                   substr(CAST(checksum_sha256 AS BLOB), 1, ?) AS checksum_blob
            FROM checkpoints
            WHERE (
                (typeof(task_id) = 'text' AND task_id = ?)
                OR (
                    typeof(task_id) <> 'text'
                    AND length(CAST(task_id AS BLOB)) = ?
                    AND substr(CAST(task_id AS BLOB), 1, ?) = ?
                )
            )
            AND (
                ? IS NULL
                OR (typeof(stage) = 'text' AND stage = ?)
                OR (
                    typeof(stage) <> 'text'
                    AND length(CAST(stage AS BLOB)) = ?
                    AND substr(CAST(stage AS BLOB), 1, ?) = ?
                )
            )
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (
                _TEXT_MAX_BYTES + 1,
                _TEXT_MAX_BYTES + 1,
                _TEXT_MAX_BYTES + 1,
                _JSON_MAX_BYTES + 1,
                _CHECKSUM_HEX_LENGTH + 1,
                task_id,
                len(task_id_bytes),
                _TEXT_MAX_BYTES + 1,
                task_id_bytes,
                stage,
                stage,
                len(stage_bytes),
                _TEXT_MAX_BYTES + 1,
                stage_bytes,
            ),
        ).fetchone()

    @staticmethod
    def _checkpoint_from_row(
        row: sqlite3.Row,
        *,
        expected_task_id: str,
        expected_stage: str | None = None,
    ) -> Checkpoint:
        checkpoint_id = _decode_persisted_text(
            storage_type=row["checkpoint_id_storage_type"],
            byte_length=row["checkpoint_id_byte_length"],
            blob=row["checkpoint_id_blob"],
            field_name="checkpoint_id",
        )
        persisted_task_id = _decode_persisted_text(
            storage_type=row["task_id_storage_type"],
            byte_length=row["task_id_byte_length"],
            blob=row["task_id_blob"],
            field_name="task_id",
        )
        if persisted_task_id != expected_task_id:
            raise ValueError("Checkpoint task identity mismatch")
        stage = _decode_persisted_text(
            storage_type=row["stage_storage_type"],
            byte_length=row["stage_byte_length"],
            blob=row["stage_blob"],
            field_name="stage",
        )
        if expected_stage is not None and stage != expected_stage:
            raise ValueError("Checkpoint stage identity mismatch")
        payload = _decode_persisted_payload(
            payload_storage_type=row["payload_storage_type"],
            payload_byte_length=row["payload_byte_length"],
            payload_blob=row["payload_blob"],
            checksum_storage_type=row["checksum_storage_type"],
            checksum_byte_length=row["checksum_byte_length"],
            checksum_blob=row["checksum_blob"],
        )
        return Checkpoint(
            checkpoint_id,
            persisted_task_id,
            stage,
            payload,
            row["checksum_blob"].decode("ascii"),
        )
