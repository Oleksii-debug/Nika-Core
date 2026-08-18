from __future__ import annotations

import json
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from nika_core.builder.spec import AgentDefinition
from nika_core.model_gateway.contracts import ModelMessage, ModelRequest, PrivacyClass


class CompletionGateway(Protocol):
    async def complete(self, request: ModelRequest): ...


class AgentDraftService:
    """Uses Model Gateway only to draft; Pydantic remains the source of schema truth."""

    def __init__(self, gateway: CompletionGateway) -> None:
        self._gateway = gateway

    async def draft(
        self,
        request_text: str,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        privacy: PrivacyClass = PrivacyClass.PRIVATE,
        timeout_seconds: float = 60.0,
    ) -> AgentDefinition:
        if not request_text.strip():
            raise ValueError("agent request must not be empty")
        schema = AgentDefinition.model_json_schema()
        prompt = (
            "Return exactly one JSON object matching the supplied AgentDefinition JSON Schema. "
            "Do not wrap it in Markdown and do not invent executable code. Dangerous tools may be "
            "requested in the draft, but authorization is decided later by Nika.\n\n"
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False, sort_keys=True)}\n\n"
            f"User request:\n{request_text.strip()}"
        )
        response = await self._gateway.complete(
            ModelRequest(
                request_id=f"agent-draft-{uuid4().hex}",
                messages=(
                    ModelMessage(
                        role="system",
                        content=(
                            "You draft declarative Nika agent configuration only. Permission truth, "
                            "tool registration and activation are validated outside the model."
                        ),
                    ),
                    ModelMessage(role="user", content=prompt),
                ),
                model=model,
                provider_id=provider_id,
                privacy=privacy,
                timeout_seconds=timeout_seconds,
                temperature=0,
                metadata={"purpose": "agent_builder_draft", "schema_version": "1"},
            )
        )
        try:
            return AgentDefinition.model_validate_json(response.text)
        except ValidationError as exc:
            raise ValueError("model returned an invalid AgentDefinition draft") from exc
