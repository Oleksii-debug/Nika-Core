"""Current-main composition for the V0.1 script-class browser Scenario B.

This module deliberately composes existing Nika authorities instead of creating another browser,
scheduler, retry, approval, or effect system. BatchCursor owns durable workflow progress,
BoundedBatchExecutor owns max-concurrency admission, TaskBrowserTabs owns logical tab identity,
PlaywrightInteractionAdapter owns semantic DOM safety, and ToolExecutor/ToolEffectGuard remains
the canonical external-effect authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from nika_core.batch_cursor import (
    AttemptState,
    BatchCursor,
    BatchCursorBlockedError,
    IntentKind,
    TargetCursor,
)
from nika_core.batch_execution import (
    BatchExecutionReport,
    BatchStopReason,
    BatchTargetState,
    BoundedBatchExecutor,
)
from nika_core.batch_report import (
    TargetReportFacts,
    TargetReportItem,
    TargetReportStatus,
    project_batch_report,
)
from nika_core.interaction import (
    AmbiguousTargetError,
    BrowserSession,
    ControlLocator,
    InteractionAction,
    PlaywrightInteractionAdapter,
    SemanticSnapshot,
    TargetNotFoundError,
    resolve_strict,
    validate_snapshot,
)
from nika_core.memory import MemoryScope, MemoryService
from nika_core.page_readiness import (
    PageObservationSignal,
    PageReadinessResult,
    PageReadinessState,
    classify_page_readiness,
    observe_page_readiness,
)
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.task_browser_tabs import (
    TaskBrowserTabError,
    TaskBrowserTabs,
    TaskTabReopenPolicy,
)
from nika_core.tools import ToolCall, ToolExecutor, ToolResult, ToolRisk, ToolSpec

_INPUT_TOOL_ID = "v01.scenario_b.semantic_set_value"
_INVOKE_TOOL_ID = "v01.scenario_b.semantic_invoke"
_TABS_MEMORY_NAMESPACE = "v01.scenario_b.task_tabs"
_UNKNOWN_EFFECT_ERRORS = frozenset(
    {
        "tool failed",
        "tool timed out",
        "tool result durability failed",
    }
)


class ScenarioBCompositionError(RuntimeError):
    """Base error for one composed Scenario-B target attempt."""


class ScenarioBReadinessError(ScenarioBCompositionError):
    """A semantic target did not become safely usable inside the bounded window."""


class ScenarioBAuthorityError(ScenarioBCompositionError):
    """Canonical ToolExecutor authority denied or could not safely complete the action."""


class ScenarioBObservableResultError(ScenarioBCompositionError):
    """The workflow-declared postcondition was absent, stale, ambiguous, or pre-existing."""


@dataclass(frozen=True, slots=True)
class ScenarioBTarget:
    """One declared browser target in the generic V0.1 script-class workflow."""

    target_id: str
    url: str
    action_locator: ControlLocator
    success_locator: ControlLocator
    input_locator: ControlLocator | None = None
    input_value: str | None = None
    reopen_policy: TaskTabReopenPolicy = TaskTabReopenPolicy.SAME_TARGET
    readiness_timeout_seconds: float = 10.0
    poll_interval_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.target_id.strip() or not self.url.strip():
            raise ValueError("Scenario-B target identity and URL must not be empty")
        if (self.input_locator is None) != (self.input_value is None):
            raise ValueError(
                "input locator and input value must either both be set or both be absent"
            )
        if (
            isinstance(self.readiness_timeout_seconds, bool)
            or isinstance(self.poll_interval_seconds, bool)
            or self.readiness_timeout_seconds <= 0
            or self.poll_interval_seconds <= 0
        ):
            raise ValueError("Scenario-B readiness windows must be positive")


@dataclass(frozen=True, slots=True)
class ScenarioBBatchResult:
    """One admitted batch attempt plus the canonical read-only per-target projection."""

    execution: BatchExecutionReport | None
    report: tuple[TargetReportItem, ...]
    waiting_until: str | None


ReadinessSignalProvider = Callable[[ScenarioBTarget, SemanticSnapshot], PageObservationSignal]


@dataclass(slots=True)
class ScenarioBSemanticTools:
    """Semantic handlers registered behind the canonical ToolExecutor."""

    tabs: TaskBrowserTabs
    readiness_signal_provider: ReadinessSignalProvider | None = None

    def adapter_for(self, *, task_id: str, tab_id: str) -> PlaywrightInteractionAdapter:
        page_id = self.tabs.runtime_page_id(task_id=task_id, tab_id=tab_id)
        return PlaywrightInteractionAdapter(session=self.tabs.session, page_id=page_id)

    def readiness(
        self,
        target: ScenarioBTarget,
        *,
        task_id: str,
        tab_id: str,
        locator: ControlLocator,
    ) -> PageReadinessResult:
        adapter = self.adapter_for(task_id=task_id, tab_id=tab_id)
        snapshot = adapter.observe()
        signal = (
            self.readiness_signal_provider(target, snapshot)
            if self.readiness_signal_provider is not None
            else PageObservationSignal.ACTIVE
        )
        if not isinstance(signal, PageObservationSignal):
            return PageReadinessResult(
                state=PageReadinessState.VALIDATION_ERROR,
                reason="readiness signal provider returned invalid type",
            )
        return classify_page_readiness(snapshot=snapshot, locator=locator, signal=signal)

    async def set_value(self, arguments: dict[str, object]) -> object:
        task_id, tab_id, target_id = _required_action_identity(arguments)
        locator = _locator_from_payload(arguments.get("input_locator"))
        value = arguments.get("value")
        if not isinstance(value, str):
            raise TypeError("Scenario-B SET_VALUE requires string value")
        adapter = self.adapter_for(task_id=task_id, tab_id=tab_id)
        before = adapter.observe()
        node = resolve_strict(before, locator)
        current = adapter.observe()
        validate_snapshot(before, current)
        current_node = resolve_strict(current, locator)
        if current_node.node_id != node.node_id:
            raise ScenarioBObservableResultError(
                "semantic input identity changed before edit"
            )
        adapter.focus(current_node)
        adapter.act(current_node, InteractionAction.SET_VALUE, value)
        after = adapter.observe()
        if not adapter.verify(
            current,
            after,
            current_node,
            InteractionAction.SET_VALUE,
            value,
        ):
            raise ScenarioBObservableResultError(
                "semantic text entry postcondition was not proven"
            )
        return {"target_id": target_id, "verified": True}

    async def invoke(self, arguments: dict[str, object]) -> object:
        task_id, tab_id, target_id = _required_action_identity(arguments)
        action_locator = _locator_from_payload(arguments.get("action_locator"))
        success_locator = _locator_from_payload(arguments.get("success_locator"))
        adapter = self.adapter_for(task_id=task_id, tab_id=tab_id)

        before = adapter.observe()
        _require_success_absent(before, success_locator)
        node = resolve_strict(before, action_locator)

        current = adapter.observe()
        validate_snapshot(before, current)
        current_node = resolve_strict(current, action_locator)
        if current_node.node_id != node.node_id:
            raise ScenarioBObservableResultError(
                "semantic action identity changed before invoke"
            )

        adapter.focus(current_node)
        adapter.act(current_node, InteractionAction.INVOKE, None)
        after = adapter.observe()
        _require_same_logical_page(current, after)
        resolve_strict(after, success_locator)

        evidence_ref = _result_reference(task_id=task_id, target_id=target_id)
        return {
            "target_id": target_id,
            "verified": True,
            "evidence_ref": evidence_ref,
        }


def register_scenario_b_semantic_tools(
    executor: ToolExecutor,
    semantic_tools: ScenarioBSemanticTools,
) -> None:
    """Register the two V0.1 semantic actions with canonical risks.

    Existing registrations are never replaced. A caller therefore cannot downgrade the external
    INVOKE path by supplying its own risk metadata.
    """

    existing = {spec.tool_id: spec for spec in executor.specs()}
    expected = {
        _INPUT_TOOL_ID: ToolRisk.LOCAL_WRITE,
        _INVOKE_TOOL_ID: ToolRisk.EXTERNAL_SIDE_EFFECT,
    }
    for tool_id, risk in expected.items():
        if tool_id in existing and existing[tool_id].risk is not risk:
            raise ScenarioBAuthorityError(
                "existing Scenario-B ToolSpec has incompatible risk"
            )

    if _INPUT_TOOL_ID not in existing:
        executor.register(
            ToolSpec(
                tool_id=_INPUT_TOOL_ID,
                description="Set declared text in one task-owned semantic browser control",
                risk=ToolRisk.LOCAL_WRITE,
                timeout_seconds=15.0,
            ),
            semantic_tools.set_value,
        )
    if _INVOKE_TOOL_ID not in existing:
        executor.register(
            ToolSpec(
                tool_id=_INVOKE_TOOL_ID,
                description=(
                    "Invoke one declared task-owned semantic action and require declared "
                    "observable completion evidence"
                ),
                risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
                timeout_seconds=30.0,
            ),
            semantic_tools.invoke,
        )


@dataclass(slots=True)
class ScenarioBService:
    """Run exactly the durable batch that BatchCursor currently admits."""

    task_id: str
    cursor: BatchCursor
    tabs: TaskBrowserTabs
    executor: BoundedBatchExecutor[ScenarioBTarget]
    tool_executor: ToolExecutor
    idempotency: IdempotencyLedger
    memory: MemoryService
    targets: Sequence[ScenarioBTarget]
    semantic_tools: ScenarioBSemanticTools
    inter_batch_delay_seconds: float = 60.0
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    cancellation_event: asyncio.Event | None = None
    _facts: dict[tuple[str, int], TargetReportFacts] = field(
        default_factory=dict,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("Scenario-B task_id must not be empty")
        if self.inter_batch_delay_seconds < 0:
            raise ValueError("inter-batch delay must not be negative")
        if self.cursor.state.task_id != self.task_id:
            raise ValueError("Scenario-B cursor belongs to a different task")
        self._restore_or_validate_tabs()
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Scenario-B target identities must be unique")
        cursor_ids = [target.target_id for target in self.cursor.state.targets]
        if cursor_ids != target_ids:
            raise ValueError(
                "Scenario-B declared target order must match durable cursor plan"
            )
        register_scenario_b_semantic_tools(self.tool_executor, self.semantic_tools)
        _require_registered_risk(
            self.tool_executor,
            _INPUT_TOOL_ID,
            ToolRisk.LOCAL_WRITE,
        )
        _require_registered_risk(
            self.tool_executor,
            _INVOKE_TOOL_ID,
            ToolRisk.EXTERNAL_SIDE_EFFECT,
        )

    async def run_ready_batch(self) -> ScenarioBBatchResult:
        state = self.cursor.state
        if (
            state.next_scheduled_intent is not None
            and state.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT
        ):
            return ScenarioBBatchResult(
                execution=None,
                report=self._project_report(),
                waiting_until=state.next_scheduled_intent.not_before,
            )
        if state.uncertain_count:
            raise BatchCursorBlockedError(
                "Scenario-B is blocked by uncertain external effect"
            )

        by_id = {target.target_id: target for target in self.targets}
        ready = tuple(
            by_id[target.target_id]
            for target in state.targets
            if target.batch_index == state.ready_batch_index
            and target.attempt_state
            not in {AttemptState.CONFIRMED, AttemptState.FAILED}
        )
        if not ready:
            return ScenarioBBatchResult(
                execution=BatchExecutionReport(
                    stop_reason=BatchStopReason.COMPLETED,
                    results=(),
                    peak_in_flight=0,
                ),
                report=self._project_report(),
                waiting_until=None,
            )

        execution = await self.executor.run(
            ready,
            identify=lambda target: target.target_id,
            execute=self._execute_target,
        )
        self._capture_batch_control_facts(execution)
        current = self.cursor.state
        wait = (
            current.next_scheduled_intent.not_before
            if current.next_scheduled_intent is not None
            and current.next_scheduled_intent.kind is IntentKind.INTER_BATCH_WAIT
            else None
        )
        return ScenarioBBatchResult(
            execution=execution,
            report=self._project_report(),
            waiting_until=wait,
        )

    def release_inter_batch_wait(self, *, now: datetime | None = None) -> None:
        self.cursor.release_inter_batch_wait(now=now)

    async def _execute_target(self, target: ScenarioBTarget) -> None:
        try:
            tab_id = self._ensure_tab(target)
            self._set_open_fact(target, opened=True)

            if target.input_locator is not None:
                await self._require_ready(
                    target,
                    tab_id=tab_id,
                    locator=target.input_locator,
                )
                assert target.input_value is not None
                set_result = await self.tool_executor.execute(
                    ToolCall(
                        call_id=_phase_call_id(
                            self.task_id,
                            self.cursor.state.cursor_id,
                            target.target_id,
                            "input",
                        ),
                        tool_id=_INPUT_TOOL_ID,
                        arguments={
                            "task_id": self.task_id,
                            "tab_id": tab_id,
                            "target_id": target.target_id,
                            "input_locator": _locator_to_payload(target.input_locator),
                            "value": target.input_value,
                        },
                        task_id=self.task_id,
                    )
                )
                if not set_result.ok:
                    self._set_reason_fact(target, "semantic_input_failed")
                    raise ScenarioBAuthorityError(
                        set_result.error or "semantic input failed"
                    )

            await self._require_ready(
                target,
                tab_id=tab_id,
                locator=target.action_locator,
            )
        except TaskBrowserTabError:
            self._set_reason_fact(target, "tab_navigation_failed")
            due = self.clock() + timedelta(seconds=self.inter_batch_delay_seconds)
            self.cursor.mark_terminal_failure(
                target.target_id,
                next_batch_not_before=due,
            )
            raise
        except (ScenarioBReadinessError, ScenarioBAuthorityError):
            due = self.clock() + timedelta(seconds=self.inter_batch_delay_seconds)
            self.cursor.mark_terminal_failure(
                target.target_id,
                next_batch_not_before=due,
            )
            raise

        grant = self.cursor.prepare_external_effect(target.target_id)
        if not grant.execute:
            return

        result = await self.tool_executor.execute(
            ToolCall(
                call_id=grant.operation_key,
                tool_id=_INVOKE_TOOL_ID,
                arguments={
                    "task_id": self.task_id,
                    "tab_id": tab_id,
                    "target_id": target.target_id,
                    "action_locator": _locator_to_payload(target.action_locator),
                    "success_locator": _locator_to_payload(target.success_locator),
                },
                task_id=self.task_id,
            )
        )
        if not result.ok:
            error = result.error or "tool failed"
            self._set_reason_fact(target, _safe_reason_code(error))
            if error in _UNKNOWN_EFFECT_ERRORS:
                self.cursor.mark_external_uncertain(
                    target.target_id,
                    {"reason": "canonical_tool_effect_unresolved"},
                )
            raise ScenarioBAuthorityError(error)

        output = _verified_output(result, target_id=target.target_id)
        due = self.clock() + timedelta(seconds=self.inter_batch_delay_seconds)
        self.cursor.confirm_external_effect(
            target.target_id,
            dict(output),
            next_batch_not_before=due,
        )

    async def _require_ready(
        self,
        target: ScenarioBTarget,
        *,
        tab_id: str,
        locator: ControlLocator,
    ) -> None:
        result = await observe_page_readiness(
            lambda: self.semantic_tools.readiness(
                target,
                task_id=self.task_id,
                tab_id=tab_id,
                locator=locator,
            ),
            timeout_seconds=target.readiness_timeout_seconds,
            poll_interval_seconds=target.poll_interval_seconds,
            cancellation_event=self.cancellation_event,
            sleeper=self.sleeper,
        )
        if result.state is not PageReadinessState.READY:
            self._set_reason_fact(
                target,
                f"readiness_{result.state.value}",
            )
            raise ScenarioBReadinessError(result.reason)

    def _ensure_tab(self, target: ScenarioBTarget) -> str:
        tab_id = _stable_tab_id(
            self.task_id,
            self.cursor.state.cursor_id,
            target.target_id,
        )
        existing = {tab.tab_id: tab for tab in self.tabs.owned_tabs(self.task_id)}
        if tab_id not in existing:
            self.tabs.open_tab(
                task_id=self.task_id,
                tab_id=tab_id,
                target_url=target.url,
                reopen_policy=target.reopen_policy,
            )
            self._persist_tabs()
            return tab_id
        self.tabs.switch_to(
            task_id=self.task_id,
            tab_id=tab_id,
            reopen_if_stale=True,
        )
        return tab_id

    def _restore_or_validate_tabs(self) -> None:
        record = self.memory.get(
            scope=MemoryScope.TASK,
            owner_id=self.task_id,
            namespace=_TABS_MEMORY_NAMESPACE,
            key=self.cursor.state.cursor_id,
        )
        current = tuple(tab.to_dict() for tab in self.tabs.owned_tabs(self.task_id))
        if record is None:
            self._persist_tabs()
            return

        restored = restore_scenario_b_tabs(
            self.memory,
            session=self.tabs.session,
            task_id=self.task_id,
            cursor_id=self.cursor.state.cursor_id,
        )
        durable = tuple(tab.to_dict() for tab in restored.owned_tabs(self.task_id))
        if current:
            if current != durable:
                raise ScenarioBCompositionError(
                    "runtime task-tab ownership conflicts with durable Scenario-B state"
                )
            return

        self.tabs = restored
        if isinstance(self.semantic_tools, ScenarioBSemanticTools):
            self.semantic_tools.tabs = restored

    def _persist_tabs(self) -> None:
        persist_scenario_b_tabs(
            self.memory,
            self.tabs,
            task_id=self.task_id,
            cursor_id=self.cursor.state.cursor_id,
        )

    def _set_open_fact(self, target: ScenarioBTarget, *, opened: bool) -> None:
        cursor_target = _cursor_target(self.cursor, target.target_id)
        for input_order in cursor_target.input_positions:
            self._facts[(target.target_id, input_order)] = TargetReportFacts(
                target_id=target.target_id,
                input_order=input_order,
                opened=opened,
                updated_at=self.clock().isoformat(),
            )

    def _set_reason_fact(self, target: ScenarioBTarget, reason: str) -> None:
        cursor_target = _cursor_target(self.cursor, target.target_id)
        for input_order in cursor_target.input_positions:
            current = self._facts.get((target.target_id, input_order))
            self._facts[(target.target_id, input_order)] = TargetReportFacts(
                target_id=target.target_id,
                input_order=input_order,
                opened=current.opened if current is not None else None,
                attempted=True,
                reason_code=reason,
                updated_at=self.clock().isoformat(),
            )

    def _capture_batch_control_facts(
        self,
        execution: BatchExecutionReport,
    ) -> None:
        if execution.stop_reason not in {
            BatchStopReason.CANCELLED,
            BatchStopReason.PAUSED,
            BatchStopReason.DEADLINE,
        }:
            return
        terminal = (
            TargetReportStatus.CANCELLED
            if execution.stop_reason is BatchStopReason.CANCELLED
            else None
        )
        for result in execution.results:
            if result.state is not BatchTargetState.NOT_STARTED:
                continue
            target = next(
                item for item in self.targets if item.target_id == result.target_id
            )
            cursor_target = _cursor_target(self.cursor, target.target_id)
            for input_order in cursor_target.input_positions:
                self._facts[(target.target_id, input_order)] = TargetReportFacts(
                    target_id=target.target_id,
                    input_order=input_order,
                    terminal_status=terminal,
                    reason_code=execution.stop_reason.value,
                    updated_at=self.clock().isoformat(),
                )

    def _project_report(self) -> tuple[TargetReportItem, ...]:
        state = self.cursor.state
        records = {
            target.operation_key: record
            for target in state.targets
            if (record := self.idempotency.get(target.operation_key)) is not None
        }
        return project_batch_report(
            state,
            batch_updated_at=self.clock(),
            effect_records=records,
            facts=tuple(self._facts.values()),
        )


def persist_scenario_b_tabs(
    memory: MemoryService,
    tabs: TaskBrowserTabs,
    *,
    task_id: str,
    cursor_id: str,
) -> None:
    """Persist only task-owned logical tab identity; never runtime browser handles."""

    snapshot = tabs.snapshot()
    payload = {
        "schema_version": snapshot["schema_version"],
        "tabs": [tab.to_dict() for tab in tabs.owned_tabs(task_id)],
    }
    memory.put(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace=_TABS_MEMORY_NAMESPACE,
        key=cursor_id,
        value=payload,
    )


def restore_scenario_b_tabs(
    memory: MemoryService,
    *,
    session: BrowserSession,
    task_id: str,
    cursor_id: str,
) -> TaskBrowserTabs:
    """Restore logical task-tab ownership with deliberately stale runtime bindings."""

    record = memory.get(
        scope=MemoryScope.TASK,
        owner_id=task_id,
        namespace=_TABS_MEMORY_NAMESPACE,
        key=cursor_id,
    )
    if record is None:
        return TaskBrowserTabs(session=session)

    restored = TaskBrowserTabs.from_snapshot(session=session, payload=record.value)
    raw = restored.snapshot().get("tabs")
    if not isinstance(raw, list) or any(
        not isinstance(item, dict) or item.get("task_id") != task_id
        for item in raw
    ):
        raise ScenarioBCompositionError(
            "durable Scenario-B tab state contains foreign task ownership"
        )
    return restored


def _required_action_identity(
    arguments: Mapping[str, object],
) -> tuple[str, str, str]:
    values = tuple(
        arguments.get(name) for name in ("task_id", "tab_id", "target_id")
    )
    if any(
        not isinstance(value, str) or not value.strip()
        for value in values
    ):
        raise TypeError("Scenario-B semantic action identity is malformed")
    return values[0], values[1], values[2]  # type: ignore[return-value]


def _locator_to_payload(locator: ControlLocator) -> dict[str, object]:
    return {
        "role": locator.role,
        "name": locator.name,
        "label": locator.label,
        "text": locator.text,
        "ancestor_node_id": locator.ancestor_node_id,
        "attributes": [list(item) for item in locator.attributes],
    }


def _locator_from_payload(value: object) -> ControlLocator:
    if not isinstance(value, Mapping):
        raise TypeError("semantic locator payload must be an object")
    expected = {
        "role",
        "name",
        "label",
        "text",
        "ancestor_node_id",
        "attributes",
    }
    if set(value) != expected:
        raise ValueError("semantic locator payload fields do not match contract")
    scalars: dict[str, str | None] = {}
    for name in ("role", "name", "label", "text", "ancestor_node_id"):
        item = value[name]
        if item is not None and not isinstance(item, str):
            raise TypeError("semantic locator scalar must be string or null")
        scalars[name] = item
    raw_attributes = value["attributes"]
    if not isinstance(raw_attributes, list):
        raise TypeError("semantic locator attributes must be a list")
    attributes: list[tuple[str, str]] = []
    for item in raw_attributes:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise TypeError("semantic locator attribute must be a string pair")
        attributes.append((item[0], item[1]))
    return ControlLocator(attributes=tuple(attributes), **scalars)


def _require_success_absent(
    snapshot: SemanticSnapshot,
    locator: ControlLocator,
) -> None:
    try:
        resolve_strict(snapshot, locator)
    except TargetNotFoundError:
        return
    except AmbiguousTargetError as exc:
        raise ScenarioBObservableResultError(
            "workflow success evidence is ambiguous before action"
        ) from exc
    raise ScenarioBObservableResultError(
        "workflow success evidence existed before action"
    )


def _require_same_logical_page(
    before: SemanticSnapshot,
    after: SemanticSnapshot,
) -> None:
    old = before.target.browser
    new = after.target.browser
    if old is None or new is None:
        raise ScenarioBObservableResultError(
            "Scenario-B completion evidence is not browser-bound"
        )
    if (
        old.session_id != new.session_id
        or old.context_id != new.context_id
        or old.page_id != new.page_id
    ):
        raise ScenarioBObservableResultError(
            "completion evidence came from a different browser page"
        )


def _require_registered_risk(
    executor: ToolExecutor,
    tool_id: str,
    risk: ToolRisk,
) -> None:
    spec = next(
        (item for item in executor.specs() if item.tool_id == tool_id),
        None,
    )
    if spec is None or spec.risk is not risk:
        raise ScenarioBAuthorityError(
            "Scenario-B ToolSpec risk contract is unavailable"
        )


def _verified_output(
    result: ToolResult,
    *,
    target_id: str,
) -> Mapping[str, object]:
    output = result.output
    if not isinstance(output, Mapping):
        raise ScenarioBObservableResultError(
            "canonical action returned no structured evidence"
        )
    if output.get("verified") is not True or output.get("target_id") != target_id:
        raise ScenarioBObservableResultError(
            "canonical action completion evidence does not match target"
        )
    evidence_ref = output.get("evidence_ref")
    if not isinstance(evidence_ref, str) or not evidence_ref.startswith("result:"):
        raise ScenarioBObservableResultError(
            "canonical action returned unsafe evidence reference"
        )
    return output


def _cursor_target(
    cursor: BatchCursor,
    target_id: str,
) -> TargetCursor:
    return next(
        target for target in cursor.state.targets if target.target_id == target_id
    )


def _stable_tab_id(
    task_id: str,
    cursor_id: str,
    target_id: str,
) -> str:
    body = json.dumps(
        [task_id, cursor_id, target_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"scenario-b-{hashlib.sha256(body).hexdigest()[:32]}"


def _phase_call_id(
    task_id: str,
    cursor_id: str,
    target_id: str,
    phase: str,
) -> str:
    body = json.dumps(
        [task_id, cursor_id, target_id, phase],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"scenario-b:{phase}:{hashlib.sha256(body).hexdigest()}"


def _result_reference(*, task_id: str, target_id: str) -> str:
    body = json.dumps(
        [task_id, target_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        "result:scenario-b/"
        f"{hashlib.sha256(body).hexdigest()[:32]}"
    )


def _safe_reason_code(error: str) -> str:
    mapping = {
        "approval required": "approval_required",
        "durable effect guard required": "durable_guard_required",
        "tool effect not safe to execute": "effect_not_safe",
        "tool timed out": "tool_timeout",
        "tool failed": "tool_failed",
        "tool result durability failed": "tool_durability_failed",
    }
    return mapping.get(error, "canonical_tool_blocked")
