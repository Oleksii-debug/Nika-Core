from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchResultSet, SourceKind
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.profiles import (
    ResearchProfile,
    ResearchProfileRepository,
    ResearchSourceSet,
)


class PreviousObservationErrorCode(StrEnum):
    MISSING_BASELINE = "missing_baseline"
    CORRUPT_BASELINE = "corrupt_baseline"
    IDENTITY_MISMATCH = "identity_mismatch"
    STALE_VERSION = "stale_version"
    DUPLICATE_BASELINE = "duplicate_baseline"


class PreviousObservationError(RuntimeError):
    """Fail-closed error raised when a durable monitoring baseline cannot be trusted."""

    def __init__(self, code: PreviousObservationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreviousObservationExpectation:
    series_id: str
    workspace_id: str
    profile_id: str
    profile_version: int
    source_set_id: str
    source_set_version: int

    def __post_init__(self) -> None:
        for field_name in ("series_id", "workspace_id", "profile_id", "source_set_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")
        for field_name in ("profile_version", "source_set_version"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class DurablePreviousObservation:
    task_id: str
    series_id: str
    profile_id: str
    profile_version: int
    source_set_id: str
    source_set_version: int
    result_set: ResearchResultSet
    created_at: str


class DurablePreviousObservationLoader:
    """Load the exact previous Research observation from canonical SQLite state only."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        profiles: ResearchProfileRepository,
        network_repository: NetworkResearchRepository,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._network = network_repository

    def load(self, expected: PreviousObservationExpectation) -> DurablePreviousObservation:
        rows = self._latest_history_rows(expected.series_id)
        if not rows:
            self._fail(
                PreviousObservationErrorCode.MISSING_BASELINE,
                f"no durable previous observation exists for series {expected.series_id!r}",
            )
        latest = rows[0]
        if len(rows) > 1 and rows[1]["created_at"] == latest["created_at"]:
            self._fail(
                PreviousObservationErrorCode.DUPLICATE_BASELINE,
                "latest durable previous observation is ambiguous: multiple history rows "
                f"share created_at={latest['created_at']!r}",
            )

        self._validate_history_identity(latest, expected)
        self._validate_task_binding(latest["task_id"], expected.series_id)
        profile, source_set = self._load_canonical_definitions(expected)
        self._validate_live_source_bindings(source_set)
        result_set = self._load_result_set(latest["result_set_id"])
        if result_set.workspace_id != expected.workspace_id:
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "durable previous result set belongs to a different workspace",
            )
        if result_set.created_at != latest["created_at"]:
            self._fail(
                PreviousObservationErrorCode.CORRUPT_BASELINE,
                "durable previous result-set timestamp does not match run history",
            )
        if result_set.query != profile.query_text:
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "durable previous result-set query does not match the versioned profile",
            )
        self._validate_result_items(result_set, source_set)
        return DurablePreviousObservation(
            task_id=latest["task_id"],
            series_id=latest["series_id"],
            profile_id=latest["profile_id"],
            profile_version=int(latest["profile_version"]),
            source_set_id=latest["source_set_id"],
            source_set_version=int(latest["source_set_version"]),
            result_set=result_set,
            created_at=latest["created_at"],
        )

    def _latest_history_rows(self, series_id: str):
        with self._store.connection() as conn:
            return conn.execute(
                """SELECT task_id, series_id, profile_id, profile_version,
                          source_set_id, source_set_version, result_set_id, created_at
                FROM research_profile_run_history
                WHERE series_id=?
                ORDER BY created_at DESC, task_id DESC
                LIMIT 2""",
                (series_id,),
            ).fetchall()

    def _validate_history_identity(self, row, expected: PreviousObservationExpectation) -> None:
        if (
            row["profile_id"] != expected.profile_id
            or row["source_set_id"] != expected.source_set_id
        ):
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "durable previous observation profile/source-set identity does not match request",
            )
        try:
            profile_version = int(row["profile_version"])
            source_set_version = int(row["source_set_version"])
        except (TypeError, ValueError):
            self._fail(
                PreviousObservationErrorCode.CORRUPT_BASELINE,
                "durable previous observation contains invalid version fields",
            )
        if (
            profile_version != expected.profile_version
            or source_set_version != expected.source_set_version
        ):
            self._fail(
                PreviousObservationErrorCode.STALE_VERSION,
                "durable previous observation uses a different profile/source-set version",
            )

    def _validate_task_binding(self, task_id: str, series_id: str) -> None:
        with self._store.connection() as conn:
            binding = conn.execute(
                """SELECT 1 FROM research_profile_series_tasks
                WHERE series_id=? AND task_id=?""",
                (series_id, task_id),
            ).fetchone()
        if binding is None:
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "durable previous observation task is not bound to the requested series",
            )

    def _load_canonical_definitions(
        self,
        expected: PreviousObservationExpectation,
    ) -> tuple[ResearchProfile, ResearchSourceSet]:
        try:
            profile = self._profiles.load_profile(expected.profile_id, expected.profile_version)
            source_set = self._profiles.load_source_set(
                expected.source_set_id,
                expected.source_set_version,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._fail(
                PreviousObservationErrorCode.CORRUPT_BASELINE,
                f"canonical profile/source-set definition cannot be loaded: {type(exc).__name__}",
            )
        if (
            profile.workspace_id != expected.workspace_id
            or source_set.workspace_id != expected.workspace_id
        ):
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "canonical profile/source set belongs to a different workspace",
            )
        if (
            profile.source_set_id != expected.source_set_id
            or profile.source_set_version != expected.source_set_version
        ):
            self._fail(
                PreviousObservationErrorCode.IDENTITY_MISMATCH,
                "canonical profile does not bind the requested source-set identity",
            )
        return profile, source_set

    def _load_result_set(self, result_set_id: str) -> ResearchResultSet:
        try:
            return self._network.get_result_set(result_set_id)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._fail(
                PreviousObservationErrorCode.CORRUPT_BASELINE,
                f"durable previous result set cannot be loaded: {type(exc).__name__}",
            )

    def _validate_live_source_bindings(self, source_set: ResearchSourceSet) -> None:
        with self._store.connection() as conn:
            for source in source_set.sources:
                local = conn.execute(
                    "SELECT workspace_id FROM research_sources WHERE source_id=?",
                    (source.source_id,),
                ).fetchone()
                http = conn.execute(
                    "SELECT workspace_id FROM research_http_sources WHERE source_id=?",
                    (source.source_id,),
                ).fetchone()
                if local is not None and http is not None:
                    self._fail(
                        PreviousObservationErrorCode.IDENTITY_MISMATCH,
                        f"source identity is ambiguous across source tables: {source.source_id!r}",
                    )
                selected = local if source.kind is SourceKind.LOCAL_FILE else http
                other = http if source.kind is SourceKind.LOCAL_FILE else local
                if selected is None or other is not None:
                    self._fail(
                        PreviousObservationErrorCode.IDENTITY_MISMATCH,
                        f"source kind/identity no longer matches source set: {source.source_id!r}",
                    )
                if selected["workspace_id"] != source_set.workspace_id:
                    self._fail(
                        PreviousObservationErrorCode.IDENTITY_MISMATCH,
                        f"source moved across workspace boundary: {source.source_id!r}",
                    )

    def _validate_result_items(
        self,
        result_set: ResearchResultSet,
        source_set: ResearchSourceSet,
    ) -> None:
        expected_sources = {(source.kind, source.source_id) for source in source_set.sources}
        ordinals = tuple(item.ordinal for item in result_set.items)
        if ordinals != tuple(range(len(result_set.items))):
            self._fail(
                PreviousObservationErrorCode.CORRUPT_BASELINE,
                "durable previous result set has non-contiguous item ordinals",
            )
        for item in result_set.items:
            if not item.evidence:
                self._fail(
                    PreviousObservationErrorCode.CORRUPT_BASELINE,
                    f"durable previous result item {item.document_id!r} has no evidence",
                )
            for evidence in item.evidence:
                if (evidence.source_kind, evidence.source_id) not in expected_sources:
                    self._fail(
                        PreviousObservationErrorCode.IDENTITY_MISMATCH,
                        "durable previous result evidence is bound to an unexpected source: "
                        f"{evidence.source_id!r}",
                    )

    @staticmethod
    def _fail(code: PreviousObservationErrorCode, message: str) -> None:
        raise PreviousObservationError(code, message)
