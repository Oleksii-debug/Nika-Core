from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Timer
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from nika_core.diagnostics.health import HealthCheck, HealthStatus

_MAX_HEALTH_TIMEOUT_SECONDS = 30.0
_MAX_HEALTH_RESPONSE_BYTES = 1024 * 1024
_MAX_MODEL_ID_CHARS = 512
_MAX_BASE_URL_CHARS = 2048
_MAX_MODEL_CATALOG_ENTRIES = 4096


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
        facts = (
            ("configured", self.configured),
            ("reachable", self.reachable),
            ("model_present", self.model_present),
            ("model_ready", self.model_ready),
            ("inference_proven", self.inference_proven),
        )
        for name, value in facts:
            if type(value) is not ModelHealthFact:
                raise TypeError(f"{name} must be a canonical ModelHealthFact")
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
        if (
            self.inference_proven is ModelHealthFact.YES
            and self.configured is not ModelHealthFact.YES
        ):
            raise ValueError("inference_proven=yes requires configured=yes")

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
    def has_successful_inference(
        self,
        *,
        provider_id: str,
        model_id: str,
        route_identity: str,
    ) -> bool | None: ...


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
        self._base_url = base_url
        self._provider_id = provider_id
        self._timeout_seconds = timeout_seconds
        self._evidence_port = evidence_port
        self._client_factory = client_factory

    def snapshot(self) -> ModelHealthSnapshot:
        configured = self._configured()
        if configured is not ModelHealthFact.YES:
            return ModelHealthSnapshot(
                configured=configured,
                reachable=ModelHealthFact.UNKNOWN,
                model_present=ModelHealthFact.UNKNOWN,
                model_ready=ModelHealthFact.UNKNOWN,
                inference_proven=ModelHealthFact.UNKNOWN,
            )

        route_identity = self._route_identity()
        request_base_url = self._request_base_url()
        inference_proven = self._inference_proven(route_identity=route_identity)
        try:
            client = self._client_factory(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
            with client, self._health_deadline(client) as deadline_expired:
                tags_response, tags_reachable = self._bounded_get(
                    client,
                    f"{request_base_url}/api/tags",
                    deadline_expired=deadline_expired,
                )
                if not tags_reachable:
                    return ModelHealthSnapshot(
                        configured=configured,
                        reachable=ModelHealthFact.UNKNOWN,
                        model_present=ModelHealthFact.UNKNOWN,
                        model_ready=ModelHealthFact.UNKNOWN,
                        inference_proven=inference_proven,
                    )
                reachable = ModelHealthFact.YES
                present = (
                    self._presence_from_response(tags_response)
                    if tags_response is not None
                    else ModelHealthFact.UNKNOWN
                )
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
                    running_response, _ = self._bounded_get(
                        client,
                        f"{request_base_url}/api/ps",
                        deadline_expired=deadline_expired,
                    )
                except httpx.TransportError:
                    ready = ModelHealthFact.UNKNOWN
                else:
                    ready = (
                        self._readiness_from_response(running_response)
                        if running_response is not None
                        else ModelHealthFact.UNKNOWN
                    )
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
            not self._valid_route_text(self._model_id, max_chars=_MAX_MODEL_ID_CHARS)
            or not self._valid_route_text(self._base_url, max_chars=_MAX_BASE_URL_CHARS)
            or type(self._provider_id) is not str
            or self._provider_id != "ollama"
            or not self._valid_timeout(self._timeout_seconds)
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
            or port == 0
        ):
            return ModelHealthFact.NO
        return ModelHealthFact.YES

    @staticmethod
    def _valid_route_text(value: object, *, max_chars: int) -> bool:
        if type(value) is not str or not value or len(value) > max_chars:
            return False
        if value != value.strip():
            return False
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return all(char.isprintable() for char in value)

    @staticmethod
    def _valid_timeout(value: object) -> bool:
        if type(value) is not int and type(value) is not float:
            return False
        try:
            number = float(value)
        except OverflowError:
            return False
        return math.isfinite(number) and 0 < number <= _MAX_HEALTH_TIMEOUT_SECONDS

    def _request_base_url(self) -> str:
        if self._base_url.endswith("/"):
            return self._base_url[:-1]
        return self._base_url

    def _route_identity(self) -> str:
        parsed = urlsplit(self._base_url)
        host = parsed.hostname
        port = parsed.port
        if host is None or port is None:
            raise ValueError("validated Ollama route lost host or port identity")
        host_identity = f"[{host}]" if ":" in host else host
        return f"{parsed.scheme.lower()}://{host_identity}:{port}"

    def _inference_proven(self, *, route_identity: str) -> ModelHealthFact:
        evidence_port = self._evidence_port
        if evidence_port is None:
            return ModelHealthFact.UNKNOWN
        try:
            result = evidence_port.has_successful_inference(
                provider_id=self._provider_id,
                model_id=self._model_id,
                route_identity=route_identity,
            )
        except Exception:  # noqa: BLE001
            return ModelHealthFact.UNKNOWN
        if result is True:
            return ModelHealthFact.YES
        if result is False:
            return ModelHealthFact.NO
        return ModelHealthFact.UNKNOWN

    @contextmanager
    def _health_deadline(self, client: httpx.Client) -> Iterator[Event]:
        """Abort the active metadata transport at one total wall-clock deadline."""

        deadline_expired = Event()
        timer = Timer(
            float(self._timeout_seconds),
            self._expire_health_transport,
            args=(client, deadline_expired),
        )
        timer.daemon = True
        timer.start()
        try:
            yield deadline_expired
        finally:
            timer.cancel()
            timer.join()

    @staticmethod
    def _expire_health_transport(client: httpx.Client, deadline_expired: Event) -> None:
        deadline_expired.set()
        try:
            client.close()
        except Exception:  # noqa: BLE001 - best-effort transport abort
            return

    @staticmethod
    def _bounded_get(
        client: httpx.Client,
        url: str,
        *,
        deadline_expired: Event | None = None,
    ) -> tuple[httpx.Response | None, bool]:
        """Read bounded metadata and report whether HTTP response headers were observed."""

        if deadline_expired is not None and deadline_expired.is_set():
            return None, False

        headers_observed = False
        stream = getattr(client, "stream", None)
        try:
            if not callable(stream):
                if deadline_expired is not None and deadline_expired.is_set():
                    return None, False
                response = client.get(url)
                headers_observed = True
                if deadline_expired is not None and deadline_expired.is_set():
                    return None, True
                return response, True

            with stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                headers_observed = True
                if deadline_expired is not None and deadline_expired.is_set():
                    return None, True
                if not OllamaModelHealthProbe._successful_response(response):
                    return httpx.Response(status_code=response.status_code), True
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    return None, True
                declared = response.headers.get("content-length")
                if declared is not None and (
                    not declared.isascii()
                    or not declared.isdecimal()
                    or len(declared) > 7
                    or int(declared) > _MAX_HEALTH_RESPONSE_BYTES
                ):
                    return None, True
                payload = bytearray()
                try:
                    for chunk in response.iter_raw(chunk_size=16384):
                        if deadline_expired is not None and deadline_expired.is_set():
                            return None, True
                        if len(chunk) > _MAX_HEALTH_RESPONSE_BYTES - len(payload):
                            return None, True
                        payload.extend(chunk)
                except httpx.TransportError:
                    # Headers arrived: keep reachability, but trust no partial catalog.
                    return None, True
                if deadline_expired is not None and deadline_expired.is_set():
                    return None, True
                return (
                    httpx.Response(
                        status_code=response.status_code,
                        content=bytes(payload),
                    ),
                    True,
                )
        except httpx.TransportError:
            if deadline_expired is not None and deadline_expired.is_set():
                return None, headers_observed
            raise
        except RuntimeError:
            if deadline_expired is not None and deadline_expired.is_set():
                return None, headers_observed
            raise
        except httpx.StreamError:
            return None, headers_observed

    def _presence_from_response(self, response: httpx.Response) -> ModelHealthFact:
        if not self._successful_response(response):
            return ModelHealthFact.UNKNOWN
        models = self._models(response)
        if models is None:
            return ModelHealthFact.UNKNOWN
        return ModelHealthFact.YES if self._selected_model_in(models) else ModelHealthFact.NO

    def _readiness_from_response(self, response: httpx.Response) -> ModelHealthFact:
        if not self._successful_response(response):
            return ModelHealthFact.UNKNOWN
        models = self._models(response)
        if models is None:
            return ModelHealthFact.UNKNOWN
        if self._selected_model_in(models):
            return ModelHealthFact.YES
        # Not currently running is not proof that an installed model cannot be made ready.
        return ModelHealthFact.UNKNOWN

    def _selected_model_in(self, models: set[str]) -> bool:
        if self._model_id in models:
            return True
        leaf = self._model_id.rsplit("/", 1)[-1]
        if ":" in leaf or "@" in leaf:
            return False
        return f"{self._model_id}:latest" in models

    @staticmethod
    def _same_model_identity(left: str, right: str) -> bool:
        if left == right:
            return True

        def default_tag_alias(value: str) -> str | None:
            leaf = value.rsplit("/", 1)[-1]
            if ":" in leaf or "@" in leaf:
                return None
            return f"{value}:latest"

        return default_tag_alias(left) == right or default_tag_alias(right) == left

    @staticmethod
    def _successful_response(response: httpx.Response) -> bool:
        status_code = response.status_code
        return type(status_code) is int and 200 <= status_code < 300

    @staticmethod
    def _json_body(response: httpx.Response) -> object | None:
        """Decode bounded real HTTP JSON without ambiguous object authority."""

        if type(response) is not httpx.Response:
            try:
                return response.json()
            except (ValueError, TypeError, RecursionError):
                return None

        def object_without_duplicates(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        def reject_nonstandard_constant(_value: str) -> object:
            raise ValueError("non-standard JSON numeric constant")

        try:
            text = response.content.decode("utf-8")
            return json.loads(
                text,
                object_pairs_hook=object_without_duplicates,
                parse_constant=reject_nonstandard_constant,
            )
        except (
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
            httpx.StreamError,
        ):
            return None

    @staticmethod
    def _models(response: httpx.Response) -> set[str] | None:
        body = OllamaModelHealthProbe._json_body(response)
        if body is None:
            return None
        if type(body) is not dict:
            return None
        raw_models = body.get("models")
        if type(raw_models) is not list or len(raw_models) > _MAX_MODEL_CATALOG_ENTRIES:
            return None
        identities: set[str] = set()
        for item in raw_models:
            if type(item) is not dict:
                return None
            item_identities: list[str] = []
            for key in ("model", "name"):
                if key not in item:
                    continue
                value = item[key]
                if not OllamaModelHealthProbe._valid_route_text(
                    value,
                    max_chars=_MAX_MODEL_ID_CHARS,
                ):
                    return None
                item_identities.append(value)
            if not item_identities:
                return None
            if (
                len(item_identities) == 2
                and not OllamaModelHealthProbe._same_model_identity(
                    item_identities[0], item_identities[1]
                )
            ):
                return None
            identities.update(item_identities)
        return identities
