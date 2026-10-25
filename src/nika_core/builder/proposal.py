from __future__ import annotations

from nika_core.builder.compiler import AgentCompiler, CompilationResult
from nika_core.builder.drafting import AgentDraftService
from nika_core.model_gateway.contracts import PrivacyClass


class AgentProposalService:
    """Turn model-authored draft text into a deterministic registry-validated proposal."""

    def __init__(self, drafting: AgentDraftService, compiler: AgentCompiler) -> None:
        self._drafting = drafting
        self._compiler = compiler

    async def propose(
        self,
        request_text: str,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        privacy: PrivacyClass = PrivacyClass.PRIVATE,
        timeout_seconds: float = 60.0,
    ) -> CompilationResult:
        draft = await self._drafting.draft(
            request_text,
            provider_id=provider_id,
            model=model,
            privacy=privacy,
            timeout_seconds=timeout_seconds,
        )
        return self._compiler.compile(draft)
