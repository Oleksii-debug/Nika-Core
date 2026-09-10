from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from nika_core.diagnostics.health import HealthCheck, HealthStatus


class ModelHealthFact(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelHealthSnapshot:
    """Provider-neutral health facts for the currently selected local model."""

    configured: ModelHealthFact
    reachable: ModelHealthFact
    model_present: ModelHealthFact
    model_ready: ModelHealthFact
    inference_proven: ModelHealthFact

    def __post_init__(self) -> None:
        if self.model_present is ModelHealthFact.YES and self.reachable is not ModelHealthFact.YES:
            raise ValueError("model_present=yes requires reachable=yes")
        if self.model_ready is ModelHealthFact.YES and not (
            self.configured is ModelHealthFact.YES
            and self.reachable is ModelHealthFact.YES
            and self.model_present is ModelHealthFact.YES
        ):
            raise ValueError(
                "model_ready=yes requires configured=yes, reachable=yes, and model_present=yes"
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "configured": self.configured.value,
            "reachable": self.reachable.value,
            "model_present": self.model_present.value,
            "model_ready": self.model_ready.value,
            "inference_proven": self.inference_proven.value,
        }

    def to_health_check(self) -> HealthCheck:
        hard_failure = (
            self.configured is ModelHealthFact.NO
            or self.reachable is ModelHealthFact.NO
            or self.model_present is ModelHealthFact.NO
            or self.model_ready is ModelHealthFact.NO
        )
        if hard_failure:
            status = HealthStatus.FAIL
        elif all(
            value is ModelHealthFact.YES
            for value in (
                self.configured,
                self.reachable,
                self.model_present,
                self.model_ready,
                self.inference_proven,
            )
        ):
            status = HealthStatus.PASS
        else:
            status = HealthStatus.WARN
        return HealthCheck(
            check_id="model.local",
            status=status,
            summary=(
                "Local model health: "
                f"configured={self.configured.value}, "
                f"reachable={self.reachable.value}, "
                f"present={self.model_present.value}, "
                f"ready={self.model_ready.value}, "
                f"inference_proven={self.inference_proven.value}."
            ),
        )


class ModelHealthProbePort(Protocol):
    def snapshot(self) -> ModelHealthSnapshot: ...


class ModelInferenceEvidencePort(Protocol):
    def has_successful_inference(self, *, provider_id: str, model_id: str) -> bool | None: ...


class OllamaModelHealthProbe:
    """Metadata-only Ollama health probe.

    The probe intentionally performs no inference and no model acquisition. It uses the
    model catalog to establish presence and the running-model inventory as positive-only
    readiness evidence. A present model that is not currently reported as running remains
    UNKNOWN for readiness rather than being promoted to READY or demoted to unusable.
    """

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str = "http://localhost:11434",
        provider_id: str = "ollama",
        timeout_seconds: float = 2.0,
        evidence_port: ModelInferenceEvidencePort | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._provider_id = provider_id
        self._timeout_seconds = timeout_seconds
        self._evidence_port = evidence_port
        self._client_factory = client_factory

    def snapshot(self) -> ModelHealthSnapshot:
        configured = self._configured()
        inference_proven = self._inference_proven()
        if configured is not ModelHealthFact.YES:
            return ModelHealthSnapshot(
                configured=configured,
                reachable=ModelHealthFact.UNKNOWN,
                model_present=ModelHealthFact.UNKNOWN,
                model_ready=ModelHealthFact.UNKNOWN,
                inference_proven=inference_proven,
            )

        try:
            with self._client_factory(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                tags_response = client.get(f"{self._base_url}/api/tags")
                reachable = ModelHealthFact.YES
                present = self._presence_from_response(tags_response)
                if present is ModelHealthFact.NO:
                    return ModelHealthSnapshot(
                        configured=configured,
                        reachable=reachable,
                        model_present=present,
                        model_ready=ModelHealthFact.NO,
                        inference_proven=inference_proven,
                    )
                if present is not ModelHealthFact.YES:
                    return ModelHealthSnapshot(
                        configured=configured,
                        reachable=reachable,
                        model_present=present,
                        model_ready=ModelHealthFact.UNKNOWN,
                        inference_proven=inference_proven,
                    )
                try:
                    running_response = client.get(f"{self._base_url}/api/ps")
                except httpx.TransportError:
                    ready = ModelHealthFact.UNKNOWN
                else:
                    ready = self._readiness_from_response(running_response)
        except httpx.TransportError:
            return ModelHealthSnapshot(
                configured=configured,
                reachable=ModelHealthFact.NO,
                model_present=ModelHealthFact.UNKNOWN,
                model_ready=ModelHealthFact.UNKNOWN,
                inference_proven=inference_proven,
            )

        return ModelHealthSnapshot(
            configured=configured,
            reachable=reachable,
            model_present=present,
            model_ready=ready,
            inference_proven=inference_proven,
        )

    def _configured(self) -> ModelHealthFact:
        if (
            not isinstance(self._model_id, str)
            or not self._model_id.strip()
            or self._model_id != self._model_id.strip()
            or not isinstance(self._base_url, str)
            or not self._base_url.strip()
            or not isinstance(self._provider_id, str)
            or not self._provider_id.strip()
            or isinstance(self._timeout_seconds, bool)
            or not isinstance(self._timeout_seconds, (int, float))
            or self._timeout_seconds <= 0
        ):
            return ModelHealthFact.NO
        try:
            parsed = urlsplit(self._base_url)
            port = parsed.port
        except ValueError:
            return ModelHealthFact.NO
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port is None
        ):
            return ModelHealthFact.NO
        return ModelHealthFact.YES

    def _inference_proven(self) -> ModelHealthFact:
        evidence_port = self._evidence_port
        if evidence_port is None:
            return ModelHealthFact.UNKNOWN
        try:
            result = evidence_port.has_successful_inference(
                provider_id=self._provider_id,
                model_id=self._model_id,
            )
        except Exception:  # noqa: BLE001
            return ModelHealthFact.UNKNOWN
        if result is True:
            return ModelHealthFact.YES
        if result is False:
            return ModelHealthFact.NO
        return ModelHealthFact.UNKNOWN

    def _presence_from_response(self, response: httpx.Response) -> ModelHealthFact:
        if not 200 <= response.status_code < 300:
            return ModelHealthFact.UNKNOWN
        models = self._models(response)
        if models is None:
            return ModelHealthFact.UNKNOWN
        return ModelHealthFact.YES if self._model_id in models else ModelHealthFact.NO

    def _readiness_from_response(self, response: httpx.Response) -> ModelHealthFact:
        if not 200 <= response.status_code < 300:
            return ModelHealthFact.UNKNOWN
        models = self._models(response)
        if models is None:
            return ModelHealthFact.UNKNOWN
        if self._model_id in models:
            return ModelHealthFact.YES
        # Not currently running is not proof that an installed model cannot be made ready.
        return ModelHealthFact.UNKNOWN

    @staticmethod
    def _models(response: httpx.Response) -> set[str] | None:
        try:
            body = response.json()
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict):
            return None
        raw_models = body.get("models")
        if not isinstance(raw_models, list):
            return None
        identities: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                return None
            for key in ("model", "name"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    identities.add(value)
        return identities
