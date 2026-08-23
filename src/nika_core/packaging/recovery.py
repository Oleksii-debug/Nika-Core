from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_NAME = "snapshot-manifest.json"
_DATABASE_NAME = "database.sqlite3"
_MANIFEST_VERSION = 1
_MANIFEST_FIELDS = {
    "manifest_version",
    "source_release_sha",
    "database_file",
    "database_sha256",
    "database_content_sha256",
    "database_size",
    "core_schema_version",
    "product_project_schema_version",
}


class DatabaseRecoveryError(RuntimeError):
    """Raised when release database backup or restore evidence is unsafe."""


@dataclass(frozen=True, slots=True)
class DatabaseSnapshotManifest:
    manifest_version: int
    source_release_sha: str
    database_file: str
    database_sha256: str
    database_content_sha256: str
    database_size: int
    core_schema_version: int | None
    product_project_schema_version: int | None


@dataclass(frozen=True, slots=True)
class DatabaseRestoreResult:
    restored: bool
    already_restored: bool
    snapshot: DatabaseSnapshotManifest
    preserved_current: DatabaseSnapshotManifest | None


def _exact_release_sha(value: str) -> str:
    candidate = value.strip().lower()
    if not _FULL_SHA_RE.fullmatch(candidate):
        raise ValueError("release recovery requires an exact 40-character source SHA")
    return candidate


def _is_reparse_or_symlink(path: Path) -> bool:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _require_regular_file(path: Path, *, label: str) -> None:
    if not path.exists():
        raise DatabaseRecoveryError(f"{label} is missing: {path}")
    if _is_reparse_or_symlink(path):
        raise DatabaseRecoveryError(f"{label} may not be a symlink or reparse point: {path}")
    if not path.is_file():
        raise DatabaseRecoveryError(f"{label} is not a regular file: {path}")


def _require_directory(path: Path, *, label: str) -> None:
    if not path.exists():
        raise DatabaseRecoveryError(f"{label} is missing: {path}")
    if _is_reparse_or_symlink(path):
        raise DatabaseRecoveryError(f"{label} may not be a symlink or reparse point: {path}")
    if not path.is_dir():
        raise DatabaseRecoveryError(f"{label} is not a directory: {path}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: Path, *, include_wal: bool = False) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    if not include_wal:
        uri += "&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _verify_sqlite_integrity(path: Path) -> None:
    try:
        with _readonly_connection(path) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise DatabaseRecoveryError(f"SQLite integrity check failed for {path.name}") from exc
    if rows != [("ok",)]:
        raise DatabaseRecoveryError(f"SQLite integrity check did not return ok for {path.name}")


def _content_sha256(path: Path) -> str:
    handle, temporary_name = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        try:
            with (
                _readonly_connection(path) as source_connection,
                sqlite3.connect(temporary) as target_connection,
            ):
                source_connection.backup(target_connection)
        except sqlite3.Error as exc:
            message = f"could not fingerprint SQLite content for {path.name}"
            raise DatabaseRecoveryError(message) from exc
        return _sha256_file(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _schema_version(connection: sqlite3.Connection, table_name: str) -> int | None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        return None
    if table_name == "schema_migrations":
        query = "SELECT MAX(version) FROM schema_migrations"
    elif table_name == "product_project_schema_migrations":
        query = "SELECT MAX(version) FROM product_project_schema_migrations"
    else:  # pragma: no cover - internal callers are fixed above
        raise ValueError(f"unsupported schema version table: {table_name}")
    version_row = connection.execute(query).fetchone()
    value = version_row[0]
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatabaseRecoveryError(f"invalid migration version in {table_name}")
    return value


def _snapshot_manifest(database_path: Path, source_release_sha: str) -> DatabaseSnapshotManifest:
    _verify_sqlite_integrity(database_path)
    try:
        with _readonly_connection(database_path) as connection:
            core_version = _schema_version(connection, "schema_migrations")
            product_project_version = _schema_version(
                connection,
                "product_project_schema_migrations",
            )
    except sqlite3.Error as exc:
        raise DatabaseRecoveryError("could not read database migration evidence") from exc
    return DatabaseSnapshotManifest(
        manifest_version=_MANIFEST_VERSION,
        source_release_sha=source_release_sha,
        database_file=_DATABASE_NAME,
        database_sha256=_sha256_file(database_path),
        database_content_sha256=_content_sha256(database_path),
        database_size=database_path.stat().st_size,
        core_schema_version=core_version,
        product_project_schema_version=product_project_version,
    )


def _write_manifest(path: Path, manifest: DatabaseSnapshotManifest) -> None:
    path.write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _fsync_file(path)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _require_exact_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatabaseRecoveryError(f"snapshot manifest has invalid {field}")
    return value


def _require_optional_int(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    return _require_exact_int(payload, field)


def _require_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise DatabaseRecoveryError(f"snapshot manifest has invalid {field}")
    return value


def _parse_manifest(path: Path) -> DatabaseSnapshotManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatabaseRecoveryError("snapshot manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise DatabaseRecoveryError("snapshot manifest must be a JSON object")
    if set(payload) != _MANIFEST_FIELDS:
        raise DatabaseRecoveryError("snapshot manifest fields do not match schema")
    manifest_version = _require_exact_int(payload, "manifest_version")
    if manifest_version != _MANIFEST_VERSION:
        raise DatabaseRecoveryError(f"unsupported snapshot manifest version: {manifest_version}")
    source_release_sha = _exact_release_sha(_require_text(payload, "source_release_sha"))
    database_file = _require_text(payload, "database_file")
    if database_file != _DATABASE_NAME:
        raise DatabaseRecoveryError("snapshot manifest database file identity is invalid")
    database_sha256 = _require_text(payload, "database_sha256")
    database_content_sha256 = _require_text(payload, "database_content_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", database_sha256):
        raise DatabaseRecoveryError("snapshot manifest database_sha256 is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", database_content_sha256):
        raise DatabaseRecoveryError("snapshot manifest database_content_sha256 is invalid")
    return DatabaseSnapshotManifest(
        manifest_version=manifest_version,
        source_release_sha=source_release_sha,
        database_file=database_file,
        database_sha256=database_sha256,
        database_content_sha256=database_content_sha256,
        database_size=_require_exact_int(payload, "database_size"),
        core_schema_version=_require_optional_int(payload, "core_schema_version"),
        product_project_schema_version=_require_optional_int(
            payload,
            "product_project_schema_version",
        ),
    )


def _verify_manifest_matches_database(
    database_path: Path,
    manifest: DatabaseSnapshotManifest,
) -> None:
    _require_regular_file(database_path, label="snapshot database")
    if database_path.stat().st_size != manifest.database_size:
        raise DatabaseRecoveryError("snapshot database size does not match manifest")
    if _sha256_file(database_path) != manifest.database_sha256:
        raise DatabaseRecoveryError("snapshot database SHA-256 does not match manifest")
    if _content_sha256(database_path) != manifest.database_content_sha256:
        raise DatabaseRecoveryError("snapshot SQLite content does not match manifest")
    _verify_sqlite_integrity(database_path)
    rebuilt = _snapshot_manifest(database_path, manifest.source_release_sha)
    if rebuilt != manifest:
        raise DatabaseRecoveryError("snapshot migration evidence does not match manifest")


def create_database_snapshot(
    database_path: Path | str,
    snapshot_dir: Path | str,
    *,
    source_release_sha: str,
) -> DatabaseSnapshotManifest:
    """Create an immutable, SQLite-consistent release rollback snapshot."""
    source = Path(database_path)
    target = Path(snapshot_dir)
    exact_source_sha = _exact_release_sha(source_release_sha)
    _require_regular_file(source, label="source database")

    if target.exists():
        return verify_database_snapshot(target, expected_source_release_sha=exact_source_sha)

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        backup_path = stage / _DATABASE_NAME
        try:
            with (
                _readonly_connection(source, include_wal=True) as source_connection,
                sqlite3.connect(backup_path) as backup_connection,
            ):
                source_connection.backup(backup_connection)
        except sqlite3.Error as exc:
            raise DatabaseRecoveryError("SQLite online backup failed") from exc
        _fsync_file(backup_path)
        manifest = _snapshot_manifest(backup_path, exact_source_sha)
        _write_manifest(stage / _MANIFEST_NAME, manifest)
        os.replace(stage, target)
        return verify_database_snapshot(target, expected_source_release_sha=exact_source_sha)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def verify_database_snapshot(
    snapshot_dir: Path | str,
    *,
    expected_source_release_sha: str | None = None,
) -> DatabaseSnapshotManifest:
    """Verify snapshot structure, exact release identity, hashes and SQLite integrity."""
    root = Path(snapshot_dir)
    _require_directory(root, label="snapshot directory")
    entries = {entry.name for entry in root.iterdir()}
    if entries != {_DATABASE_NAME, _MANIFEST_NAME}:
        raise DatabaseRecoveryError("snapshot directory contents do not match schema")
    manifest_path = root / _MANIFEST_NAME
    database_path = root / _DATABASE_NAME
    _require_regular_file(manifest_path, label="snapshot manifest")
    manifest = _parse_manifest(manifest_path)
    if expected_source_release_sha is not None:
        expected = _exact_release_sha(expected_source_release_sha)
        if manifest.source_release_sha != expected:
            raise DatabaseRecoveryError("snapshot source release SHA does not match expectation")
    _verify_manifest_matches_database(database_path, manifest)
    return manifest


@contextmanager
def _database_recovery_lock(database_path: Path):
    lock_path = Path(f"{database_path}.nika-release-recovery.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                message = "another database recovery operation is active"
                raise DatabaseRecoveryError(message) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                message = "another database recovery operation is active"
                raise DatabaseRecoveryError(message) from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _assert_quiescent_database(database_path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            raise DatabaseRecoveryError(
                f"database restore requires quiescent SQLite state; sidecar exists: {sidecar.name}"
            )


def _copy_verified_snapshot_database(
    source: Path,
    destination: Path,
    manifest: DatabaseSnapshotManifest,
) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-",
        dir=destination.parent,
    )
    os.close(handle)
    stage = Path(temporary_name)
    try:
        shutil.copyfile(source, stage)
        _fsync_file(stage)
        _verify_manifest_matches_database(stage, manifest)
        os.replace(stage, destination)
        _fsync_file(destination)
    finally:
        stage.unlink(missing_ok=True)


def _restore_database_snapshot_locked(
    snapshot_dir: Path | str,
    database_path: Path | str,
    *,
    expected_source_release_sha: str,
    current_release_sha: str | None = None,
    preserve_current_to: Path | str | None = None,
) -> DatabaseRestoreResult:
    """Restore a verified rollback snapshot with mandatory preservation of replaced state."""
    root = Path(snapshot_dir)
    destination = Path(database_path)
    manifest = verify_database_snapshot(
        root,
        expected_source_release_sha=expected_source_release_sha,
    )
    source = root / _DATABASE_NAME
    root_absolute = root.absolute()
    destination_absolute = destination.absolute()
    if destination_absolute.is_relative_to(root_absolute):
        raise DatabaseRecoveryError("restore destination may not be inside the snapshot")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_quiescent_database(destination)

    preserved: DatabaseSnapshotManifest | None = None
    if destination.exists():
        _require_regular_file(destination, label="current database")
        _verify_sqlite_integrity(destination)
        current_content_sha256 = _content_sha256(destination)
        if current_content_sha256 == manifest.database_content_sha256:
            return DatabaseRestoreResult(
                restored=False,
                already_restored=True,
                snapshot=manifest,
                preserved_current=None,
            )
        if current_release_sha is None or preserve_current_to is None:
            raise DatabaseRecoveryError(
                "replacing an existing database requires current release SHA and preservation path"
            )
        preservation_path = Path(preserve_current_to)
        preservation_absolute = preservation_path.absolute()
        if preservation_absolute.is_relative_to(root_absolute):
            raise DatabaseRecoveryError("preservation path may not be inside the restore snapshot")
        if root_absolute.is_relative_to(preservation_absolute):
            raise DatabaseRecoveryError("preservation path may not contain the restore snapshot")
        preserved = create_database_snapshot(
            destination,
            preservation_path,
            source_release_sha=current_release_sha,
        )
        if preserved.database_content_sha256 != current_content_sha256:
            raise DatabaseRecoveryError(
                "preservation snapshot does not match current database; reconcile first"
            )

    _copy_verified_snapshot_database(source, destination, manifest)
    restored = verify_database_file_against_snapshot(destination, manifest)
    if not restored:
        raise DatabaseRecoveryError("restored database does not match verified snapshot")
    return DatabaseRestoreResult(
        restored=True,
        already_restored=False,
        snapshot=manifest,
        preserved_current=preserved,
    )


def restore_database_snapshot(
    snapshot_dir: Path | str,
    database_path: Path | str,
    *,
    expected_source_release_sha: str,
    current_release_sha: str | None = None,
    preserve_current_to: Path | str | None = None,
) -> DatabaseRestoreResult:
    """Restore one verified snapshot under an exclusive recovery-operation lock."""
    root = Path(snapshot_dir)
    destination = Path(database_path)
    if destination.absolute().is_relative_to(root.absolute()):
        raise DatabaseRecoveryError("restore destination may not be inside the snapshot")
    with _database_recovery_lock(destination):
        return _restore_database_snapshot_locked(
            snapshot_dir,
            destination,
            expected_source_release_sha=expected_source_release_sha,
            current_release_sha=current_release_sha,
            preserve_current_to=preserve_current_to,
        )


def verify_database_file_against_snapshot(
    database_path: Path | str,
    manifest: DatabaseSnapshotManifest,
) -> bool:
    """Return whether one quiescent database file exactly matches verified snapshot evidence."""
    path = Path(database_path)
    try:
        _require_regular_file(path, label="database")
        _verify_manifest_matches_database(path, manifest)
    except DatabaseRecoveryError:
        return False
    return True
