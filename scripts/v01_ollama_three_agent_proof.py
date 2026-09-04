from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.multi_agent.contracts import ChildRequest, TeamQuota
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.multi_agent.store import MultiAgentStore
from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.runtime.contracts import RuntimeOutcome


def _activate(
    repository: AgentDefinitionRepository,
    compiler: AgentCompiler,
    *,
    agent_id: str,
    goal: str,
) -> None:
    definition = AgentDefinition(
        agent_id=agent_id,
        name=agent_id,
        goal=goal,
        instructions="Return one short factual sentence. Do not claim tool use.",
        model_profile="ollama-local",
    )
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)


async def main() -> None:
    model = os.environ.get("NIKA_OLLAMA_PROOF_MODEL", "qwen3:8b")
    base_url = os.environ.get("NIKA_OLLAMA_BASE_URL", "http://localhost:11434")
    with tempfile.TemporaryDirectory(prefix="nika-v01-ollama-") as temp_dir:
        sqlite = SQLiteStore(Path(temp_dir) / "nika.db")
        sqlite.initialize()
        definitions = AgentDefinitionRepository(sqlite)
        compiler = AgentCompiler(tools=(), model_profiles={"ollama-local"})
        _activate(
            definitions,
            compiler,
            agent_id="supervisor",
            goal="Coordinate two local-model workers.",
        )
        _activate(
            definitions,
            compiler,
            agent_id="worker",
            goal="Complete the assigned local-model handoff.",
        )

        gateway = ModelGateway()
        gateway.register(
            OllamaProvider(default_model=model, base_url=base_url, think=False),
            default=True,
        )
        runtime = ModelGatewayAgentRuntime(
            gateway=gateway,
            definitions=definitions,
            provider_id="ollama",
            model=model,
            timeout_seconds=120,
        )
        team_store = MultiAgentStore(sqlite)
        team_store.create_team(
            team_id="v01-local-proof",
            root_member_id="root",
            root_agent_id="supervisor",
            root_agent_version=1,
            root_thread_id="thread-root",
            root_grants=(),
            quota=TeamQuota(
                max_depth=2,
                max_children_per_parent=2,
                max_total_agents=3,
                max_parallel=2,
            ),
        )
        supervisor = MultiAgentSupervisor(
            runtime=runtime,
            store=team_store,
            definitions=definitions,
        )
        executions = await supervisor.fan_out(
            team_id="v01-local-proof",
            parent_id="root",
            requests=(
                ChildRequest(
                    member_id="worker-1",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-worker-1",
                    payload={"request": "Reply that worker one is operational."},
                ),
                ChildRequest(
                    member_id="worker-2",
                    agent_id="worker",
                    agent_version=1,
                    thread_id="thread-worker-2",
                    payload={"request": "Reply that worker two is operational."},
                ),
            ),
        )

        identities: set[tuple[str, str]] = set()
        for execution in executions:
            result = execution.result
            if result is None or result.outcome is not RuntimeOutcome.COMPLETED:
                detail = result.error if result is not None else execution.exception
                raise RuntimeError(detail or "local model worker did not complete")
            provider_id = str(result.output.get("provider_id") or "")
            response_model = str(result.output.get("model_fingerprint") or "")
            if provider_id != "ollama" or not response_model:
                raise RuntimeError("local model identity proof is incomplete")
            identities.add((provider_id, response_model))

        if identities != {("ollama", model_identity_fingerprint(model))}:
            raise RuntimeError(f"unexpected local model identity: {sorted(identities)}")
        print(
            "V0.1 three-agent Ollama route proof passed:",
            "provider=ollama",
            f"model={model}",
            f"workers={len(executions)}",
        )


if __name__ == "__main__":
    asyncio.run(main())
