from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class EvidenceStrength(IntEnum):
    NONE = 0
    DOCUMENTED = 1
    STABLE = 2
    DIRECT_FIT = 3


@dataclass(frozen=True, slots=True)
class RuntimeCandidate:
    runtime_id: str
    durable_local_resume: EvidenceStrength
    crash_recovery: EvidenceStrength
    human_approval: EvidenceStrength
    teams_subagents: EvidenceStrength
    mcp_tools: EvidenceStrength
    ollama: EvidenceStrength
    cancellation_async: EvidenceStrength
    observability: EvidenceStrength
    desktop_glue: EvidenceStrength
    stability: EvidenceStrength

    @property
    def score(self) -> int:
        return sum(
            int(value)
            for value in (
                self.durable_local_resume,
                self.crash_recovery,
                self.human_approval,
                self.teams_subagents,
                self.mcp_tools,
                self.ollama,
                self.cancellation_async,
                self.observability,
                self.desktop_glue,
                self.stability,
            )
        )


LANGGRAPH_2026_08 = RuntimeCandidate(
    runtime_id="langgraph",
    durable_local_resume=EvidenceStrength.DIRECT_FIT,
    crash_recovery=EvidenceStrength.DIRECT_FIT,
    human_approval=EvidenceStrength.DIRECT_FIT,
    teams_subagents=EvidenceStrength.STABLE,
    mcp_tools=EvidenceStrength.STABLE,
    ollama=EvidenceStrength.STABLE,
    cancellation_async=EvidenceStrength.STABLE,
    observability=EvidenceStrength.STABLE,
    desktop_glue=EvidenceStrength.DIRECT_FIT,
    stability=EvidenceStrength.STABLE,
)

MICROSOFT_AGENT_FRAMEWORK_2026_08 = RuntimeCandidate(
    runtime_id="microsoft-agent-framework",
    durable_local_resume=EvidenceStrength.STABLE,
    crash_recovery=EvidenceStrength.STABLE,
    human_approval=EvidenceStrength.DIRECT_FIT,
    teams_subagents=EvidenceStrength.DIRECT_FIT,
    mcp_tools=EvidenceStrength.DIRECT_FIT,
    ollama=EvidenceStrength.DOCUMENTED,
    cancellation_async=EvidenceStrength.STABLE,
    observability=EvidenceStrength.STABLE,
    desktop_glue=EvidenceStrength.STABLE,
    stability=EvidenceStrength.STABLE,
)


def choose_primary(candidates: tuple[RuntimeCandidate, ...]) -> RuntimeCandidate:
    if not candidates:
        raise ValueError("At least one runtime candidate is required")
    return max(candidates, key=lambda candidate: (candidate.score, candidate.runtime_id))
