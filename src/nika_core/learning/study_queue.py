from __future__ import annotations

import dataclasses
import enum
import hashlib
import hmac
import json
import string
import urllib.parse

from nika_core.kernel import task_queue
from nika_core.kernel import task_state


_STUDY_PAYLOAD_KIND = "study_material_v1"
_EVIDENCE_POLICY = "source_bound_v1"
_SECRET_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "secret",
        "authorization",
        "cookie",
    }
)
_SECRET_TEXT_MARKERS = (
    "authorization:",
    "bearer ",
    "cookie:",
    "api_key=",
    "apikey=",
    "access_token=",
    "refresh_token=",
    "password=",
    "passwd=",
    "secret=",
    "token=",
)


class StudyMaterialKind(enum.StrEnum):
    BOOK = "book"
    DOCUMENT = "document"
    WEB = "web"
    AUDIO = "audio"
    VIDEO = "video"


@dataclasses.dataclass(frozen=True, slots=True)
class StudyMaterial:
    material_id: str
    title: str
    kind: StudyMaterialKind
    source_ref: str
    source_version: str | None = None
    content_sha256: str | None = None
    learning_goal: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.material_id, "material_id", maximum=256)
        _require_text(self.title, "title", maximum=512)
        _require_text(self.source_ref, "source_ref", maximum=4096)
        if self.source_version is not None:
            _require_text(self.source_version, "source_version", maximum=256)
        if self.learning_goal is not None:
            _require_text(self.learning_goal, "learning_goal", maximum=2000)
        if self.content_sha256 is not None:
            _validate_sha256(self.content_sha256, "content_sha256")
        _reject_secret_bearing_reference(self.source_ref)


@dataclasses.dataclass(frozen=True, slots=True)
class StudyTask:
    task_id: str
    workspace_id: str
    agent_id: str
    state: task_state.TaskState
    material: StudyMaterial


class StudyQueue:
    """Durable Loop-B study work built on the canonical Nika TaskQueue.

    The adapter stores only bounded material references and evidence identity in
    the existing task payload. It deliberately does not persist document bodies,
    prompts, model responses, credentials, or a second scheduler/queue state.
    """

    def __init__(self, task_queue_service: task_queue.TaskQueue) -> None:
        self._tasks = task_queue_service

    def enqueue(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        material: StudyMaterial,
    ) -> StudyTask:
        _require_text(workspace_id, "workspace_id", maximum=256)
        _require_text(agent_id, "agent_id", maximum=256)
        record = self._tasks.create(
            workspace_id=workspace_id,
            agent_id=agent_id,
            payload=_material_payload(material),
        )
        self._tasks.transition(record.task_id, task_state.TaskState.READY)
        return self.get(record.task_id)

    def get(self, task_id: str) -> StudyTask:
        _require_text(task_id, "task_id", maximum=256)
        return _study_task_from_record(self._tasks.get(task_id))

    def list_recent(
        self,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> tuple[StudyTask, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if workspace_id is not None:
            _require_text(workspace_id, "workspace_id", maximum=256)
        if agent_id is not None:
            _require_text(agent_id, "agent_id", maximum=256)

        scan_limit = min(500, max(limit * 5, limit))
        selected: list[StudyTask] = []
        for record in self._tasks.list_recent(limit=scan_limit):
            if record.payload.get("nika_kind") != _STUDY_PAYLOAD_KIND:
                continue
            if workspace_id is not None and record.workspace_id != workspace_id:
                continue
            if agent_id is not None and record.agent_id != agent_id:
                continue
            selected.append(_study_task_from_record(record))
            if len(selected) >= limit:
                break
        return tuple(selected)

    def recover_created(self, *, limit: int = 100) -> tuple[StudyTask, ...]:
        """Move interrupted enqueue operations from CREATED to READY.

        Task creation and READY transition are intentionally ordinary canonical
        TaskQueue operations. If a process dies between them, this bounded
        recovery pass resumes only study tasks and does not touch other work.
        """
        recovered: list[StudyTask] = []
        for task in self.list_recent(limit=limit):
            if task.state is task_state.TaskState.CREATED:
                self._tasks.transition(task.task_id, task_state.TaskState.READY)
                recovered.append(self.get(task.task_id))
        return tuple(recovered)

    def start(self, task_id: str) -> StudyTask:
        return self._transition(task_id, task_state.TaskState.RUNNING)

    def pause(self, task_id: str) -> StudyTask:
        return self._transition(task_id, task_state.TaskState.PAUSED)

    def resume(self, task_id: str) -> StudyTask:
        task = self.get(task_id)
        if task.state in {
            task_state.TaskState.CREATED,
            task_state.TaskState.PAUSED,
            task_state.TaskState.BLOCKED,
            task_state.TaskState.FAILED,
        }:
            self._tasks.transition(task_id, task_state.TaskState.READY)
        else:
            raise ValueError(f"study task cannot resume from {task.state.value}")
        return self.get(task_id)

    def complete(self, task_id: str) -> StudyTask:
        return self._transition(task_id, task_state.TaskState.COMPLETED)

    def fail(self, task_id: str) -> StudyTask:
        return self._transition(task_id, task_state.TaskState.FAILED)

    def cancel(self, task_id: str) -> StudyTask:
        return self._transition(task_id, task_state.TaskState.CANCELLED)

    def _transition(self, task_id: str, target: task_state.TaskState) -> StudyTask:
        self.get(task_id)
        self._tasks.transition(task_id, target)
        return self.get(task_id)


def _semantic_payload(material: StudyMaterial) -> dict[str, object]:
    payload: dict[str, object] = {
        "nika_kind": _STUDY_PAYLOAD_KIND,
        "material_id": material.material_id,
        "title": material.title,
        "material_kind": material.kind.value,
        "source_ref": material.source_ref,
        "evidence_policy": _EVIDENCE_POLICY,
    }
    if material.source_version is not None:
        payload["source_version"] = material.source_version
    if material.content_sha256 is not None:
        payload["content_sha256"] = material.content_sha256
    if material.learning_goal is not None:
        payload["learning_goal"] = material.learning_goal
    return payload


def _payload_fingerprint(material: StudyMaterial) -> str:
    encoded = json.dumps(
        _semantic_payload(material),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _material_payload(material: StudyMaterial) -> dict[str, object]:
    payload = _semantic_payload(material)
    payload["study_fingerprint"] = _payload_fingerprint(material)
    return payload


def _study_task_from_record(record: task_queue.TaskRecord) -> StudyTask:
    payload = record.payload
    if payload.get("nika_kind") != _STUDY_PAYLOAD_KIND:
        raise ValueError("task is not a study task")
    try:
        material_id = _required_payload_text(payload, "material_id")
        title = _required_payload_text(payload, "title")
        source_ref = _required_payload_text(payload, "source_ref")
        kind = StudyMaterialKind(_required_payload_text(payload, "material_kind"))
        source_version = _optional_payload_text(payload, "source_version")
        content_sha256 = _optional_payload_text(payload, "content_sha256")
        learning_goal = _optional_payload_text(payload, "learning_goal")
        fingerprint = _required_payload_text(payload, "study_fingerprint")
        _validate_sha256(fingerprint, "study_fingerprint")
        if payload.get("evidence_policy") != _EVIDENCE_POLICY:
            raise ValueError("unsupported evidence policy")
        material = StudyMaterial(
            material_id=material_id,
            title=title,
            kind=kind,
            source_ref=source_ref,
            source_version=source_version,
            content_sha256=content_sha256,
            learning_goal=learning_goal,
        )
        if not hmac.compare_digest(fingerprint, _payload_fingerprint(material)):
            raise ValueError("study payload fingerprint mismatch")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid durable study task payload") from exc
    return StudyTask(
        task_id=record.task_id,
        workspace_id=record.workspace_id,
        agent_id=record.agent_id,
        state=record.state,
        material=material,
    )


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _optional_payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{key} must be text")
    return value


def _require_text(value: str, name: str, *, maximum: int) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum length")
    if "\x00" in value:
        raise ValueError(f"{name} contains an invalid character")


def _validate_sha256(value: str, name: str) -> None:
    if (
        len(value) != 64
        or any(char not in string.hexdigits for char in value)
        or value != value.lower()
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _decoded_views(value: str) -> tuple[str, ...]:
    views = [value]
    current = value
    for _ in range(5):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            return tuple(views)
        views.append(decoded)
        current = decoded
    if urllib.parse.unquote(current) != current:
        raise ValueError("source_ref encoding depth exceeds safety limit")
    return tuple(views)


def _reject_secret_bearing_reference(value: str) -> None:
    for view in _decoded_views(value):
        lowered = view.casefold()
        if any(marker in lowered for marker in _SECRET_TEXT_MARKERS):
            raise ValueError("source_ref must not contain credential material")
        parsed = urllib.parse.urlsplit(view)
        if parsed.scheme.casefold() in {"http", "https"}:
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("source_ref must not contain URL credentials")
            for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if key.casefold() in _SECRET_QUERY_KEYS:
                    raise ValueError("source_ref must not contain credential query fields")
