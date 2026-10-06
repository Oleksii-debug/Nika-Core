from __future__ import annotations

import asyncio
import os

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_gateway.providers import OllamaProvider


async def main() -> None:
    model = os.environ.get("NIKA_OLLAMA_PROOF_MODEL", "smollm2:135m-instruct-q5_K_M")
    base_url = os.environ.get("NIKA_OLLAMA_BASE_URL", "http://localhost:11434")

    gateway = ModelGateway()
    gateway.register(
        OllamaProvider(default_model=model, base_url=base_url, think=False),
        default=True,
    )
    response = await gateway.complete(
        ModelRequest(
            request_id="m4-live-ollama-proof",
            messages=(ModelMessage(role="user", content="Reply with one short word."),),
            provider_kind=ProviderKind.LOCAL,
            privacy=PrivacyClass.PRIVATE,
            timeout_seconds=120,
            temperature=0,
        )
    )
    if response.provider_id != "ollama":
        raise RuntimeError(f"unexpected provider: {response.provider_id}")
    if response.provider_kind is not ProviderKind.LOCAL:
        raise RuntimeError(f"unexpected provider kind: {response.provider_kind}")
    if response.model != model:
        raise RuntimeError(f"unexpected model: {response.model}")
    if not response.text.strip():
        raise RuntimeError("Ollama returned an empty response")
    print(
        "M4 live native Ollama proof passed:",
        response.provider_id,
        response.model,
        f"{response.latency_ms:.1f}ms" if response.latency_ms is not None else "latency unknown",
    )


if __name__ == "__main__":
    asyncio.run(main())
