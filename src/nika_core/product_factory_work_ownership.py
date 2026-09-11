from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nika_core.data.sqlite import SQLiteStore


class WorkOwnershipError(ValueError):
    """Raised when Product Factory work ownership authority is violated."""


@dataclass(frozen=True, slots=True)
class WorkOwnershipLease:
    project_id: str
    work_id: str
    owner_id: str
    fence: int
    issued_at: datetime
    expires_at: datetime


class ProductFactoryWorkOwnership:
    """Durable single-writer lease authority for Product Factory work slices.

    Production time authority is internal. Tests may inject one trusted clock when the
    service is constructed; individual callers cannot choose the instant used to expire,
    renew, take over, or assert a lease.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or _utc_now

    def acquire(
        self,
        *,
        project_id: str,
        work_id: str,
        owner_id: str,
        lease_seconds: int = 300,
    ) -> WorkOwnershipLease:
        _identity(project_id, work_id, owner_id)
        _lease_seconds(lease_seconds)
        with self._store.connection() as connection:
            _begin_immediate(connection)
            instant = self._instant()
            expires_at = _expiry(instant, lease_seconds)
            row = connection.execute(
                "SELECT owner_id, fence, issued_at, expires_at FROM product_factory_work_ownership "
                "WHERE project_id = ? AND work_id = ?",
                (project_id, work_id),
            ).fetchone()
            if row is None:
                fence = 1
                connection.execute(
                    "INSERT INTO product_factory_work_ownership "
                    "(project_id, work_id, owner_id, fence, issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, work_id, owner_id, fence, _stamp(instant), _stamp(expires_at)),
                )
            else:
                current_owner = row[0]
                current_fence = _strict_fence(row[1])
                current_issued = _optional_time(row[2])
                current_expires = _optional_time(row[3])
                if current_owner is None:
                    if current_issued is not None or current_expires is not None:
                        raise WorkOwnershipError("corrupt work ownership record")
                elif (
                    not isinstance(current_owner, str)
                    or not current_owner.strip()
                    or current_owner != current_owner.strip()
                    or current_issued is None
                    or current_expires is None
                    or current_expires <= current_issued
                ):
                    raise WorkOwnershipError("corrupt work ownership record")
                if current_owner is not None and current_expires > instant:
                    if instant < current_issued:
                        raise WorkOwnershipError("work ownership clock precedes lease issuance")
                    if current_owner == owner_id:
                        raise WorkOwnershipError(
                            "work is already owned by this owner; renew the existing lease"
                        )
                    raise WorkOwnershipError("work is already owned by another active owner")
                fence = current_fence + 1
                connection.execute(
                    "UPDATE product_factory_work_ownership SET owner_id = ?, fence = ?, "
                    "issued_at = ?, expires_at = ? WHERE project_id = ? AND work_id = ?",
                    (owner_id, fence, _stamp(instant), _stamp(expires_at), project_id, work_id),
                )
            _commit_mutation(connection)
        return WorkOwnershipLease(project_id, work_id, owner_id, fence, instant, expires_at)

    def renew(
        self,
        *,
        project_id: str,
        work_id: str,
        owner_id: str,
        fence: int,
        lease_seconds: int = 300,
    ) -> WorkOwnershipLease:
        _identity(project_id, work_id, owner_id)
        _strict_fence(fence)
        _lease_seconds(lease_seconds)
        with self._store.connection() as connection:
            _begin_immediate(connection)
            instant = self._instant()
            expires_at = _expiry(instant, lease_seconds)
            current = _load(connection, project_id, work_id)
            _assert_exact(current, owner_id=owner_id, fence=fence, now=instant)
            assert current is not None
            if expires_at <= current.expires_at:
                raise WorkOwnershipError("renewal must extend the current lease")
            connection.execute(
                "UPDATE product_factory_work_ownership SET expires_at = ? "
                "WHERE project_id = ? AND work_id = ?",
                (_stamp(expires_at), project_id, work_id),
            )
            _commit_mutation(connection)
        return WorkOwnershipLease(project_id, work_id, owner_id, fence, current.issued_at, expires_at)

    def release(self, *, project_id: str, work_id: str, owner_id: str, fence: int) -> None:
        _identity(project_id, work_id, owner_id)
        _strict_fence(fence)
        with self._store.connection() as connection:
            _begin_immediate(connection)
            instant = self._instant()
            current = _load(connection, project_id, work_id)
            _assert_exact(current, owner_id=owner_id, fence=fence, now=instant)
            connection.execute(
                "UPDATE product_factory_work_ownership SET owner_id = NULL, "
                "issued_at = NULL, expires_at = NULL WHERE project_id = ? AND work_id = ?",
                (project_id, work_id),
            )
            _commit_mutation(connection)

    def current(self, *, project_id: str, work_id: str) -> WorkOwnershipLease | None:
        _identity(project_id, work_id)
        instant = self._instant()
        with self._store.connection() as connection:
            current = _load(connection, project_id, work_id)
        if current is None:
            return None
        _validate_observation_time(current, instant)
        if current.expires_at <= instant:
            return None
        return current

    def assert_owner(
        self,
        *,
        project_id: str,
        work_id: str,
        owner_id: str,
        fence: int,
    ) -> None:
        _identity(project_id, work_id, owner_id)
        _strict_fence(fence)
        instant = self._instant()
        with self._store.connection() as connection:
            current = _load(connection, project_id, work_id)
        _assert_exact(current, owner_id=owner_id, fence=fence, now=instant)

    def assert_owner_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        work_id: str,
        owner_id: str,
        fence: int,
    ) -> None:
        """Assert exact ownership inside the caller's already-serialized mutation transaction."""
        _identity(project_id, work_id, owner_id)
        _strict_fence(fence)
        if not connection.in_transaction:
            raise WorkOwnershipError("fenced mutation requires an active SQLite transaction")
        instant = self._instant()
        current = _load(connection, project_id, work_id)
        _assert_exact(current, owner_id=owner_id, fence=fence, now=instant)

    def _instant(self) -> datetime:
        try:
            value = self._clock()
        except WorkOwnershipError:
            raise
        except Exception as exc:
            raise WorkOwnershipError("work ownership clock failed") from exc
        if not isinstance(value, datetime):
            raise WorkOwnershipError("work ownership clock must return datetime")
        return _aware(value)


def _load(
    connection: sqlite3.Connection,
    project_id: str,
    work_id: str,
) -> WorkOwnershipLease | None:
    row = connection.execute(
        "SELECT owner_id, fence, issued_at, expires_at FROM product_factory_work_ownership "
        "WHERE project_id = ? AND work_id = ?",
        (project_id, work_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    issued_at = _optional_time(row[2])
    expires_at = _optional_time(row[3])
    if issued_at is None or expires_at is None or expires_at <= issued_at:
        raise WorkOwnershipError("corrupt work ownership record")
    return WorkOwnershipLease(
        project_id,
        work_id,
        str(row[0]),
        _strict_fence(row[1]),
        issued_at,
        expires_at,
    )


def _validate_observation_time(current: WorkOwnershipLease, now: datetime) -> None:
    if now < current.issued_at:
        raise WorkOwnershipError("work ownership clock precedes lease issuance")


def _assert_exact(
    current: WorkOwnershipLease | None,
    *,
    owner_id: str,
    fence: int,
    now: datetime,
) -> None:
    if current is None:
        raise WorkOwnershipError("stale work ownership authority")
    _validate_observation_time(current, now)
    if (
        current.owner_id != owner_id
        or current.fence != fence
        or current.expires_at <= now
    ):
        raise WorkOwnershipError("stale work ownership authority")


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_busy_or_locked(exc):
            raise WorkOwnershipError("work ownership authority is busy") from exc
        raise


def _commit_mutation(connection: sqlite3.Connection) -> None:
    try:
        connection.commit()
    except sqlite3.OperationalError as exc:
        if _is_busy_or_locked(exc):
            connection.rollback()
            raise WorkOwnershipError("work ownership authority is busy") from exc
        raise


def _is_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _identity(*values: str) -> None:
    if not values or any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in values
    ):
        raise WorkOwnershipError("work ownership identity must be canonical non-empty text")


def _strict_fence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkOwnershipError("work ownership fence must be a positive integer")
    return value


def _lease_seconds(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WorkOwnershipError("lease_seconds must be a positive integer")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WorkOwnershipError("work ownership time must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _aware(value).isoformat(timespec="microseconds")


def _expiry(instant: datetime, lease_seconds: int) -> datetime:
    try:
        expires_at = instant + timedelta(seconds=lease_seconds)
    except OverflowError as exc:
        raise WorkOwnershipError("lease duration exceeds supported time range") from exc
    if expires_at <= instant:
        raise WorkOwnershipError("lease expiry must follow issuance time")
    return expires_at


def _optional_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkOwnershipError("corrupt work ownership timestamp")
    try:
        return _aware(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise WorkOwnershipError("corrupt work ownership timestamp") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC)
