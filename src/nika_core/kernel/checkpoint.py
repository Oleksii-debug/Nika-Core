from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    stage: str
    payload: dict[str, object]
    checksum_sha256: str


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CheckpointService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def save(self, *, task_id: str, stage: str, payload: dict[str, object]) -> Checkpoint:
        body = _canonical_json(payload)
        checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
        checkpoint_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self.store.connection() as conn:
            exists = conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if exists is None:
                raise KeyError(f"Unknown task: {task_id}")
            conn.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (checkpoint_id, task_id, stage, body, checksum, now),
            )
        return Checkpoint(checkpoint_id, task_id, stage, dict(payload), checksum)

    def latest(self, task_id: str) -> Checkpoint | None:
        with self.store.connection() as conn:
            row = conn.execute(
                """
                SELECT checkpoint_id, task_id, stage, payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload_json = row[3]
        expected = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        if expected != row[4]:
            raise ValueError("Checkpoint checksum mismatch")
        return Checkpoint(row[0], row[1], row[2], json.loads(payload_json), row[4])
