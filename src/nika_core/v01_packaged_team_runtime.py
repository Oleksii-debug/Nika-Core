from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping
from typing import cast

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    MultiAgentStore,
    MultiAgentSupervisor,
    SourceInspectionAssignment,
    TeamState,
    V01CheckerAgent,
    encode_source_result,
)
from nika_core.research.local import extract_local_file, resolve_local_file
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
    SourceSpec,
)
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.tools import ToolRisk, ToolSpec
from nika_core.v01_three_agent_supervisor import (
    V01ChildAssignment,
    V01SourceWorkerAssignment,
    V01ThreeAgentConfig,
    V01ThreeAgentSupervisor,
)

_RUNTIME_ID = "nika.v01.packaged-three-agent"
_MODEL_PROFILE = "deterministic"
_WORKSPACE_ID = "default"
_CHECKER_ID = "v01.checker"
_WORKER_A_ID = "v01.source-a"
_WORKER_B_ID = "v01.source-b"
_GRANT = ToolGrant(tool_id="file.read", max_risk=0, scopes=("workspace",))
_MAX_SOURCE_BYTES = 16 * 1024 * 1024


class V01PackagedThreeAgentRuntime(AgentRuntimePort):
    """Thin packaged AgentRuntimePort over the canonical V0.1 three-agent team."""

    def __init__(self, *, store: SQLiteStore, config: AppConfig) -> None:
        self._sqlite = store
        self._config = config
        self._multi_store = MultiAgentStore(store)
        self._definitions = AgentDefinitionRepository(store)
        self._coordinator = MultiAgentSupervisor(
            runtime=self,
            store=self._multi_store,
            definitions=self._definitions,
        )

    @property
    def runtime_id(self) -> str:
        return _RUNTIME_ID

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]:
        return frozenset(
            {
                RuntimeCapability.DETERMINISTIC_NO_LLM,
                RuntimeCapability.DURABLE_RESUME,
                RuntimeCapability.CANCELLATION,
                RuntimeCapability.PARALLELISM,
                RuntimeCapability.SUBAGENTS,
            }
        )

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        material = f"{_RUNTIME_ID}\0{task_id}\0{thread_id}".encode()
        return "v01:" + hashlib.sha256(material).hexdigest()

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        expected = self.initial_resume_token(task_id=task_id, thread_id=thread_id)
        if resume_token != expected:
            return RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.INVALID,
                reason="Persisted V0.1 runtime cursor does not match task identity.",
            )
        checkpoint = hashlib.sha256(
            f"v01-packaged-checkpoint\0{expected}".encode()
        ).hexdigest()
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="Deterministic V0.1 team cursor is reconstructible from durable Nika state.",
            checkpoint_id=checkpoint,
        )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if self._is_member_thread(request.thread_id):
            return self._run_member_from_store(thread_id=request.thread_id)
        command = str(request.payload.get("command", "")).strip()
        if not command:
            return self._failed()
        return await self._run_outer(task_id=request.task_id, command=command)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        if request.mode is not RuntimeResumeMode.CONTINUE:
            return self._failed()
        expected = self.initial_resume_token(task_id=request.task_id, thread_id=request.thread_id)
        if request.resume_token != expected:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="V0.1 durable resume cursor is invalid.",
                error_code=RuntimeErrorCode.INVALID_RESUME,
            )
        if self._is_member_thread(request.thread_id):
            return self._run_member_from_store(thread_id=request.thread_id)
        command = self._stored_outer_command(request.task_id)
        if not command:
            return self._failed()
        return await self._run_outer(task_id=request.task_id, command=command)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        if self._is_member_thread(thread_id):
            return True
        team_id = self._team_id(task_id)
        try:
            state = self._multi_store.team_state(team_id)
        except KeyError:
            return True
        if state is TeamState.ACTIVE:
            await self._coordinator.cancel_team(team_id)
        return True

    async def _run_outer(self, *, task_id: str, command: str) -> RuntimeResult:
        try:
            _root, source_a, source_b = self._source_config()
            self._ensure_definitions()
            team_id = self._team_id(task_id)
            adapter = V01ThreeAgentSupervisor(
                coordinator=self._coordinator,
                store=self._multi_store,
                definitions=self._definitions,
                config=self._scenario_config(source_a=source_a, source_b=source_b),
            )
            result = await adapter.run(
                user_goal=command,
                shared_task_id=task_id,
                team_id=team_id,
            )
        except Exception:  # noqa: BLE001 - runtime boundary fails closed without diagnostics
            return self._failed()

        if result.team_state is TeamState.CANCELLED:
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED, output=result.final_output)
        if result.team_state is not TeamState.COMPLETED:
            return self._failed()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={
                "schema": "nika.v01.packaged-three-agent-result:v1",
                "task_id": task_id,
                "team_id": result.team_id,
                "team_result": result.final_output,
            },
        )

    def _run_member_from_store(self, *, thread_id: str) -> RuntimeResult:
        identity = self._member_identity(thread_id)
        if identity is None:
            return self._failed()
        team_id, member_id = identity
        try:
            handoff = self._multi_store.task_payload(team_id, member_id)
            stage = str(handoff.get("stage", ""))
            if stage == "source_worker":
                return self._run_source_worker(
                    team_id=team_id,
                    member_id=member_id,
                    handoff=handoff,
                )
            if stage == "checker":
                return self._run_checker(
                    team_id=team_id,
                    member_id=member_id,
                    handoff=handoff,
                )
        except Exception:  # noqa: BLE001 - runtime boundary fails closed without diagnostics
            return self._failed()
        return self._failed()

    def _run_source_worker(
        self,
        *,
        team_id: str,
        member_id: str,
        handoff: Mapping[str, object],
    ) -> RuntimeResult:
        assignment = SourceInspectionAssignment.from_payload(
            cast(Mapping[str, object], handoff["source_assignment"])
        )
        if assignment.team_id != team_id or assignment.member_id != member_id:
            return self._failed()
        if assignment.source.kind is not SourceKind.LOCAL_FILE:
            return self._failed()
        root, _, _ = self._source_config()
        document = extract_local_file(
            assignment.source.locator,
            allowed_root=root,
            max_bytes=_MAX_SOURCE_BYTES,
        )
        observed_at = datetime.now(UTC).isoformat()
        result_set = ResearchResultSet(
            result_set_id="result:" + hashlib.sha256(
                (
                    assignment.assignment_id
                    + "\0"
                    + document.text
                    + "\0"
                    + document.media_type
                ).encode()
            ).hexdigest()[:24],
            workspace_id=assignment.source.workspace_id,
            query="compare declared condition",
            items=(
                ResearchResultItem(
                    ordinal=0,
                    document_id=f"doc:{assignment.source.source_id}",
                    title="declared source",
                    snippet=document.text[:4000],
                    rank=1.0,
                    why_matched="bounded declared local source",
                    evidence=(
                        ResearchEvidence(
                            source_id=assignment.source.source_id,
                            source_kind=assignment.source.kind,
                            locator=assignment.source.locator,
                            observed_at=observed_at,
                            freshness=FreshnessState.CURRENT,
                        ),
                    ),
                ),
            ),
            created_at=observed_at,
        )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output=encode_source_result(assignment, result_set),
        )

    def _run_checker(
        self,
        *,
        team_id: str,
        member_id: str,
        handoff: Mapping[str, object],
    ) -> RuntimeResult:
        raw_assignments = handoff.get("source_assignments")
        if not isinstance(raw_assignments, list):
            return self._failed()
        assignments = tuple(
            SourceInspectionAssignment.from_payload(cast(Mapping[str, object], item))
            for item in raw_assignments
        )
        summary = V01CheckerAgent().compare(
            team_id=team_id,
            task_id=str(handoff["shared_task_id"]),
            checker_id=member_id,
            assignments=assignments,
            handoffs=self._multi_store.inbound_result_handoffs(team_id, member_id),
        )
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"checker_summary": summary.to_payload()},
        )

    def _source_config(self) -> tuple[Path, Path, Path]:
        raw_root = self._config.v01_source_root
        raw_a = self._config.v01_source_a
        raw_b = self._config.v01_source_b
        if raw_root is None or raw_a is None or raw_b is None:
            raise ValueError("V0.1 source configuration is incomplete")
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise ValueError("V0.1 source root is unavailable")

        def resolve(raw: Path) -> Path:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            return resolve_local_file(
                candidate,
                allowed_root=root,
                max_bytes=_MAX_SOURCE_BYTES,
            )

        source_a = resolve(raw_a)
        source_b = resolve(raw_b)
        if source_a == source_b:
            raise ValueError("V0.1 Scenario A requires two distinct source files")
        return root, source_a, source_b

    @staticmethod
    def _scenario_config(*, source_a: Path, source_b: Path) -> V01ThreeAgentConfig:
        return V01ThreeAgentConfig(
            checker=V01ChildAssignment(
                member_id="checker",
                agent_id=_CHECKER_ID,
                agent_version=1,
                requested_grants=(_GRANT,),
                instruction="Compare exactly two canonical source results.",
            ),
            workers=(
                V01SourceWorkerAssignment(
                    member_id="worker-a",
                    agent_id=_WORKER_A_ID,
                    agent_version=1,
                    requested_grants=(_GRANT,),
                    instruction="Inspect only declared source A.",
                    source=SourceSpec(
                        source_id="source-a",
                        workspace_id=_WORKSPACE_ID,
                        kind=SourceKind.LOCAL_FILE,
                        locator=str(source_a),
                    ),
                    max_items=1,
                ),
                V01SourceWorkerAssignment(
                    member_id="worker-b",
                    agent_id=_WORKER_B_ID,
                    agent_version=1,
                    requested_grants=(_GRANT,),
                    instruction="Inspect only declared source B.",
                    source=SourceSpec(
                        source_id="source-b",
                        workspace_id=_WORKSPACE_ID,
                        kind=SourceKind.LOCAL_FILE,
                        locator=str(source_b),
                    ),
                    max_items=1,
                ),
            ),
        )

    def _ensure_definitions(self) -> None:
        compiler = AgentCompiler(
            tools=(ToolSpec("file.read", "Read declared source", ToolRisk.READ_ONLY),),
            model_profiles={_MODEL_PROFILE},
        )
        for agent_id in (_CHECKER_ID, _WORKER_A_ID, _WORKER_B_ID):
            definition = AgentDefinition(
                agent_id=agent_id,
                version=1,
                name=agent_id,
                goal="Complete one bounded V0.1 Scenario A role.",
                instructions="Use only declared evidence and return canonical structured output.",
                model_profile=_MODEL_PROFILE,
                tool_grants=(_GRANT,),
                enabled=True,
            )
            stored = self._definitions.get(agent_id, 1)
            if stored is None:
                self._definitions.save_draft(compiler.compile(definition))
                self._definitions.activate(definition)
                continue
            active = self._definitions.require_active(agent_id, 1)
            if active.definition != definition:
                raise PermissionError("existing V0.1 packaged agent definition differs")

    def _stored_outer_command(self, task_id: str) -> str:
        team_id = self._team_id(task_id)
        try:
            handoff = self._multi_store.task_payload(team_id, "checker")
        except KeyError:
            handoff = None
        if isinstance(handoff, Mapping):
            goal = str(handoff.get("user_goal", "")).strip()
            if goal:
                return goal
        with self._sqlite.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return ""
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("command", "")).strip()

    @staticmethod
    def _failed() -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            error="V0.1 packaged three-agent execution failed closed.",
            error_code=RuntimeErrorCode.INTERNAL,
        )

    @staticmethod
    def _team_id(task_id: str) -> str:
        digest = hashlib.sha256(task_id.encode()).hexdigest()[:24]
        return f"v01-team-{digest}"

    @staticmethod
    def _is_member_thread(thread_id: str) -> bool:
        return thread_id.startswith("v01:")

    @staticmethod
    def _member_identity(thread_id: str) -> tuple[str, str] | None:
        parts = thread_id.split(":")
        if len(parts) != 3 or parts[0] != "v01" or not parts[1] or not parts[2]:
            return None
        return parts[1], parts[2]
