from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from nika_core.interaction import (
    AmbiguousTargetError,
    ApplicationIdentity,
    ControlLocator,
    ControlNode,
    InteractionTarget,
    SemanticSnapshot,
    TargetNotFoundError,
    resolve_strict,
)


class ObservableResultStatus(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    UNCHANGED = "unchanged"
    UNCERTAIN = "uncertain"


class CompletionSemantics(StrEnum):
    OBSERVABLE_ONLY = "observable_only"
    EXTERNAL_EFFECT_CONFIRMED = "external_effect_confirmed"


@dataclass(frozen=True, slots=True)
class DeclaredObservableResult:
    locator: ControlLocator
    completion_semantics: CompletionSemantics


@dataclass(frozen=True, slots=True)
class ObservableResultEvidence:
    status: ObservableResultStatus
    invoked: bool
    observations: int
    state_changed: bool
    matched_node_id: str | None
    external_effect_complete: bool
    detail: str


class FixtureActionRejected(RuntimeError):
    pass


class FixtureActionOutcomeUnknown(RuntimeError):
    pass


class FixtureObservationUnknown(RuntimeError):
    pass


def _snapshot(
    *nodes: ControlNode,
    generation: int = 1,
    revision: int = 1,
) -> SemanticSnapshot:
    return SemanticSnapshot(
        target=InteractionTarget(application=ApplicationIdentity("fixture-browser.exe", 42, 100)),
        generation=generation,
        revision=revision,
        controls=tuple(nodes),
    )


def _snapshot_changed(before: SemanticSnapshot, after: SemanticSnapshot) -> bool:
    return (
        before.target != after.target
        or before.generation != after.generation
        or before.revision != after.revision
        or before.controls != after.controls
    )


def _verify_declared_observable_result(
    *,
    permitted: bool,
    invoke: Callable[[], None],
    observe: Callable[[], SemanticSnapshot],
    before: SemanticSnapshot,
    declared: DeclaredObservableResult,
    max_observations: int,
) -> ObservableResultEvidence:
    """QA oracle for the production-owned verified-result contract.

    This intentionally lives in tests while ``src/nika_core/interaction/**`` has another owner.
    It is an executable specification, not a second browser/runtime implementation.
    """
    if max_observations < 1:
        raise ValueError("max_observations must be positive")
    if not permitted:
        return ObservableResultEvidence(
            status=ObservableResultStatus.REJECTED,
            invoked=False,
            observations=0,
            state_changed=False,
            matched_node_id=None,
            external_effect_complete=False,
            detail="permission rejected before action invocation",
        )

    try:
        invoke()
    except FixtureActionRejected as exc:
        return ObservableResultEvidence(
            status=ObservableResultStatus.REJECTED,
            invoked=True,
            observations=0,
            state_changed=False,
            matched_node_id=None,
            external_effect_complete=False,
            detail=str(exc),
        )
    except FixtureActionOutcomeUnknown as exc:
        return ObservableResultEvidence(
            status=ObservableResultStatus.UNCERTAIN,
            invoked=True,
            observations=0,
            state_changed=False,
            matched_node_id=None,
            external_effect_complete=False,
            detail=f"action outcome could not be established: {exc}",
        )

    changed = False
    last_observation = 0
    for observation in range(1, max_observations + 1):
        last_observation = observation
        try:
            after = observe()
        except FixtureObservationUnknown as exc:
            return ObservableResultEvidence(
                status=ObservableResultStatus.UNCERTAIN,
                invoked=True,
                observations=observation,
                state_changed=changed,
                matched_node_id=None,
                external_effect_complete=False,
                detail=f"post-action observation failed: {exc}",
            )

        changed = changed or _snapshot_changed(before, after)
        try:
            matched = resolve_strict(after, declared.locator)
        except TargetNotFoundError:
            continue
        except AmbiguousTargetError as exc:
            return ObservableResultEvidence(
                status=ObservableResultStatus.UNCERTAIN,
                invoked=True,
                observations=observation,
                state_changed=changed,
                matched_node_id=None,
                external_effect_complete=False,
                detail=f"declared result is ambiguous: {exc}",
            )

        return ObservableResultEvidence(
            status=ObservableResultStatus.SUCCESS,
            invoked=True,
            observations=observation,
            state_changed=changed,
            matched_node_id=matched.node_id,
            external_effect_complete=(
                declared.completion_semantics is CompletionSemantics.EXTERNAL_EFFECT_CONFIRMED
            ),
            detail="declared observable result matched",
        )

    status = ObservableResultStatus.TIMEOUT if changed else ObservableResultStatus.UNCHANGED
    return ObservableResultEvidence(
        status=status,
        invoked=True,
        observations=last_observation,
        state_changed=changed,
        matched_node_id=None,
        external_effect_complete=False,
        detail=(
            "observable state changed but declared result did not arrive before the bound"
            if changed
            else "observable state remained unchanged through the bounded verification window"
        ),
    )


@dataclass(slots=True)
class ControlledBrowserFixture:
    observations: list[SemanticSnapshot | FixtureObservationUnknown]
    rejected: bool = False
    uncertain_action: bool = False
    invoke_calls: int = 0
    observe_calls: int = 0

    def invoke(self) -> None:
        self.invoke_calls += 1
        if self.rejected:
            raise FixtureActionRejected("fixture action rejected")
        if self.uncertain_action:
            raise FixtureActionOutcomeUnknown("fixture transport lost after dispatch")

    def observe(self) -> SemanticSnapshot:
        self.observe_calls += 1
        if not self.observations:
            raise FixtureObservationUnknown("fixture observation sequence exhausted")
        item = self.observations.pop(0)
        if isinstance(item, FixtureObservationUnknown):
            raise item
        return item


def _button() -> ControlNode:
    return ControlNode("submit", "button", "Submit")


def _success_status(node_id: str = "result") -> ControlNode:
    return ControlNode(node_id, "status", "Saved")


def _declared(
    completion_semantics: CompletionSemantics = CompletionSemantics.EXTERNAL_EFFECT_CONFIRMED,
) -> DeclaredObservableResult:
    return DeclaredObservableResult(
        locator=ControlLocator(role="status", name="Saved"),
        completion_semantics=completion_semantics,
    )


def test_permitted_action_waits_for_declared_explicit_success_state() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(
        observations=[
            _snapshot(_button(), ControlNode("busy", "status", "Working"), revision=2),
            _snapshot(_button(), _success_status(), revision=3),
        ]
    )

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=3,
    )

    assert evidence.status is ObservableResultStatus.SUCCESS
    assert evidence.invoked is True
    assert evidence.observations == 2
    assert evidence.matched_node_id == "result"
    assert evidence.external_effect_complete is True
    assert fixture.invoke_calls == 1


def test_generic_dom_change_is_timeout_not_declared_success() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(
        observations=[
            _snapshot(_button(), ControlNode("busy", "status", "Working"), revision=2),
            _snapshot(_button(), ControlNode("busy", "status", "Still working"), revision=3),
        ]
    )

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=2,
    )

    assert evidence.status is ObservableResultStatus.TIMEOUT
    assert evidence.state_changed is True
    assert evidence.external_effect_complete is False


def test_unchanged_state_is_distinct_from_timeout_after_change() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(observations=[before, before])

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=2,
    )

    assert evidence.status is ObservableResultStatus.UNCHANGED
    assert evidence.state_changed is False
    assert evidence.external_effect_complete is False


def test_permission_rejection_prevents_invocation() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(observations=[before])

    evidence = _verify_declared_observable_result(
        permitted=False,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=1,
    )

    assert evidence.status is ObservableResultStatus.REJECTED
    assert evidence.invoked is False
    assert evidence.external_effect_complete is False
    assert fixture.invoke_calls == 0
    assert fixture.observe_calls == 0


def test_explicit_action_rejection_is_not_completion_or_uncertainty() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(observations=[before], rejected=True)

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=1,
    )

    assert evidence.status is ObservableResultStatus.REJECTED
    assert evidence.invoked is True
    assert evidence.external_effect_complete is False
    assert fixture.observe_calls == 0


def test_action_transport_loss_preserves_uncertainty() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(observations=[before], uncertain_action=True)

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=1,
    )

    assert evidence.status is ObservableResultStatus.UNCERTAIN
    assert evidence.invoked is True
    assert evidence.external_effect_complete is False
    assert "action outcome could not be established" in evidence.detail


def test_post_action_observation_failure_preserves_uncertainty() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(
        observations=[FixtureObservationUnknown("page/session became stale")]
    )

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=2,
    )

    assert evidence.status is ObservableResultStatus.UNCERTAIN
    assert evidence.invoked is True
    assert evidence.external_effect_complete is False
    assert "post-action observation failed" in evidence.detail


def test_observable_success_without_completion_grade_does_not_complete_external_effect() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(observations=[_snapshot(_button(), _success_status(), revision=2)])

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(CompletionSemantics.OBSERVABLE_ONLY),
        max_observations=1,
    )

    assert evidence.status is ObservableResultStatus.SUCCESS
    assert evidence.matched_node_id == "result"
    assert evidence.external_effect_complete is False


def test_ambiguous_declared_success_state_preserves_uncertainty() -> None:
    before = _snapshot(_button())
    fixture = ControlledBrowserFixture(
        observations=[
            _snapshot(
                _button(),
                _success_status("result-a"),
                _success_status("result-b"),
                revision=2,
            )
        ]
    )

    evidence = _verify_declared_observable_result(
        permitted=True,
        invoke=fixture.invoke,
        observe=fixture.observe,
        before=before,
        declared=_declared(),
        max_observations=1,
    )

    assert evidence.status is ObservableResultStatus.UNCERTAIN
    assert evidence.external_effect_complete is False
    assert "ambiguous" in evidence.detail
