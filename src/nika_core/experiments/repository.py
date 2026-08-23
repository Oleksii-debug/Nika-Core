from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from typing import Protocol

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import (
    ArtifactKind,
    DatasetSplit,
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)


class ExperimentRepository(Protocol):
    def create(self, snapshot: ExperimentSnapshot) -> None: ...

    def get(self, experiment_id: str) -> ExperimentSnapshot: ...

    def save(self, snapshot: ExperimentSnapshot) -> None: ...


class InMemoryExperimentRepository:
    """Deterministic test/prototype adapter behind the M8 repository port."""

    def __init__(self) -> None:
        self._items: dict[str, ExperimentSnapshot] = {}

    def create(self, snapshot: ExperimentSnapshot) -> None:
        experiment_id = snapshot.definition.experiment_id
        if experiment_id in self._items:
            raise ValueError(f"experiment already exists: {experiment_id}")
        self._items[experiment_id] = deepcopy(snapshot)

    def get(self, experiment_id: str) -> ExperimentSnapshot:
        try:
            return deepcopy(self._items[experiment_id])
        except KeyError as exc:
            raise KeyError(f"unknown experiment: {experiment_id}") from exc

    def save(self, snapshot: ExperimentSnapshot) -> None:
        experiment_id = snapshot.definition.experiment_id
        if experiment_id not in self._items:
            raise KeyError(f"unknown experiment: {experiment_id}")
        self._items[experiment_id] = deepcopy(snapshot)


_ALLOWED_TRANSITIONS = {
    (ExperimentStatus.DRAFT, ExperimentStatus.RUNNING),
    (ExperimentStatus.RUNNING, ExperimentStatus.COMPLETED),
    (ExperimentStatus.RUNNING, ExperimentStatus.PROMOTED),
    (ExperimentStatus.PROMOTED, ExperimentStatus.ROLLED_BACK),
}


class SQLiteExperimentRepository:
    """Durable M8 adapter with immutable definition/evidence and atomic transitions."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def create(self, snapshot: ExperimentSnapshot) -> None:
        if snapshot.status is not ExperimentStatus.DRAFT or snapshot.observations:
            raise ValueError("new experiments must begin as an empty draft")
        now = datetime.now(UTC).isoformat()
        payload = _encode_definition(snapshot.definition)
        with self._store.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO experiments(experiment_id, definition_json, status, "
                    "selected_candidate_id, previous_champion_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.definition.experiment_id,
                        payload,
                        snapshot.status.value,
                        snapshot.selected_candidate_id,
                        snapshot.previous_champion_id,
                        now,
                        now,
                    ),
                )
                self._append_event(conn, snapshot.definition.experiment_id, None, snapshot)
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: experiments.experiment_id" in str(exc):
                    raise ValueError(
                        f"experiment already exists: {snapshot.definition.experiment_id}"
                    ) from exc
                raise

    def get(self, experiment_id: str) -> ExperimentSnapshot:
        with self._store.connection() as conn:
            return self._load(conn, experiment_id)

    def save(self, snapshot: ExperimentSnapshot) -> None:
        experiment_id = snapshot.definition.experiment_id
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load(conn, experiment_id)
            if _encode_definition(current.definition) != _encode_definition(snapshot.definition):
                raise ValueError("experiment definition is immutable")
            self._validate_evidence_append_only(current, snapshot)
            transition = (current.status, snapshot.status)
            if current.status != snapshot.status and transition not in _ALLOWED_TRANSITIONS:
                raise ValueError(
                    f"invalid experiment transition: {current.status.value} -> "
                    f"{snapshot.status.value}"
                )
            current_keys = {_observation_key(item) for item in current.observations}
            now = datetime.now(UTC).isoformat()
            for item in snapshot.observations:
                if _observation_key(item) in current_keys:
                    continue
                conn.execute(
                    "INSERT INTO experiment_observations("
                    "experiment_id, candidate_id, replay_id, metric, value, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        experiment_id,
                        item.candidate_id,
                        item.replay_id,
                        item.metric,
                        float(item.value),
                        now,
                    ),
                )
            conn.execute(
                "UPDATE experiments SET status = ?, selected_candidate_id = ?, "
                "previous_champion_id = ?, updated_at = ? WHERE experiment_id = ?",
                (
                    snapshot.status.value,
                    snapshot.selected_candidate_id,
                    snapshot.previous_champion_id,
                    now,
                    experiment_id,
                ),
            )
            if (
                current.status != snapshot.status
                or current.selected_candidate_id != snapshot.selected_candidate_id
                or current.previous_champion_id != snapshot.previous_champion_id
            ):
                self._append_event(conn, experiment_id, current.status, snapshot)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        experiment_id: str,
        previous_status: ExperimentStatus | None,
        snapshot: ExperimentSnapshot,
    ) -> None:
        conn.execute(
            "INSERT INTO experiment_events("
            "experiment_id, previous_status, new_status, selected_candidate_id, "
            "previous_champion_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                experiment_id,
                None if previous_status is None else previous_status.value,
                snapshot.status.value,
                snapshot.selected_candidate_id,
                snapshot.previous_champion_id,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _validate_evidence_append_only(
        current: ExperimentSnapshot,
        proposed: ExperimentSnapshot,
    ) -> None:
        proposed_map = {_observation_key(item): item for item in proposed.observations}
        if len(proposed_map) != len(proposed.observations):
            raise ValueError("duplicate observation evidence")
        for item in current.observations:
            candidate = proposed_map.get(_observation_key(item))
            if candidate is None:
                raise ValueError("experiment evidence is append-only")
            if float(candidate.value) != float(item.value):
                raise ValueError("recorded experiment evidence is immutable")

    @staticmethod
    def _load(conn: sqlite3.Connection, experiment_id: str) -> ExperimentSnapshot:
        row = conn.execute(
            "SELECT definition_json, status, selected_candidate_id, previous_champion_id "
            "FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment: {experiment_id}")
        observation_rows = conn.execute(
            "SELECT candidate_id, replay_id, metric, value FROM experiment_observations "
            "WHERE experiment_id = ? ORDER BY observation_id",
            (experiment_id,),
        ).fetchall()
        observations = tuple(
            MetricObservation(
                candidate_id=item["candidate_id"],
                replay_id=item["replay_id"],
                metric=item["metric"],
                value=float(item["value"]),
            )
            for item in observation_rows
        )
        return ExperimentSnapshot(
            definition=_decode_definition(row["definition_json"]),
            status=ExperimentStatus(row["status"]),
            observations=observations,
            selected_candidate_id=row["selected_candidate_id"],
            previous_champion_id=row["previous_champion_id"],
        )


def _observation_key(item: MetricObservation) -> tuple[str, str, str]:
    return item.candidate_id, item.replay_id, item.metric


def _encode_definition(definition: ExperimentDefinition) -> str:
    payload = {
        "experiment_id": definition.experiment_id,
        "champion": _strategy_payload(definition.champion),
        "challengers": [_strategy_payload(item) for item in definition.challengers],
        "replays": [
            {
                "replay_id": item.replay_id,
                "dataset_ref": item.dataset_ref,
                "dataset_version": item.dataset_version,
                "split": item.split.value,
                "dataset_fingerprint": item.dataset_fingerprint,
                "data_end_at": _encode_datetime(item.data_end_at),
            }
            for item in definition.replays
        ],
        "policy": {
            "primary_metric": definition.policy.primary_metric,
            "minimum_improvement": definition.policy.minimum_improvement,
            "minimum_replays": definition.policy.minimum_replays,
            "guardrails": [
                {
                    "metric": item.metric,
                    "higher_is_better": item.higher_is_better,
                    "max_regression": item.max_regression,
                }
                for item in definition.policy.guardrails
            ],
            "primary_higher_is_better": definition.policy.primary_higher_is_better,
        },
        "evaluation_cutoff": _encode_datetime(definition.evaluation_cutoff),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _strategy_payload(item: StrategyRef) -> dict[str, object]:
    return {
        "candidate_id": item.candidate_id,
        "version": item.version,
        "artifact_kind": item.artifact_kind.value,
        "artifact_ref": item.artifact_ref,
        "permission_fingerprint": item.permission_fingerprint,
        "training_dataset_fingerprints": list(item.training_dataset_fingerprints),
    }


def _decode_definition(raw: str) -> ExperimentDefinition:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("persisted experiment definition must be an object")
    policy = _required_mapping(payload.get("policy"), "policy")
    challengers = _required_sequence(payload.get("challengers"), "challengers")
    replays = _required_sequence(payload.get("replays"), "replays")
    guardrails = _required_sequence(policy.get("guardrails"), "policy.guardrails")
    return ExperimentDefinition(
        experiment_id=_required_string(payload.get("experiment_id"), "experiment_id"),
        champion=_decode_strategy(_required_mapping(payload.get("champion"), "champion")),
        challengers=tuple(
            _decode_strategy(_required_mapping(item, "challenger")) for item in challengers
        ),
        replays=tuple(
            _decode_replay(_required_mapping(item, "replay"))
            for item in replays
        ),
        policy=PromotionPolicy(
            primary_metric=_required_string(
                policy.get("primary_metric"),
                "policy.primary_metric",
            ),
            minimum_improvement=_required_number(
                policy.get("minimum_improvement"),
                "policy.minimum_improvement",
            ),
            minimum_replays=_required_integer(
                policy.get("minimum_replays"),
                "policy.minimum_replays",
            ),
            guardrails=tuple(
                MetricRule(
                    metric=_required_string(item.get("metric"), "guardrail.metric"),
                    higher_is_better=_required_boolean(
                        item.get("higher_is_better"),
                        "guardrail.higher_is_better",
                    ),
                    max_regression=_required_number(
                        item.get("max_regression"),
                        "guardrail.max_regression",
                    ),
                )
                for item in (
                    _required_mapping(raw_guardrail, "guardrail")
                    for raw_guardrail in guardrails
                )
            ),
            primary_higher_is_better=_required_boolean(
                policy.get("primary_higher_is_better", True),
                "policy.primary_higher_is_better",
            ),
        ),
        evaluation_cutoff=_decode_datetime(payload.get("evaluation_cutoff")),
    )


def _decode_replay(payload: dict[str, object]) -> ReplayCase:
    raw_split = payload.get("split", DatasetSplit.EVALUATION.value)
    return ReplayCase(
        replay_id=_required_string(payload.get("replay_id"), "replay.replay_id"),
        dataset_ref=_required_string(payload.get("dataset_ref"), "replay.dataset_ref"),
        dataset_version=_required_string(
            payload.get("dataset_version"),
            "replay.dataset_version",
        ),
        split=DatasetSplit(_required_string(raw_split, "replay.split")),
        dataset_fingerprint=_optional_string(payload.get("dataset_fingerprint")),
        data_end_at=_decode_datetime(payload.get("data_end_at")),
    )


def _decode_strategy(payload: dict[str, object]) -> StrategyRef:
    training = payload.get("training_dataset_fingerprints", ())
    if not isinstance(training, (list, tuple)):
        raise TypeError("training_dataset_fingerprints must be a sequence")
    if any(not isinstance(item, str) for item in training):
        raise TypeError("training_dataset_fingerprints must contain only strings")
    return StrategyRef(
        candidate_id=_required_string(payload.get("candidate_id"), "strategy.candidate_id"),
        version=_required_string(payload.get("version"), "strategy.version"),
        artifact_kind=ArtifactKind(
            _required_string(payload.get("artifact_kind"), "strategy.artifact_kind")
        ),
        artifact_ref=_required_string(payload.get("artifact_ref"), "strategy.artifact_ref"),
        permission_fingerprint=_required_string(
            payload.get("permission_fingerprint"),
            "strategy.permission_fingerprint",
        ),
        training_dataset_fingerprints=tuple(training),
    )


def _encode_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _decode_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("persisted experiment datetime must be a string")
    return datetime.fromisoformat(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("persisted dataset fingerprint must be a string")
    return value


def _required_mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"persisted {field} must be an object")
    return value


def _required_sequence(value: object, field: str) -> list[object] | tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"persisted {field} must be a sequence")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"persisted {field} must be a string")
    return value


def _required_boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"persisted {field} must be a boolean")
    return value


def _required_integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"persisted {field} must be an integer")
    return value


def _required_number(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"persisted {field} must be numeric")
    return float(value)
