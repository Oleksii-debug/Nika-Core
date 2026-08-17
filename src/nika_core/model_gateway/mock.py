from __future__ import annotations

from nika_core.model_gateway.base import ModelRequest, ModelResponse


class MockProvider:
    provider_id = "mock"

    def health(self) -> bool:
        return True

    def chat(self, request: ModelRequest) -> ModelResponse:
        last = request.messages[-1]["content"] if request.messages else ""
        return ModelResponse(
            text=f"MOCK:{last}",
            provider=self.provider_id,
            model=request.model or "mock-v1",
        )
