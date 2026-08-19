from __future__ import annotations

from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.media.transcription import ChunkState, TranscriptionChunk


class TranscriptionChunkRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def put(self, chunk: TranscriptionChunk) -> None:
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT chunk_json FROM media_transcription_chunks WHERE chunk_id = ?",
                (chunk.chunk_id,),
            ).fetchone()
            if existing is not None:
                current = TranscriptionChunk.model_validate_json(existing["chunk_json"])
                if current.state == ChunkState.COMPLETED and current != chunk:
                    raise ValueError("completed transcription chunks are immutable")
            conn.execute(
                """INSERT INTO media_transcription_chunks(
                    chunk_id, job_id, ordinal, state, chunk_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    state = excluded.state,
                    chunk_json = excluded.chunk_json,
                    updated_at = excluded.updated_at
                """,
                (
                    chunk.chunk_id,
                    chunk.job_id,
                    chunk.ordinal,
                    chunk.state.value,
                    chunk.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, chunk_id: str) -> TranscriptionChunk:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT chunk_json FROM media_transcription_chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown transcription chunk: {chunk_id}")
        return TranscriptionChunk.model_validate_json(row["chunk_json"])

    def list_for_job(self, job_id: str) -> tuple[TranscriptionChunk, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT chunk_json FROM media_transcription_chunks
                WHERE job_id = ? ORDER BY ordinal""",
                (job_id,),
            ).fetchall()
        return tuple(TranscriptionChunk.model_validate_json(row["chunk_json"]) for row in rows)

    def pending_for_resume(self, job_id: str) -> tuple[TranscriptionChunk, ...]:
        chunks = self.list_for_job(job_id)
        resumable: list[TranscriptionChunk] = []
        for chunk in chunks:
            if chunk.state == ChunkState.COMPLETED:
                continue
            if chunk.state == ChunkState.RUNNING:
                chunk = chunk.model_copy(
                    update={
                        "state": ChunkState.PENDING,
                        "error_code": "restart_reconciliation_required",
                        "error_message": "Chunk was running during restart and must be replayed from its durable boundary.",
                    }
                )
                self.put(chunk)
            resumable.append(chunk)
        return tuple(resumable)
