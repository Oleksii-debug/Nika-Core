from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog

_SCHEMA_VERSION = 1
_MAX_TEXT = 2048
_MAX_MACHINE_INT = (1 << 63) - 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[A-Za-z0-9_.:+-]{1,128}")
_ARCH = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
_DESCRIPTOR_KEYS = {
    "schema_version",
    "kind",
    "provider_id",
    "model_id",
    "model_version",
    "source_reference",
    "license_reference",
    "integrity_basis",
    "sha256",
    "size_bytes",
    "capabilities",
    "resources",
}
_RESOURCE_KEYS = {
    "min_system_memory_bytes",
    "min_available_memory_bytes",
    "min_vram_bytes",
    "recommended_memory_bytes",
    "cpu_architectures",
}


class ModelArtifactRegistryError(RuntimeError):
    """Durable model-artifact provenance is unavailable or internally inconsistent."""


class ModelArtifactConflictError(ModelArtifactRegistryError):
    """An existing provider/model identity was presented with different provenance."""


class ModelArtifactKind(StrEnum):
    EMBEDDED = "embedded"
    EXTERNAL_LOCAL = "external_local"
    CLOUD = "cloud"


class ModelIntegrityBasis(StrEnum):
    PROVIDER_IDENTITY = "provider_identity"
    SHA256 = "sha256"


@dataclass(frozen=True, slots=True)
class ModelArtifactResources:
    min_system_memory_bytes: int | None = None
    min_available_memory_bytes: int | None = None
    min_vram_bytes: int | None = None
    recommended_memory_bytes: int | None = None
    cpu_architectures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("min_system_memory_bytes", self.min_system_memory_bytes),
            ("min_available_memory_bytes", self.min_available_memory_bytes),
            ("min_vram_bytes", self.min_vram_bytes),
            ("recommended_memory_bytes", self.recommended_memory_bytes),
        ):
            _bounded_positive_int(name, value)
        if (
            self.min_system_memory_bytes is not None
            and self.recommended_memory_bytes is not None
            and self.recommended_memory_bytes < self.min_system_memory_bytes
        ):
            raise ValueError(
                "recommended_memory_bytes must not be below min_system_memory_bytes"
            )
        if not isinstance(self.cpu_architectures, tuple) or not all(
            isinstance(value, str) for value in self.cpu_architectures
        ):
            raise TypeError("cpu_architectures must be a tuple of text labels")
        architectures = tuple(sorted(set(self.cpu_architectures)))
        if any(_ARCH.fullmatch(value) is None for value in architectures):
            raise ValueError("cpu_architectures contains an invalid architecture label")
        object.__setattr__(self, "cpu_architectures", architectures)

    def as_dict(self) -> dict[str, object]:
        return {
            "min_system_memory_bytes": self.min_system_memory_bytes,
            "min_available_memory_bytes": self.min_available_memory_bytes,
            "min_vram_bytes": self.min_vram_bytes,
            "recommended_memory_bytes": self.recommended_memory_bytes,
            "cpu_architectures": list(self.cpu_architectures),
        }


@dataclass(frozen=True, slots=True)
class ModelArtifactDescriptor:
    """Secret-free immutable provenance for one exact provider/model identity.

    A provider-specific public identity may be the reproducible integrity boundary
    when the provider does not expose stable model bytes. When stable bytes are
    available, SHA256 requires the exact lowercase digest.
    """

    kind: ModelArtifactKind
    provider_id: str
    model_id: str
    source_reference: str
    license_reference: str
    integrity_basis: ModelIntegrityBasis
    model_version: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    capabilities: tuple[str, ...] = ()
    resources: ModelArtifactResources = field(default_factory=ModelArtifactResources)
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported model artifact schema_version")
        if not isinstance(self.kind, ModelArtifactKind):
            raise TypeError("kind must be ModelArtifactKind")
        if not isinstance(self.integrity_basis, ModelIntegrityBasis):
            raise TypeError("integrity_basis must be ModelIntegrityBasis")
        _clean_text("provider_id", self.provider_id, label=True)
        _clean_text("model_id", self.model_id)
        _public_reference("source_reference", self.source_reference)
        _public_reference("license_reference", self.license_reference)
        if self.model_version is not None:
            _clean_text("model_version", self.model_version)
        if self.integrity_basis is ModelIntegrityBasis.SHA256:
            if self.sha256 is None or _SHA256.fullmatch(self.sha256) is None:
                raise ValueError("sha256 integrity requires an exact lowercase SHA-256")
        elif self.sha256 is not None:
            raise ValueError("provider_identity integrity must not claim a content SHA-256")
        if self.size_bytes is not None:
            _bounded_positive_int("size_bytes", self.size_bytes)
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(value, str) for value in self.capabilities
        ):
            raise TypeError("capabilities must be a tuple of text labels")
        capabilities = tuple(sorted(set(self.capabilities)))
        if any(_LABEL.fullmatch(value) is None for value in capabilities):
            raise ValueError("capabilities contains an invalid capability label")
        object.__setattr__(self, "capabilities", capabilities)
        if not isinstance(self.resources, ModelArtifactResources):
            raise TypeError("resources must be ModelArtifactResources")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "source_reference": self.source_reference,
            "license_reference": self.license_reference,
            "integrity_basis": self.integrity_basis.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "capabilities": list(self.capabilities),
            "resources": self.resources.as_dict(),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def descriptor_digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def registry_key(self) -> str:
        material = f"{self.provider_id}\x00{self.model_id}".encode()
        return hashlib.sha256(material).hexdigest()

    @classmethod
    def from_json(cls, body: str) -> ModelArtifactDescriptor:
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ModelArtifactRegistryError("stored model artifact JSON is invalid") from exc
        if not isinstance(raw, dict) or set(raw) != _DESCRIPTOR_KEYS:
            raise ModelArtifactRegistryError("stored model artifact schema is invalid")
        resources = raw.get("resources")
        capabilities = raw.get("capabilities")
        if not isinstance(resources, dict) or set(resources) != _RESOURCE_KEYS:
            raise ModelArtifactRegistryError("stored model artifact resource schema is invalid")
        if not isinstance(capabilities, list) or not all(
            isinstance(value, str) for value in capabilities
        ):
            raise ModelArtifactRegistryError("stored model artifact capabilities are invalid")
        architectures = resources.get("cpu_architectures")
        if not isinstance(architectures, list) or not all(
            isinstance(value, str) for value in architectures
        ):
            raise ModelArtifactRegistryError(
                "stored model artifact cpu architectures are invalid"
            )
        try:
            return cls(
                schema_version=raw["schema_version"],
                kind=ModelArtifactKind(raw["kind"]),
                provider_id=raw["provider_id"],
                model_id=raw["model_id"],
                model_version=raw["model_version"],
                source_reference=raw["source_reference"],
                license_reference=raw["license_reference"],
                integrity_basis=ModelIntegrityBasis(raw["integrity_basis"]),
                sha256=raw["sha256"],
                size_bytes=raw["size_bytes"],
                capabilities=tuple(capabilities),
                resources=ModelArtifactResources(
                    min_system_memory_bytes=resources["min_system_memory_bytes"],
                    min_available_memory_bytes=resources["min_available_memory_bytes"],
                    min_vram_bytes=resources["min_vram_bytes"],
                    recommended_memory_bytes=resources["recommended_memory_bytes"],
                    cpu_architectures=tuple(architectures),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelArtifactRegistryError("stored model artifact values are invalid") from exc


class ModelArtifactRegistry:
    """Single durable registry for immutable provider/model provenance descriptors."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._audit = AuditLog(store)
        self._initialize()

    def _initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_artifact_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM model_artifact_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _SCHEMA_VERSION:
                raise ModelArtifactRegistryError(
                    "model artifact schema is newer than this Nika build"
                )
            if current < 1:
                conn.execute(
                    "CREATE TABLE model_artifacts ("
                    "provider_id TEXT NOT NULL, "
                    "model_id TEXT NOT NULL, "
                    "descriptor_json TEXT NOT NULL, "
                    "descriptor_digest TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, "
                    "PRIMARY KEY(provider_id, model_id))"
                )
                conn.execute(
                    "INSERT INTO model_artifact_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (1, datetime.now(UTC).isoformat()),
                )

    def register(self, descriptor: ModelArtifactDescriptor) -> str:
        if not isinstance(descriptor, ModelArtifactDescriptor):
            raise TypeError("descriptor must be ModelArtifactDescriptor")
        body = descriptor.canonical_json()
        digest = descriptor.descriptor_digest
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT descriptor_json, descriptor_digest FROM model_artifacts "
                    "WHERE provider_id = ? AND model_id = ?",
                    (descriptor.provider_id, descriptor.model_id),
                ).fetchone()
                if row is not None:
                    existing = self._validated_stored(
                        row["descriptor_json"],
                        row["descriptor_digest"],
                    )
                    if existing.canonical_json() == body:
                        return digest
                    raise ModelArtifactConflictError(
                        "model artifact identity already has different provenance"
                    )
                conn.execute(
                    "INSERT INTO model_artifacts("
                    "provider_id, model_id, descriptor_json, descriptor_digest, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        descriptor.provider_id,
                        descriptor.model_id,
                        body,
                        digest,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="model.artifact.registered",
                    entity_type="model_artifact",
                    entity_id=descriptor.registry_key,
                    payload={
                        "provider_id": descriptor.provider_id,
                        "kind": descriptor.kind.value,
                        "model_id_fingerprint": _fingerprint(descriptor.model_id),
                        "descriptor_digest": digest,
                        "integrity_basis": descriptor.integrity_basis.value,
                        "checksum_recorded": descriptor.sha256 is not None,
                        "size_recorded": descriptor.size_bytes is not None,
                    },
                )
        except sqlite3.Error as exc:
            raise ModelArtifactRegistryError("model artifact registry write failed") from exc
        return digest

    def get(self, provider_id: str, model_id: str) -> ModelArtifactDescriptor:
        _clean_text("provider_id", provider_id, label=True)
        _clean_text("model_id", model_id)
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT descriptor_json, descriptor_digest FROM model_artifacts "
                    "WHERE provider_id = ? AND model_id = ?",
                    (provider_id, model_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ModelArtifactRegistryError("model artifact registry read failed") from exc
        if row is None:
            raise KeyError((provider_id, model_id))
        return self._validated_stored(row["descriptor_json"], row["descriptor_digest"])

    def list(self) -> tuple[ModelArtifactDescriptor, ...]:
        try:
            with self._store.connection() as conn:
                rows = conn.execute(
                    "SELECT descriptor_json, descriptor_digest FROM model_artifacts "
                    "ORDER BY provider_id, model_id"
                ).fetchall()
        except sqlite3.Error as exc:
            raise ModelArtifactRegistryError("model artifact registry read failed") from exc
        return tuple(
            self._validated_stored(row["descriptor_json"], row["descriptor_digest"])
            for row in rows
        )

    @staticmethod
    def _validated_stored(body: object, digest: object) -> ModelArtifactDescriptor:
        if not isinstance(body, str) or not isinstance(digest, str):
            raise ModelArtifactRegistryError("stored model artifact row is invalid")
        calculated = hashlib.sha256(body.encode()).hexdigest()
        if _SHA256.fullmatch(digest) is None or calculated != digest:
            raise ModelArtifactRegistryError("stored model artifact digest mismatch")
        descriptor = ModelArtifactDescriptor.from_json(body)
        if descriptor.descriptor_digest != digest:
            raise ModelArtifactRegistryError("stored model artifact is not canonical")
        return descriptor


def _bounded_positive_int(name: str, value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0 or value > _MAX_MACHINE_INT:
        raise ValueError(f"{name} must be in the range 1..{_MAX_MACHINE_INT}")


def _clean_text(name: str, value: str, *, label: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value or value != value.strip() or len(value) > _MAX_TEXT:
        raise ValueError(f"{name} is empty, unbounded, or ambiguously padded")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} contains control characters")
    if label and _LABEL.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters")
    return value


def _public_reference(name: str, value: str) -> str:
    text = _clean_text(name, value)
    lowered = text.lower()
    if lowered.startswith(("env:", "credential:", "secret:")):
        raise ValueError(f"{name} must be public provenance, not a credential reference")
    if (
        text.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", text) is not None
    ):
        raise ValueError(f"{name} must not contain a local filesystem path")
    if "://" in text and not lowered.startswith(("http://", "https://")):
        raise ValueError(f"{name} uses an unsupported URL scheme")
    if lowered.startswith(("http://", "https://")):
        parsed = urlsplit(text)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{name} must be a public secret-free URL reference")
    return text


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
