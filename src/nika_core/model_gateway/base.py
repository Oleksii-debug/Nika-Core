from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[dict[str, str], ...]
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str


class ModelProvider(Protocol):
    provider_id: str

    def health(self) -> bool: ...

    def chat(self, request: ModelRequest) -> ModelResponse: ...
