import asyncio
from collections.abc import Callable

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_worker_recovery import (
    ProductFactoryWorkerRecovery,
    WorkerRecoveryDisposition,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _coordinator() -> ProductFactoryCoordinator:
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="ui",
                repository_id="repo-1",
                paths=("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
            ProductComponent(
                component_id="docs",
                repository_id="repo-1",
                paths=("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _run(coroutine):
    return asyncio.run(coroutine)


def _record_success(coordinator, request):
    evidence = tuple(
        TestEvidence(command, 0, f"concurrent-pass-{index}")
        for index, command in enumerate(request.acceptance_commands)
    )
    envelope = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(job_id=request.work_id, test_evidence=evidence),
    )
    return coordinator.record_result(envelope)


class _DuckFailure:
    message = "duck failure"


class _DuckTestEvidence:
    command = ("python", "-m", "pytest", "tests/core")
    exit_code = 0
    output_digest = "duck-output"


class _BehavioralText(str):
    def strip(self, *args, **kwargs):
        del args, kwargs
        return "trusted"


class FakeRecoveryPort:
    def __init__(
        self,
        state: RecoveryState | None,
        *,
        failure: WorkerFailure | None = None,
        inspect_error: Exception | None = None,
        recover_error: Exception | None = None,
        component_id_override: str | None = None,
        envelope_override: object | None = None,
        mutate_recover_state: bool = False,
        inspect_hook: Callable[[], None] | None = None,
        recover_hook: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.failure = failure
        self.inspect_error = inspect_error
        self.recover_error = recover_error
        self.component_id_override = component_id_override
        self.envelope_override = envelope_override
        self.mutate_recover_state = mutate_recover_state
        self.inspect_hook = inspect_hook
        self.recover_hook = recover_hook
        self.inspected: list[str] = []
        self.recovered = []

    async def inspect(self, work_id: str) -> RecoveryState | None:
        self.inspected.append(work_id)
        if self.inspect_hook is not None:
            self.inspect_hook()
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.state

    async def recover(self, request, state):
        self.recovered.append((request, state))
        if self.recover_hook is not None:
            self.recover_hook()
        if self.mutate_recover_state:
            object.__setattr__(state, "phase", "")
        if self.recover_error is not None:
            raise self.recover_error
        if self.envelope_override is not None:
            return self.envelope_override
        result = CodingResult(
            job_id=request.work_id,
            test_evidence=()
            if self.failure is not None
            else (TestEvidence(("pytest",), 0, "tests-ok"),),
            recovery_state=state,
            failure=self.failure,
        )
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=self.component_id_override or request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=result,
        )


def test_restart_recovery_returns_running_component_to_independent_review() -> None:
    original = _coordinator()
    request = original.start("core")
    snapshot = original.snapshot()
    restored = _coordinator()
    restored.restore(snapshot)
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(state)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(restored, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.RECOVERED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert worker.inspected == [request.work_id]
    assert worker.recovered[0][0].work_id == request.work_id
    assert worker.recovered[0][1] == state
    ready = {item.component_id for item in restored.ready_requests()}
    assert "docs" in ready
    assert "ui" not in ready


def test_missing_worker_state_blocks_only_the_lost_component() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    worker = FakeRecoveryPort(None)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_MISSING_STATE
    assert outcome.record.state is WorkState.BLOCKED
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_inspect_failure_blocks_only_the_affected_component() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("running", "unused"),
        inspect_error=RuntimeError("secret worker detail"),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INSPECTION_FAILED
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state is None
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert "secret worker detail" not in (outcome.record.blocker or "")
    assert worker.recovered == []
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_inspect_cancellation_propagates_without_reclassifying_running_work() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("running", "unused"),
        inspect_error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    core = next(
        record for record in coordinator.snapshot().records if record.request.component_id == "core"
    )
    assert core.state is WorkState.RUNNING
    assert core.blocker is None
    assert worker.recovered == []


def test_recover_failure_blocks_only_the_affected_component_and_retains_state() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(
        state,
        recover_error=RuntimeError("secret recover detail"),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_RECOVERY_FAILED
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state == state
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert "secret recover detail" not in (outcome.record.blocker or "")
    assert len(worker.recovered) == 1
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_recover_failure_retains_private_state_when_worker_mutates_argument() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(
        state,
        recover_error=RuntimeError("worker failed after mutation"),
        mutate_recover_state=True,
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_RECOVERY_FAILED
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state == RecoveryState("interrupted", "resume-token")
    assert state == RecoveryState("interrupted", "resume-token")
    assert worker.recovered[0][1].phase == ""
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_invalid_recovery_evidence_blocks_only_the_affected_component() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(state, component_id_override="foreign-component")

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state == state
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert "foreign-component" not in (outcome.record.blocker or "")
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_malformed_inspection_state_is_contained_before_recover_call() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    worker = FakeRecoveryPort(object())  # type: ignore[arg-type]

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state is None
    assert worker.recovered == []
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_forged_recovery_state_contract_invariant_is_rejected_before_recover_call() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    object.__setattr__(state, "phase", "")
    worker = FakeRecoveryPort(state)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state is None
    assert worker.recovered == []
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_non_envelope_recovery_result_is_contained_without_coordinator_mutation() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(state, envelope_override=object())

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_envelope_with_non_coding_result_is_contained() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=object(),  # type: ignore[arg-type]
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_forged_outer_envelope_contract_invariant_is_rejected() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(
                TestEvidence(
                    ("python", "-m", "pytest", "tests/core"),
                    0,
                    "tests-ok",
                ),
            ),
            recovery_state=state,
        ),
    )
    object.__setattr__(malformed, "diff_digest", "forged")
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_duck_typed_failure_evidence_is_rejected_before_coordinator_persistence() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            recovery_state=state,
            failure=_DuckFailure(),  # type: ignore[arg-type]
        ),
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_duck_typed_test_evidence_is_rejected_before_coordinator_persistence() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(_DuckTestEvidence(),),  # type: ignore[arg-type]
            recovery_state=state,
        ),
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_behavioral_primitive_inside_canonical_failure_is_rejected() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    behavioral_failure = WorkerFailure(
        WorkerFailureKind.INTERNAL_ERROR,
        _BehavioralText(""),
    )
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            recovery_state=state,
            failure=behavioral_failure,
        ),
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_forged_nested_failure_contract_invariant_is_rejected() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    failure = WorkerFailure(WorkerFailureKind.INTERNAL_ERROR, "worker failed")
    object.__setattr__(failure, "message", "")
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            recovery_state=state,
            failure=failure,
        ),
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_nested_malformed_test_evidence_is_contained() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed_result = CodingResult(
        job_id=request.work_id,
        test_evidence=(object(),),  # type: ignore[arg-type]
        recovery_state=state,
    )
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=malformed_result,
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_nested_malformed_failure_evidence_is_contained() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    state = RecoveryState("interrupted", "resume-token")
    malformed_result = CodingResult(
        job_id=request.work_id,
        recovery_state=state,
        failure=object(),  # type: ignore[arg-type]
    )
    malformed = WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=malformed_result,
    )
    worker = FakeRecoveryPort(state, envelope_override=malformed)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.record.state is WorkState.BLOCKED
    assert outcome.record.result is None
    assert outcome.recovery_state == state
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_recovery_cannot_mutate_another_running_component_with_valid_foreign_evidence() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    docs_request = coordinator.start("docs")
    state = RecoveryState("interrupted", "resume-token")
    foreign_result = CodingResult(
        job_id=docs_request.work_id,
        test_evidence=(TestEvidence(("pytest",), 0, "docs-ok"),),
        recovery_state=state,
    )
    foreign_envelope = WorkerResultEnvelope(
        work_id=docs_request.work_id,
        component_id=docs_request.component_id,
        repository_id=docs_request.repository_id,
        base_sha=docs_request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=foreign_result,
    )
    worker = FakeRecoveryPort(state, envelope_override=foreign_envelope)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_INVALID_EVIDENCE
    assert outcome.component_id == "core"
    assert outcome.record.request.component_id == "core"
    assert outcome.record.state is WorkState.BLOCKED

    records = {
        record.request.component_id: record
        for record in coordinator.snapshot().records
    }
    assert records["core"].state is WorkState.BLOCKED
    assert records["core"].result is None
    assert records["docs"].state is WorkState.RUNNING
    assert records["docs"].result is None


def test_recovered_cancelled_result_preserves_typed_repair_evidence() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("cancelled", "resume-later")
    failure = WorkerFailure(WorkerFailureKind.CANCELLED, "cancelled before restart")
    worker = FakeRecoveryPort(state, failure=failure)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.record.state is WorkState.REPAIR_REQUIRED
    assert outcome.record.result is not None
    result = outcome.record.result.coding_result
    assert result.failure == failure
    assert result.recovery_state == state


def test_non_running_component_cannot_be_recovered_or_inspected() -> None:
    coordinator = _coordinator()
    worker = FakeRecoveryPort(RecoveryState("running", "token"))

    with pytest.raises(CoordinatorError, match="must be running"):
        _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "docs"))

    assert worker.inspected == []


def test_unknown_component_recovery_fails_closed_without_worker_call() -> None:
    coordinator = _coordinator()
    worker = FakeRecoveryPort(RecoveryState("running", "token"))

    with pytest.raises(CoordinatorError, match="unknown component"):
        _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "missing"))

    assert worker.inspected == []


def test_concurrent_completion_during_inspect_stops_before_recover_effect() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("interrupted", "resume-token"),
        inspect_hook=lambda: _record_success(coordinator, request),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.SUPERSEDED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert outcome.record.result is not None
    assert outcome.record.result.coding_result.test_evidence[0].output_digest.startswith(
        "concurrent-pass-"
    )
    assert worker.recovered == []


def test_concurrent_completion_during_failed_inspect_is_not_overwritten_by_block() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("interrupted", "resume-token"),
        inspect_error=RuntimeError("stale inspection failure"),
        inspect_hook=lambda: _record_success(coordinator, request),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.SUPERSEDED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert outcome.record.blocker is None
    assert outcome.record.result is not None
    assert worker.recovered == []


def test_concurrent_completion_during_failed_recover_is_not_overwritten_by_block() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("interrupted", "resume-token"),
        recover_error=RuntimeError("stale recovery failure"),
        recover_hook=lambda: _record_success(coordinator, request),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.SUPERSEDED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert outcome.record.blocker is None
    assert outcome.record.result is not None
    assert len(worker.recovered) == 1


def test_stale_recovery_result_after_concurrent_completion_is_ignored() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeRecoveryPort(
        RecoveryState("interrupted", "resume-token"),
        recover_hook=lambda: _record_success(coordinator, request),
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.SUPERSEDED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert outcome.record.result is not None
    digests = tuple(
        item.output_digest for item in outcome.record.result.coding_result.test_evidence
    )
    assert all(item.startswith("concurrent-pass-") for item in digests)
    assert len(worker.recovered) == 1


def test_new_running_attempt_supersedes_old_recovery_by_exact_work_id() -> None:
    coordinator = _coordinator()
    original = coordinator.start("core")
    replacement = []

    def advance_to_repair_attempt() -> None:
        failed = WorkerResultEnvelope(
            work_id=original.work_id,
            component_id=original.component_id,
            repository_id=original.repository_id,
            base_sha=original.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=CodingResult(
                job_id=original.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.INTERNAL_ERROR,
                    "first attempt failed",
                ),
            ),
        )
        coordinator.record_result(failed)
        prepared = coordinator.prepare_repair(
            "core",
            base_sha=SHA_B,
            reason="retry after restart race",
        )
        replacement.append(coordinator.start("core"))
        assert replacement[0] == prepared

    worker = FakeRecoveryPort(
        RecoveryState("interrupted", "old-attempt-token"),
        inspect_hook=advance_to_repair_attempt,
    )

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.SUPERSEDED
    assert outcome.record.state is WorkState.RUNNING
    assert outcome.record.request.work_id == replacement[0].work_id
    assert outcome.record.request.work_id != original.work_id
    assert outcome.record.request.attempt == 2
    assert worker.recovered == []
