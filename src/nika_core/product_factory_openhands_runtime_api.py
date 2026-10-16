from __future__ import annotations

import asyncio
import math
import pathlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from nika_core.toolsmith.contracts import CodingJob, IsolationClass, NetworkMode
from nika_core.toolsmith.openhands_remote_worker import OpenHandsSandboxEndpoint

_RUNTIME_API_KEY_HEADER = "X-API-Key"
_SESSION_API_KEY_HEADER = "X-Session-API-Key"
_AGENT_SERVER_PORT = 60000
_MAX_CONTROL_RESPONSE_BYTES = 256 * 1024
_ALLOWED_IMAGE_PULL_POLICIES = frozenset({"Always", "IfNotPresent", "Never"})
_ALLOWED_RESOURCE_FACTORS = frozenset({1, 2, 4, 8})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class OpenHandsRuntimeApiError(RuntimeError):
    """Raised when Runtime API sandbox authority cannot be proven safely."""


@dataclass(frozen=True, slots=True)
class OpenHandsRuntimeApiConfig:
    """Trusted deployment contract for an OpenHands Runtime API control plane.

    network_policy_enforced is an operator/deployment attestation. The public
    Runtime API response does not prove sandbox egress policy, so Nika requires
    composition to assert that property explicitly instead of inventing it from
    a returned URL.
    """

    runtime_api_url: str
    server_image: str
    working_dir: str
    agent_server_hosts: tuple[str, ...]
    sandbox_egress_hosts: tuple[str, ...]
    network_policy_enforced: bool
    image_pull_policy: str = "IfNotPresent"
    runtime_class: str | None = "sysbox-runc"
    resource_factor: int = 1
    init_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        _validate_authority_url(
            self.runtime_api_url,
            label="Runtime API",
            exception_type=ValueError,
        )
        _canonical_text(self.server_image, "OpenHands server image", max_bytes=1024)
        if self.server_image.casefold().endswith(":latest"):
            raise ValueError("OpenHands server image must not use the mutable latest tag")
        _canonical_working_dir(self.working_dir)
        _canonical_host_tuple(
            self.agent_server_hosts,
            "Agent Server hosts",
            require_nonempty=True,
        )
        _canonical_host_tuple(
            self.sandbox_egress_hosts,
            "sandbox egress hosts",
            require_nonempty=False,
        )
        if type(self.network_policy_enforced) is not bool or not self.network_policy_enforced:
            raise ValueError(
                "OpenHands Runtime API deployment must attest enforced sandbox network policy"
            )
        if self.image_pull_policy not in _ALLOWED_IMAGE_PULL_POLICIES:
            raise ValueError("OpenHands image pull policy is unsupported")
        if self.runtime_class is not None:
            _canonical_text(self.runtime_class, "OpenHands runtime class", max_bytes=256)
        if (
            type(self.resource_factor) is not int
            or self.resource_factor not in _ALLOWED_RESOURCE_FACTORS
        ):
            raise ValueError("OpenHands resource factor must be one of 1, 2, 4, 8")
        _bounded_positive_number(
            self.init_timeout_seconds,
            "OpenHands Runtime API init timeout",
            maximum=900.0,
        )
        _bounded_positive_number(
            self.poll_interval_seconds,
            "OpenHands Runtime API poll interval",
            maximum=10.0,
        )


@dataclass(frozen=True, slots=True)
class _RuntimeSession:
    session_id: str
    runtime_id: str
    url: str
    session_api_key: str
    status: str
    pod_status: str | None


class OpenHandsRuntimeApiSandboxProvider:
    """Provision remote Agent Server sandboxes without embedding OpenHands in Nika.

    The injected control-client factory owns the Runtime API credential. The
    Agent Server session key is redeemed per use and is never stored in the
    endpoint, durable recovery binding, config object, or provider instance.
    Fresh-process recovery therefore re-redeems authentication by durable
    session identity instead of persisting a secret.

    Successful work stops its runtime. Unsuccessful work pauses its runtime so
    an exact bound conversation remains available for fail-closed recovery.
    """

    def __init__(
        self,
        config: OpenHandsRuntimeApiConfig,
        *,
        control_client_factory: Callable[[], httpx.Client],
        agent_client_factory: Callable[[str, str], httpx.Client] | None = None,
    ) -> None:
        if type(config) is not OpenHandsRuntimeApiConfig:
            raise TypeError("config must be OpenHandsRuntimeApiConfig")
        if not callable(control_client_factory):
            raise TypeError("control_client_factory must be callable")
        if agent_client_factory is not None and not callable(agent_client_factory):
            raise TypeError("agent_client_factory must be callable")
        self._config = config
        self._control_client_factory = control_client_factory
        self._agent_client_factory = agent_client_factory or _default_agent_client

    async def acquire(self, job: CodingJob) -> OpenHandsSandboxEndpoint:
        if type(job) is not CodingJob:
            raise OpenHandsRuntimeApiError("Runtime API acquire requires an exact CodingJob")
        self._require_job_network_authority(job)
        session_id = str(uuid.uuid4())
        return await asyncio.to_thread(self._acquire_sync, session_id)

    async def release(
        self,
        job: CodingJob,
        endpoint: OpenHandsSandboxEndpoint,
        *,
        succeeded: bool,
    ) -> None:
        if type(job) is not CodingJob:
            raise OpenHandsRuntimeApiError("Runtime API release requires an exact CodingJob")
        if type(succeeded) is not bool:
            raise OpenHandsRuntimeApiError("Runtime API release outcome must be an exact boolean")
        self._require_job_network_authority(job)
        self._require_endpoint(endpoint)
        await asyncio.to_thread(self._release_sync, endpoint, succeeded)

    def client_for(self, endpoint: OpenHandsSandboxEndpoint) -> httpx.Client:
        """Redeem a current authenticated Agent Server client for this endpoint."""

        self._require_endpoint(endpoint)
        with self._control_client() as control:
            session = self._load_session(
                control,
                endpoint.endpoint_id,
                allow_missing=False,
            )
            assert session is not None
            if session.status == "paused":
                self._resume(control, session.runtime_id)
            elif session.status != "running":
                raise OpenHandsRuntimeApiError(
                    "OpenHands runtime is not available for bound-session recovery"
                )
            session = self._wait_until_running(
                control,
                endpoint.endpoint_id,
                expected_runtime_id=session.runtime_id,
                expected_url=endpoint.host,
            )
            self._require_bound_session(session, endpoint)
            self._verify_agent_server_health(session)
            session_key = session.session_api_key

        return self._agent_client(endpoint.host, session_key)

    def _agent_client(self, base_url: str, session_key: str) -> httpx.Client:
        try:
            client = self._agent_client_factory(base_url, session_key)
        except Exception as exc:  # noqa: BLE001 - authenticated client authority boundary
            raise OpenHandsRuntimeApiError(
                "Agent Server authenticated client could not be acquired"
            ) from exc
        if not isinstance(client, httpx.Client):
            raise OpenHandsRuntimeApiError(
                "Agent Server client factory must return an httpx.Client"
            )
        if str(client.base_url).rstrip("/") != base_url.rstrip("/"):
            client.close()
            raise OpenHandsRuntimeApiError(
                "Agent Server client is bound to a different endpoint"
            )
        header = client.headers.get(_SESSION_API_KEY_HEADER)
        if header != session_key:
            client.close()
            raise OpenHandsRuntimeApiError(
                "Agent Server client authentication disagrees with Runtime API authority"
            )
        return client

    def _verify_agent_server_health(self, session: _RuntimeSession) -> None:
        client = self._agent_client(session.url, session.session_api_key)
        try:
            try:
                response = client.get("/health", timeout=5.0, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise OpenHandsRuntimeApiError(
                    "Agent Server health probe transport failed"
                ) from exc
            if not 200 <= response.status_code < 300:
                raise OpenHandsRuntimeApiError(
                    f"Agent Server health probe returned HTTP {response.status_code}"
                )
            if len(response.content) > _MAX_CONTROL_RESPONSE_BYTES:
                raise OpenHandsRuntimeApiError(
                    "Agent Server health response exceeded the control-plane limit"
                )
        finally:
            client.close()

    def _acquire_sync(self, session_id: str) -> OpenHandsSandboxEndpoint:
        runtime_id: str | None = None
        with self._control_client() as control:
            try:
                payload: dict[str, object] = {
                    "image": self._config.server_image,
                    "command": (
                        f"/usr/local/bin/openhands-agent-server --port {_AGENT_SERVER_PORT}"
                    ),
                    "working_dir": "/",
                    "environment": {},
                    "session_id": session_id,
                    "run_as_user": 10001,
                    "fs_group": 10001,
                    "image_pull_policy": self._config.image_pull_policy,
                }
                if self._config.runtime_class is not None:
                    payload["runtime_class"] = self._config.runtime_class
                if self._config.resource_factor != 1:
                    payload["resource_factor"] = self._config.resource_factor

                started = self._request_json(
                    control,
                    "POST",
                    "/start",
                    operation="runtime start",
                    json=payload,
                    timeout=self._config.init_timeout_seconds,
                )
                runtime_id = _runtime_id(started.get("runtime_id", started.get("id")))
                session = self._wait_until_running(
                    control,
                    session_id,
                    expected_runtime_id=runtime_id,
                    expected_url=None,
                )
                endpoint = OpenHandsSandboxEndpoint(
                    endpoint_id=session.session_id,
                    host=session.url,
                    working_dir=self._config.working_dir,
                    isolation_class=IsolationClass.REMOTE_SANDBOXED,
                    sandbox_egress_hosts=self._config.sandbox_egress_hosts,
                    network_policy_enforced=self._config.network_policy_enforced,
                    fresh_workspace=True,
                )
                self._require_endpoint(endpoint)
                self._verify_agent_server_health(session)
                return endpoint
            except Exception:  # noqa: BLE001 - failed provisioning must attempt cleanup
                if runtime_id is not None:
                    self._stop_best_effort(control, runtime_id)
                raise

    def _release_sync(self, endpoint: OpenHandsSandboxEndpoint, succeeded: bool) -> None:
        with self._control_client() as control:
            session = self._load_session(
                control,
                endpoint.endpoint_id,
                allow_missing=succeeded,
            )
            if session is None:
                return
            self._require_bound_session(session, endpoint)
            if succeeded:
                self._request_action(
                    control,
                    "/stop",
                    operation="runtime stop",
                    runtime_id=session.runtime_id,
                    timeout=30.0,
                )
                return
            if session.status == "paused":
                return
            if session.status != "running":
                raise OpenHandsRuntimeApiError(
                    "failed OpenHands work no longer has a preservable runtime"
                )
            self._request_action(
                control,
                "/pause",
                operation="runtime pause",
                runtime_id=session.runtime_id,
                timeout=30.0,
            )

    def _require_job_network_authority(self, job: CodingJob) -> None:
        if job.network_policy.mode is not NetworkMode.APPROVED_HOSTS:
            raise OpenHandsRuntimeApiError(
                "Runtime API provisioning requires explicit approved-host policy"
            )
        try:
            approved = {
                _canonical_host(item) for item in job.network_policy.approved_hosts
            }
        except ValueError as exc:
            raise OpenHandsRuntimeApiError(
                "Runtime API provisioning received an invalid approved-host policy"
            ) from exc
        required = {
            _url_host(self._config.runtime_api_url),
            *self._config.agent_server_hosts,
            *self._config.sandbox_egress_hosts,
        }
        if not required <= approved:
            raise OpenHandsRuntimeApiError(
                "Runtime API, Agent Server, or sandbox egress host is not approved"
            )

    def _require_endpoint(self, endpoint: OpenHandsSandboxEndpoint) -> None:
        if type(endpoint) is not OpenHandsSandboxEndpoint:
            raise OpenHandsRuntimeApiError(
                "Runtime API client requires an exact sandbox endpoint attestation"
            )
        if (
            endpoint.working_dir != self._config.working_dir
            or endpoint.isolation_class is not IsolationClass.REMOTE_SANDBOXED
            or endpoint.sandbox_egress_hosts != self._config.sandbox_egress_hosts
            or endpoint.network_policy_enforced is not True
            or endpoint.fresh_workspace is not True
            or endpoint.control_plane_host not in self._config.agent_server_hosts
        ):
            raise OpenHandsRuntimeApiError(
                "bound OpenHands endpoint disagrees with trusted Runtime API configuration"
            )

    @staticmethod
    def _require_bound_session(
        session: _RuntimeSession,
        endpoint: OpenHandsSandboxEndpoint,
    ) -> None:
        if session.session_id != endpoint.endpoint_id or session.url != endpoint.host:
            raise OpenHandsRuntimeApiError(
                "Runtime API session identity or Agent Server URL changed"
            )

    def _wait_until_running(
        self,
        control: httpx.Client,
        session_id: str,
        *,
        expected_runtime_id: str,
        expected_url: str | None,
    ) -> _RuntimeSession:
        deadline = time.monotonic() + float(self._config.init_timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OpenHandsRuntimeApiError("OpenHands runtime did not become ready")
            session = self._load_session(control, session_id, allow_missing=True)
            if session is not None:
                if session.runtime_id != expected_runtime_id:
                    raise OpenHandsRuntimeApiError(
                        "Runtime API session changed runtime identity"
                    )
                if expected_url is not None and session.url != expected_url:
                    raise OpenHandsRuntimeApiError(
                        "Runtime API session changed Agent Server URL"
                    )
                if session.status == "running" and session.pod_status in {None, "running", "ready"}:
                    return session
                if session.status not in {"starting", "running"}:
                    raise OpenHandsRuntimeApiError(
                        "OpenHands runtime entered a non-runnable state"
                    )
            time.sleep(min(float(self._config.poll_interval_seconds), remaining))

    def _resume(self, control: httpx.Client, runtime_id: str) -> None:
        self._request_action(
            control,
            "/resume",
            operation="runtime resume",
            runtime_id=runtime_id,
            timeout=self._config.init_timeout_seconds,
        )

    def _load_session(
        self,
        control: httpx.Client,
        session_id: str,
        *,
        allow_missing: bool,
    ) -> _RuntimeSession | None:
        response = self._request(
            control,
            "GET",
            f"/sessions/{session_id}",
            operation="runtime session lookup",
            timeout=min(30.0, float(self._config.init_timeout_seconds)),
            allow_not_found=allow_missing,
        )
        if response is None:
            return None
        payload = _json_object(response, "runtime session lookup")
        returned_session = payload.get("session_id")
        if returned_session is not None and returned_session != session_id:
            raise OpenHandsRuntimeApiError("Runtime API returned a different session identity")
        raw_pod = payload.get("pod_status")
        return _RuntimeSession(
            session_id=session_id,
            runtime_id=_runtime_id(payload.get("runtime_id", payload.get("id"))),
            url=_agent_server_url(payload.get("url")),
            session_api_key=_session_key(payload.get("session_api_key")),
            status=_status(payload.get("status")),
            pod_status=None if raw_pod is None else _status(raw_pod),
        )

    def _control_client(self) -> httpx.Client:
        try:
            client = self._control_client_factory()
        except Exception as exc:  # noqa: BLE001 - credential authority boundary
            raise OpenHandsRuntimeApiError(
                "Runtime API authenticated client could not be acquired"
            ) from exc
        if not isinstance(client, httpx.Client):
            raise OpenHandsRuntimeApiError(
                "Runtime API client factory must return an httpx.Client"
            )
        if str(client.base_url).rstrip("/") != self._config.runtime_api_url:
            client.close()
            raise OpenHandsRuntimeApiError(
                "Runtime API client is bound to a different control plane"
            )
        api_key = client.headers.get(_RUNTIME_API_KEY_HEADER)
        if type(api_key) is not str or not api_key.strip():
            client.close()
            raise OpenHandsRuntimeApiError(
                "Runtime API client lacks X-API-Key authentication"
            )
        return client

    @staticmethod
    def _request_action(
        client: httpx.Client,
        path: str,
        *,
        operation: str,
        runtime_id: str,
        timeout: float,
    ) -> None:
        response = OpenHandsRuntimeApiSandboxProvider._request(
            client,
            "POST",
            path,
            operation=operation,
            timeout=timeout,
            json={"runtime_id": runtime_id},
            allow_not_found=False,
        )
        assert response is not None

    @staticmethod
    def _request_json(
        client: httpx.Client,
        method: str,
        path: str,
        *,
        operation: str,
        json: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        response = OpenHandsRuntimeApiSandboxProvider._request(
            client,
            method,
            path,
            operation=operation,
            timeout=timeout,
            json=json,
            allow_not_found=False,
        )
        assert response is not None
        return _json_object(response, operation)

    @staticmethod
    def _request(
        client: httpx.Client,
        method: str,
        path: str,
        *,
        operation: str,
        timeout: float,
        allow_not_found: bool,
        json: dict[str, object] | None = None,
    ) -> httpx.Response | None:
        try:
            response = client.request(
                method,
                path,
                json=json,
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise OpenHandsRuntimeApiError(
                f"OpenHands {operation} transport failed"
            ) from exc
        if allow_not_found and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise OpenHandsRuntimeApiError(
                f"OpenHands {operation} returned HTTP {response.status_code}"
            )
        if len(response.content) > _MAX_CONTROL_RESPONSE_BYTES:
            raise OpenHandsRuntimeApiError(
                f"OpenHands {operation} response exceeded the control-plane limit"
            )
        return response

    @staticmethod
    def _stop_best_effort(control: httpx.Client, runtime_id: str) -> None:
        try:
            OpenHandsRuntimeApiSandboxProvider._request_action(
                control,
                "/stop",
                operation="failed-acquire cleanup",
                runtime_id=runtime_id,
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001 - cleanup is best-effort after failed acquire
            pass


def _default_agent_client(base_url: str, session_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={_SESSION_API_KEY_HEADER: session_key},
        timeout=httpx.Timeout(10.0),
        follow_redirects=False,
    )


def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenHandsRuntimeApiError(
            f"OpenHands {operation} returned invalid JSON"
        ) from exc
    if type(payload) is not dict:
        raise OpenHandsRuntimeApiError(
            f"OpenHands {operation} returned a non-object payload"
        )
    return payload


def _validate_authority_url(
    value: object,
    *,
    label: str,
    exception_type: type[Exception],
) -> str:
    if type(value) is not str or not value or value != value.strip() or value.endswith("/"):
        raise exception_type(f"{label} URL must be canonical text without trailing slash")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
        or parsed.path not in {"", "/"}
    ):
        raise exception_type(f"{label} URL must contain only an http(s) authority")
    host = parsed.hostname.casefold().rstrip(".")
    if host not in _LOOPBACK_HOSTS and parsed.scheme != "https":
        raise exception_type(f"non-loopback {label} control plane requires HTTPS")
    return value


def _agent_server_url(value: object) -> str:
    try:
        return _validate_authority_url(
            value,
            label="Agent Server",
            exception_type=OpenHandsRuntimeApiError,
        )
    except TypeError as exc:
        raise OpenHandsRuntimeApiError("Agent Server URL is invalid") from exc


def _canonical_working_dir(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("OpenHands working_dir must be canonical text")
    candidate = pathlib.PurePosixPath(value)
    if (
        not value.startswith("/")
        or "\\" in value
        or ".." in candidate.parts
        or candidate == pathlib.PurePosixPath("/")
        or candidate.as_posix() != value
    ):
        raise ValueError("OpenHands working_dir must be a canonical absolute POSIX directory")
    return value


def _canonical_host_tuple(
    values: object,
    label: str,
    *,
    require_nonempty: bool,
) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{label} must be an immutable tuple")
    canonical = tuple(_canonical_host(item) for item in values)
    if require_nonempty and not canonical:
        raise ValueError(f"{label} must not be empty")
    if canonical != values or len(set(canonical)) != len(canonical):
        raise ValueError(f"{label} must be canonical and unique")
    return canonical


def _canonical_host(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("network host must be canonical non-empty text")
    candidate = value.casefold().rstrip(".")
    if (
        candidate != value
        or "/" in candidate
        or ":" in candidate
        or any(character.isspace() for character in candidate)
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise ValueError("network host must be a canonical bare host name")
    return candidate


def _url_host(value: str) -> str:
    parsed = urlparse(value)
    assert parsed.hostname is not None
    return parsed.hostname.casefold().rstrip(".")


def _canonical_text(value: object, label: str, *, max_bytes: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical non-empty text")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the byte limit")
    return value


def _runtime_id(value: object) -> str:
    try:
        return _canonical_text(value, "OpenHands runtime identity", max_bytes=512)
    except ValueError as exc:
        raise OpenHandsRuntimeApiError(
            "Runtime API returned an invalid runtime identity"
        ) from exc


def _status(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OpenHandsRuntimeApiError("Runtime API returned an invalid status")
    status = value.casefold()
    if status != value:
        raise OpenHandsRuntimeApiError(
            "Runtime API status must use canonical lowercase spelling"
        )
    return status


def _session_key(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise OpenHandsRuntimeApiError(
            "Runtime API did not return usable session authentication"
        )
    return value


def _bounded_positive_number(value: object, label: str, *, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
        or float(value) > maximum
    ):
        raise ValueError(f"{label} must be within (0, {maximum:g}] seconds")
    return float(value)
