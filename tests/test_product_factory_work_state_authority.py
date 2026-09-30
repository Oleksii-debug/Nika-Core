from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    trusted_plan_fingerprint,
)
from nika_core.toolsmith.contracts import CodingResult
from tests.test_product_factory_coordinator import DIGEST, PERMISSIONS, SHA_A, SHA_B
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph, _success


@pytest.mark.parametrize("state", ("planned", "ready", "accepted", "done", "cancelled"))
def test_work_record_state_requires_exact_enum(state: str) -> None:
    record = _core_record(_coordinator())

    with pytest.raises(CoordinatorError, match="work state must be an exact WorkState"):
        replace(record, state=state)


def _forge_work_record_state(record: WorkRecord, state: object) -> WorkRecord:
    forged = object.__new__(WorkRecord)
    for field_name in ("request", "state", "result", "review", "blocker"):
        object.__setattr__(
            forged,
            field_name,
            state if field_name == "state" else getattr(record, field_name),
        )
    return forged


@pytest.mark.parametrize("state", ("ready", "accepted", "done"))
def test_restore_rejects_forged_strenum_equal_work_state(state: str) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    records = tuple(
        _forge_work_record_state(record, state)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="snapshot work state must be an exact WorkState",
    ):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def _plan(graph, *, permission_ceiling=PERMISSIONS) -> None:
    ProductFactoryCoordinator(graph).plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui"},
        permission_ceiling=permission_ceiling,
    )


def test_plan_rejects_mutable_permission_ceiling_alias() -> None:
    with pytest.raises(
        CoordinatorError,
        match="permission ceiling must be a non-empty exact frozenset",
    ):
        _plan(_graph(), permission_ceiling=set(PERMISSIONS))


def test_plan_rejects_mutable_allowed_paths_alias() -> None:
    graph = _graph()
    components = tuple(
        replace(component, paths=list(component.paths))
        if component.component_id == "core"
        else component
        for component in graph.components
    )
    graph = replace(graph, components=components)

    with pytest.raises(
        CoordinatorError,
        match="allowed paths must be a non-empty exact tuple",
    ):
        _plan(graph)


def test_plan_rejects_mutable_acceptance_command_alias() -> None:
    graph = _graph()
    components = tuple(
        replace(component, test_commands=(["pytest", "tests/core"],))
        if component.component_id == "core"
        else component
        for component in graph.components
    )
    graph = replace(graph, components=components)

    with pytest.raises(
        CoordinatorError,
        match="acceptance commands must be an exact tuple of non-empty argv tuples",
    ):
        _plan(graph)


def _forge_request_authority(
    request: ComponentWorkRequest,
    field_name: str,
    value: object,
) -> ComponentWorkRequest:
    forged = object.__new__(ComponentWorkRequest)
    for request_field in (
        "work_id",
        "project_id",
        "component_id",
        "repository_id",
        "goal",
        "base_sha",
        "allowed_paths",
        "permission_ceiling",
        "acceptance_commands",
        "attempt",
    ):
        object.__setattr__(
            forged,
            request_field,
            value if request_field == field_name else getattr(request, request_field),
        )
    return forged


def _mutable_authority_value(request: ComponentWorkRequest, field_name: str) -> object:
    if field_name == "allowed_paths":
        return list(request.allowed_paths)
    if field_name == "permission_ceiling":
        return set(request.permission_ceiling)
    if field_name == "acceptance_commands":
        return (["pytest", "tests/core"],)
    raise AssertionError(f"unknown request authority field: {field_name}")


@pytest.mark.parametrize(
    ("field_name", "error_pattern"),
    (
        ("allowed_paths", "allowed paths must be a non-empty exact tuple"),
        ("permission_ceiling", "permission ceiling must be a non-empty exact frozenset"),
        (
            "acceptance_commands",
            "acceptance commands must be an exact tuple of non-empty argv tuples",
        ),
    ),
)
def test_restore_rejects_mutable_authority_in_trusted_plan(
    field_name: str,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    assert snapshot.trusted_plan is not None
    tampered_plan = tuple(
        _forge_request_authority(
            request,
            field_name,
            _mutable_authority_value(request, field_name),
        )
        if request.component_id == "core"
        else request
        for request in snapshot.trusted_plan
    )
    tampered = replace(snapshot, trusted_plan=tampered_plan)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match=error_pattern):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


@pytest.mark.parametrize(
    ("field_name", "error_pattern"),
    (
        ("allowed_paths", "allowed paths must be a non-empty exact tuple"),
        ("permission_ceiling", "permission ceiling must be a non-empty exact frozenset"),
        (
            "acceptance_commands",
            "acceptance commands must be an exact tuple of non-empty argv tuples",
        ),
    ),
)
def test_restore_rejects_mutable_authority_in_work_record(
    field_name: str,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(
            record,
            request=_forge_request_authority(
                record.request,
                field_name,
                _mutable_authority_value(record.request, field_name),
            ),
        )
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match=error_pattern):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )

class _HostilePlan(tuple):
    def __bool__(self):  # pragma: no cover - must never execute
        raise AssertionError("hostile plan truthiness executed")


class _HostileStr(str):
    def __bool__(self):  # pragma: no cover - must never execute
        raise AssertionError("hostile truthiness executed")

    def strip(self, *args, **kwargs):  # pragma: no cover - must never execute
        raise AssertionError("hostile strip executed")

    def casefold(self):  # pragma: no cover - must never execute
        raise AssertionError("hostile casefold executed")

    def __hash__(self):  # pragma: no cover - must never execute
        raise AssertionError("hostile hash executed")

    def __eq__(self, other):  # pragma: no cover - must never execute
        raise AssertionError("hostile equality executed")


class _HostileInt(int):
    def __lt__(self, other):  # pragma: no cover - must never execute
        raise AssertionError("hostile comparison executed")


@pytest.mark.parametrize(
    ("field_name", "value", "error_pattern"),
    (
        ("work_id", _HostileStr("work-forged"), "identity and goal must be exact strings"),
        ("project_id", _HostileStr("project-1"), "identity and goal must be exact strings"),
        ("component_id", _HostileStr("core"), "identity and goal must be exact strings"),
        ("repository_id", _HostileStr("repo-1"), "identity and goal must be exact strings"),
        ("goal", _HostileStr("build core"), "identity and goal must be exact strings"),
        ("base_sha", _HostileStr(SHA_A), "base_sha must be a 40-character"),
        ("attempt", _HostileInt(1), "attempt must be positive"),
    ),
)
def test_work_request_rejects_polymorphic_scalar_authority(
    field_name: str,
    value: object,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    request = coordinator.snapshot().trusted_plan[0]

    with pytest.raises(CoordinatorError, match=error_pattern):
        _forge_request_authority(request, field_name, value).__post_init__()


@pytest.mark.parametrize(
    ("field_name", "value", "error_pattern"),
    (
        ("project_id", _HostileStr("project-1"), "identity and goal must be exact strings"),
        ("component_id", _HostileStr("core"), "identity and goal must be exact strings"),
        ("base_sha", _HostileStr(SHA_A), "base_sha must be a 40-character"),
        ("attempt", _HostileInt(1), "attempt must be positive"),
    ),
)
def test_restore_rejects_forged_request_scalar_before_identity_operations(
    field_name: str,
    value: object,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    assert snapshot.trusted_plan is not None
    forged_plan = tuple(
        _forge_request_authority(request, field_name, value)
        if request.component_id == "core"
        else request
        for request in snapshot.trusted_plan
    )
    tampered = replace(snapshot, trusted_plan=forged_plan)

    with pytest.raises(CoordinatorError, match=error_pattern):
        ProductFactoryCoordinator(_graph()).restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def _forge_worker_envelope(
    envelope: WorkerResultEnvelope,
    field_name: str,
    value: object,
) -> WorkerResultEnvelope:
    forged = object.__new__(WorkerResultEnvelope)
    for envelope_field in (
        "work_id",
        "component_id",
        "repository_id",
        "base_sha",
        "result_sha",
        "diff_digest",
        "coding_result",
    ):
        object.__setattr__(
            forged,
            envelope_field,
            value if envelope_field == field_name else getattr(envelope, envelope_field),
        )
    return forged


@pytest.mark.parametrize(
    ("field_name", "value", "error_pattern"),
    (
        ("work_id", _HostileStr("work-forged"), "worker result identity must be exact strings"),
        ("component_id", _HostileStr("core"), "worker result identity must be exact strings"),
        ("repository_id", _HostileStr("repo-1"), "worker result identity must be exact strings"),
        ("base_sha", _HostileStr(SHA_A), "base_sha must be a 40-character"),
        ("result_sha", _HostileStr(SHA_B), "result_sha must be a 40-character"),
        ("diff_digest", _HostileStr(DIGEST), "diff_digest must be a 64-character"),
    ),
)
def test_record_result_rejects_forged_scalar_before_lookup_or_equality(
    field_name: str,
    value: object,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    envelope = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(job_id=request.work_id),
    )
    forged = _forge_worker_envelope(envelope, field_name, value)

    with pytest.raises(CoordinatorError, match=error_pattern):
        coordinator.record_result(forged)


def test_worker_result_rejects_polymorphic_coding_job_identity() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")

    with pytest.raises(CoordinatorError, match="coding result job id must be an exact"):
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=CodingResult(job_id=_HostileStr(request.work_id)),
        )


def test_plan_rejects_polymorphic_goal_before_strip() -> None:
    coordinator = ProductFactoryCoordinator(_graph())

    with pytest.raises(CoordinatorError, match="component goal must be an exact string"):
        coordinator.plan(
            base_shas={"repo-1": SHA_A},
            goals={
                "core": _HostileStr("build core"),
                "ui": "build ui",
            },
            permission_ceiling=PERMISSIONS,
        )


def test_plan_rejects_polymorphic_base_sha_before_casefold() -> None:
    coordinator = ProductFactoryCoordinator(_graph())

    with pytest.raises(CoordinatorError, match="base_sha must be a 40-character"):
        coordinator.plan(
            base_shas={"repo-1": _HostileStr(SHA_A)},
            goals={"core": "build core", "ui": "build ui"},
            permission_ceiling=PERMISSIONS,
        )


def test_restore_rejects_behavioral_repair_blocker_before_truthiness() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    coordinator.review(
        "core",
        ReviewDecision("qa-1", False, "needs repair", ("ci:review-1",)),
    )
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(record, blocker=_HostileStr(record.blocker))
        if record.request.component_id == "core" and record.blocker is not None
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="repair blocker must be canonical single-line text",
    ):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
    assert restored.revision == 0

def test_restore_rejects_behavioral_snapshot_revision_before_comparison() -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    tampered = replace(snapshot, revision=_HostileInt(snapshot.revision))

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="snapshot revision must be an exact non-negative"):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
    assert restored.revision == 0


def test_restore_rejects_mutable_snapshot_records_alias() -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    tampered = replace(snapshot, records=list(snapshot.records))

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="snapshot records must be an exact tuple"):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
    assert restored.revision == 0


def test_restore_rejects_mutable_snapshot_trusted_plan_alias() -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    assert snapshot.trusted_plan is not None
    tampered = replace(snapshot, trusted_plan=list(snapshot.trusted_plan))

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="snapshot trusted plan must be an exact tuple"):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
    assert restored.revision == 0


def test_validate_snapshot_rejects_coordinator_snapshot_subclass() -> None:
    class SnapshotSubclass(CoordinatorSnapshot):
        pass

    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    subclass = SnapshotSubclass(
        snapshot.project_id,
        snapshot.revision,
        snapshot.records,
        snapshot.trusted_plan,
    )

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="snapshot must be an exact CoordinatorSnapshot"):
        restored.restore(
            subclass,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
    assert restored.revision == 0


def test_review_revalidates_forged_decision_before_state_mutation() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    before = coordinator.snapshot()

    forged = object.__new__(ReviewDecision)
    object.__setattr__(forged, "reviewer_id", "qa-1")
    object.__setattr__(forged, "accepted", 1)
    object.__setattr__(forged, "reason", "verified")
    object.__setattr__(forged, "evidence_refs", ("ci:1",))

    with pytest.raises(CoordinatorError, match="review acceptance must be an exact boolean"):
        coordinator.review("core", forged)
    assert coordinator.snapshot() == before



def test_trusted_plan_fingerprint_rejects_behavioral_container_before_truthiness() -> None:
    coordinator = _coordinator()
    plan = coordinator.snapshot().trusted_plan
    assert plan is not None
    hostile = _HostilePlan(plan)

    with pytest.raises(CoordinatorError, match="trusted plan descriptor must be an exact tuple"):
        trusted_plan_fingerprint(hostile)


def test_restore_validates_explicit_fingerprint_before_truthiness() -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    hostile = _HostileStr(coordinator.trusted_plan_fingerprint)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="trusted_plan_fingerprint must be a 64-character hexadecimal digest",
    ):
        restored.restore(snapshot, trusted_plan_fingerprint=hostile)
    assert restored.revision == 0
