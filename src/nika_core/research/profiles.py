from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import FreshnessState, SourceKind
from nika_core.research.query import (
    DeterministicResearchQueryService,
    ResearchQueryExecution,
    ResearchQuerySpec,
    ResearchSearchFilters,
    SearchMode,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchSourceRef:
    source_id: str
    kind: SourceKind


@dataclass(frozen=True, slots=True)
class ResearchSourceSet:
    source_set_id: str
    workspace_id: str
    version: int
    name: str
    sources: tuple[ResearchSourceRef, ...]


@dataclass(frozen=True, slots=True)
class ResearchProfile:
    profile_id: str
    workspace_id: str
    version: int
    name: str
    source_set_id: str
    source_set_version: int
    query_text: str
    query_mode: SearchMode = SearchMode.LITERAL
    filters: ResearchSearchFilters = field(default_factory=ResearchSearchFilters)
    result_limit: int = 20


@dataclass(frozen=True, slots=True)
class ResearchProfileExecution:
    profile: ResearchProfile
    source_set: ResearchSourceSet
    query: ResearchQueryExecution


class ResearchProfileRepository:
    """Immutable versioned research definitions persisted in canonical Nika SQLite."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def save_source_set(self, source_set: ResearchSourceSet) -> ResearchSourceSet:
        self._validate_source_set(source_set)
        existing = self._load_source_set_optional(source_set.source_set_id, source_set.version)
        if existing is not None:
            if existing != source_set:
                raise ValueError("source set version already exists with different content")
            return existing

        now = _now()
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_source_sets(
                    source_set_id, workspace_id, version, name, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    source_set.source_set_id,
                    source_set.workspace_id,
                    source_set.version,
                    source_set.name,
                    now,
                ),
            )
            conn.executemany(
                """INSERT INTO research_source_set_members(
                    source_set_id, source_set_version, ordinal, source_id, source_kind
                ) VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        source_set.source_set_id,
                        source_set.version,
                        ordinal,
                        source.source_id,
                        source.kind.value,
                    )
                    for ordinal, source in enumerate(source_set.sources)
                ],
            )
        return source_set

    def load_source_set(
        self,
        source_set_id: str,
        version: int | None = None,
    ) -> ResearchSourceSet:
        normalized_id = _required(source_set_id, "source_set_id")
        with self._store.connection() as conn:
            if version is None:
                row = conn.execute(
                    """SELECT * FROM research_source_sets
                    WHERE source_set_id=? ORDER BY version DESC LIMIT 1""",
                    (normalized_id,),
                ).fetchone()
            else:
                if version < 1:
                    raise ValueError("version must be positive")
                row = conn.execute(
                    """SELECT * FROM research_source_sets
                    WHERE source_set_id=? AND version=?""",
                    (normalized_id, version),
                ).fetchone()
            if row is None:
                raise KeyError(f"research source set not found: {normalized_id}")
            member_rows = conn.execute(
                """SELECT source_id, source_kind FROM research_source_set_members
                WHERE source_set_id=? AND source_set_version=? ORDER BY ordinal""",
                (row["source_set_id"], row["version"]),
            ).fetchall()
        return ResearchSourceSet(
            source_set_id=row["source_set_id"],
            workspace_id=row["workspace_id"],
            version=int(row["version"]),
            name=row["name"],
            sources=tuple(
                ResearchSourceRef(
                    source_id=member["source_id"],
                    kind=SourceKind(member["source_kind"]),
                )
                for member in member_rows
            ),
        )

    def save_profile(self, profile: ResearchProfile) -> ResearchProfile:
        source_set = self._validate_profile(profile)
        existing = self._load_profile_optional(profile.profile_id, profile.version)
        if existing is not None:
            if existing != profile:
                raise ValueError("research profile version already exists with different content")
            return existing

        if profile.filters.freshness and not any(
            source.kind is SourceKind.HTTP for source in source_set.sources
        ):
            raise ValueError("freshness filters require an HTTP source in the source set")

        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO research_profiles(
                    profile_id, workspace_id, version, name,
                    source_set_id, source_set_version,
                    query_text, query_mode, filters_json, result_limit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    profile.profile_id,
                    profile.workspace_id,
                    profile.version,
                    profile.name,
                    profile.source_set_id,
                    profile.source_set_version,
                    profile.query_text,
                    profile.query_mode.value,
                    self._filters_json(profile.filters),
                    profile.result_limit,
                    _now(),
                ),
            )
        return profile

    def load_profile(
        self,
        profile_id: str,
        version: int | None = None,
    ) -> ResearchProfile:
        normalized_id = _required(profile_id, "profile_id")
        with self._store.connection() as conn:
            if version is None:
                row = conn.execute(
                    """SELECT * FROM research_profiles
                    WHERE profile_id=? ORDER BY version DESC LIMIT 1""",
                    (normalized_id,),
                ).fetchone()
            else:
                if version < 1:
                    raise ValueError("version must be positive")
                row = conn.execute(
                    """SELECT * FROM research_profiles
                    WHERE profile_id=? AND version=?""",
                    (normalized_id, version),
                ).fetchone()
        if row is None:
            raise KeyError(f"research profile not found: {normalized_id}")
        return ResearchProfile(
            profile_id=row["profile_id"],
            workspace_id=row["workspace_id"],
            version=int(row["version"]),
            name=row["name"],
            source_set_id=row["source_set_id"],
            source_set_version=int(row["source_set_version"]),
            query_text=row["query_text"],
            query_mode=SearchMode(row["query_mode"]),
            filters=self._filters_from_json(row["filters_json"]),
            result_limit=int(row["result_limit"]),
        )

    def _load_source_set_optional(
        self,
        source_set_id: str,
        version: int,
    ) -> ResearchSourceSet | None:
        try:
            return self.load_source_set(source_set_id, version)
        except KeyError:
            return None

    def _load_profile_optional(
        self,
        profile_id: str,
        version: int,
    ) -> ResearchProfile | None:
        try:
            return self.load_profile(profile_id, version)
        except KeyError:
            return None

    def _validate_source_set(self, source_set: ResearchSourceSet) -> None:
        _required(source_set.source_set_id, "source_set_id")
        workspace_id = _required(source_set.workspace_id, "workspace_id")
        _required(source_set.name, "name")
        if source_set.version < 1:
            raise ValueError("source set version must be positive")
        if not source_set.sources:
            raise ValueError("source set must contain at least one source")
        source_ids = [_required(source.source_id, "source_id") for source in source_set.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source set must not contain duplicate source IDs")

        with self._store.connection() as conn:
            workspace = conn.execute(
                "SELECT 1 FROM research_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise ValueError(f"unknown research workspace: {workspace_id}")
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
                    raise ValueError(f"ambiguous source ID exists in both source tables: {source.source_id}")
                row = local if source.kind is SourceKind.LOCAL_FILE else http
                other = http if source.kind is SourceKind.LOCAL_FILE else local
                if row is None:
                    if other is not None:
                        raise ValueError(
                            f"source kind mismatch for {source.source_id}: expected {source.kind.value}"
                        )
                    raise ValueError(f"unknown source: {source.source_id}")
                if row["workspace_id"] != workspace_id:
                    raise ValueError("source set crosses workspace boundary")

    def _validate_profile(self, profile: ResearchProfile) -> ResearchSourceSet:
        _required(profile.profile_id, "profile_id")
        _required(profile.workspace_id, "workspace_id")
        _required(profile.name, "name")
        _required(profile.query_text, "query_text")
        _required(profile.source_set_id, "source_set_id")
        if profile.version < 1:
            raise ValueError("profile version must be positive")
        if profile.source_set_version < 1:
            raise ValueError("source_set_version must be positive")
        if profile.result_limit < 1 or profile.result_limit > 100:
            raise ValueError("result_limit must be between 1 and 100")
        if profile.filters.source_ids:
            raise ValueError("profile source IDs come from its versioned source set")
        self._validate_filters(profile.filters)
        source_set = self.load_source_set(profile.source_set_id, profile.source_set_version)
        if source_set.workspace_id != profile.workspace_id:
            raise ValueError("profile and source set workspaces do not match")
        return source_set

    @staticmethod
    def _validate_filters(filters: ResearchSearchFilters) -> None:
        if any(not media_type.strip() for media_type in filters.media_types):
            raise ValueError("media_types must not contain empty values")
        if (
            filters.freshness
            and filters.source_kinds
            and SourceKind.HTTP not in filters.source_kinds
        ):
            raise ValueError("freshness filters require HTTP sources")

    @staticmethod
    def _filters_json(filters: ResearchSearchFilters) -> str:
        payload = {
            "freshness": [value.value for value in filters.freshness],
            "media_types": list(filters.media_types),
            "source_kinds": [value.value for value in filters.source_kinds],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _filters_from_json(raw: str) -> ResearchSearchFilters:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("stored research profile filters must be an object")
        allowed = {"freshness", "media_types", "source_kinds"}
        if set(payload) != allowed:
            raise ValueError("stored research profile filters have an unsupported schema")
        freshness = payload["freshness"]
        media_types = payload["media_types"]
        source_kinds = payload["source_kinds"]
        if not all(isinstance(value, list) for value in (freshness, media_types, source_kinds)):
            raise TypeError("stored research profile filters have invalid collection types")
        if not all(isinstance(value, str) for value in (*freshness, *media_types, *source_kinds)):
            raise TypeError("stored research profile filters contain non-string values")
        filters = ResearchSearchFilters(
            source_kinds=tuple(SourceKind(value) for value in source_kinds),
            media_types=tuple(media_types),
            freshness=tuple(FreshnessState(value) for value in freshness),
        )
        ResearchProfileRepository._validate_filters(filters)
        return filters


class ResearchProfileService:
    def __init__(
        self,
        *,
        repository: ResearchProfileRepository,
        query_service: DeterministicResearchQueryService,
    ) -> None:
        self._repository = repository
        self._query_service = query_service

    def execute(
        self,
        profile_id: str,
        version: int | None = None,
    ) -> ResearchProfileExecution:
        profile = self._repository.load_profile(profile_id, version)
        source_set = self._repository.load_source_set(
            profile.source_set_id,
            profile.source_set_version,
        )
        filters = ResearchSearchFilters(
            source_ids=tuple(source.source_id for source in source_set.sources),
            source_kinds=profile.filters.source_kinds,
            media_types=profile.filters.media_types,
            freshness=profile.filters.freshness,
        )
        query = self._query_service.execute(
            ResearchQuerySpec(
                workspace_id=profile.workspace_id,
                text=profile.query_text,
                mode=profile.query_mode,
                filters=filters,
                limit=profile.result_limit,
            )
        )
        return ResearchProfileExecution(
            profile=profile,
            source_set=source_set,
            query=query,
        )
