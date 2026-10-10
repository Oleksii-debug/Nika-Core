from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from nika_core.research.models import RefreshDisposition, ResearchFetchFailureClass
from nika_core.research.source_identity import (
    ResearchSourceIdentityError,
    canonical_http_locator,
)


class NetworkPolicyError(RuntimeError):
    pass


class PrivateResearchSourceError(NetworkPolicyError):
    pass


class UnsupportedResearchSourceError(NetworkPolicyError):
    pass


class ResponseTooLargeError(RuntimeError):
    pass


Resolver = Callable[[str, int], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class HttpFetchPolicy:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    write_timeout_seconds: float = 5.0
    pool_timeout_seconds: float = 5.0
    max_response_bytes: int = 16 * 1024 * 1024
    max_redirects: int = 5
    max_resolved_addresses: int = 4
    max_connections: int = 10
    max_keepalive_connections: int = 5
    max_attempts: int = 3
    backoff_base_seconds: float = 0.25
    max_backoff_seconds: float = 2.0
    allow_insecure_http: bool = False
    allow_private_networks: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_ports: tuple[int, ...] = (80, 443)
    user_agent: str = "Nika-Core Research/1"

    def __post_init__(self) -> None:
        positive = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.write_timeout_seconds,
            self.pool_timeout_seconds,
            self.max_response_bytes,
            self.max_resolved_addresses,
            self.max_connections,
            self.max_keepalive_connections,
            self.max_attempts,
            self.max_backoff_seconds,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("HTTP policy limits must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds must not be negative")
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("keepalive connections cannot exceed total connections")
        if not self.allowed_ports or any(port < 1 or port > 65535 for port in self.allowed_ports):
            raise ValueError("allowed_ports must contain valid TCP ports")
        if "\r" in self.user_agent or "\n" in self.user_agent or not self.user_agent.strip():
            raise ValueError("user_agent must be a single non-empty header value")


@dataclass(frozen=True, slots=True)
class HttpValidators:
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True, slots=True)
class HttpFetchResult:
    disposition: RefreshDisposition
    requested_url: str
    final_url: str
    status_code: int | None
    media_type: str | None = None
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error_code: str | None = None
    message: str = ""
    retryable: bool = False
    retry_after_seconds: float | None = None
    failure_class: ResearchFetchFailureClass | None = None


@dataclass(frozen=True, slots=True)
class _ResponseData:
    status_code: int
    headers: httpx.Headers
    body: bytes


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "text/csv",
        "application/json",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for record in records:
        address = str(record[4][0])
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _normalized_host(host: str) -> str:
    return host.casefold().rstrip(".")


def _media_type(headers: httpx.Headers) -> str | None:
    value = headers.get("content-type")
    if not value:
        return None
    media_type = value.split(";", 1)[0].strip().casefold()
    return media_type or None


def _retry_after(headers: httpx.Headers, *, maximum: float) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, maximum)


def _display_host(host: str, port: int, scheme: str) -> str:
    rendered = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return rendered if port == default_port else f"{rendered}:{port}"


def _connect_url(logical_url: str, address: str) -> tuple[str, str, str]:
    try:
        parts = urlsplit(logical_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise NetworkPolicyError("URL authority or port is malformed") from exc
    if parts.hostname is None:
        raise NetworkPolicyError("URL has no hostname")
    host_header = _display_host(parts.hostname, port, parts.scheme)
    address_host = f"[{address}]" if ":" in address else address
    netloc = (
        address_host
        if port == (443 if parts.scheme == "https" else 80)
        else f"{address_host}:{port}"
    )
    path = parts.path or "/"
    connect = urlunsplit((parts.scheme, netloc, path, parts.query, ""))
    return connect, host_header, parts.hostname


def _identity_failure_class(error: ResearchSourceIdentityError) -> ResearchFetchFailureClass:
    if error.code == "unsupported_source":
        return ResearchFetchFailureClass.UNSUPPORTED
    return ResearchFetchFailureClass.POLICY


class HttpxResearchFetcher:
    """Public HTTP fetcher with DNS pinning, redirect revalidation and hard body limits."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver
        self._transport = transport

    def _resolve_public_addresses(
        self,
        logical_url: str,
        *,
        policy: HttpFetchPolicy,
    ) -> tuple[str, ...]:
        try:
            parts = urlsplit(logical_url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise NetworkPolicyError("URL authority or port is malformed") from exc
        if parts.scheme not in {"http", "https"}:
            raise UnsupportedResearchSourceError("only HTTP(S) URLs are supported")
        if parts.scheme == "http" and not policy.allow_insecure_http:
            raise NetworkPolicyError("plain HTTP is disabled by policy")
        if parts.hostname is None:
            raise NetworkPolicyError("URL has no hostname")
        if parts.username is not None or parts.password is not None:
            raise NetworkPolicyError("URL userinfo credentials are forbidden")
        host = _normalized_host(parts.hostname)
        allowed_hosts = {_normalized_host(item) for item in policy.allowed_hosts}
        if allowed_hosts and host not in allowed_hosts:
            raise NetworkPolicyError("URL host is outside the approved host set")
        if port not in policy.allowed_ports:
            raise NetworkPolicyError("URL port is outside the approved port set")

        try:
            literal = ip_address(host)
            resolved = (str(literal),)
        except ValueError:
            resolved = self._resolver(host, port)
        if not resolved:
            raise NetworkPolicyError("hostname resolved to no addresses")

        safe: list[str] = []
        for value in resolved:
            try:
                address = ip_address(value)
            except ValueError as exc:
                raise NetworkPolicyError("resolver returned an invalid IP address") from exc
            if not policy.allow_private_networks and not address.is_global:
                continue
            rendered = str(address)
            if rendered not in safe:
                safe.append(rendered)
        if not safe:
            raise PrivateResearchSourceError("hostname resolves only to non-public addresses")
        return tuple(safe[: policy.max_resolved_addresses])

    @staticmethod
    def _read_bounded(response: httpx.Response, *, maximum: int) -> bytes:
        data = bytearray()
        for chunk in response.iter_bytes():
            if len(data) + len(chunk) > maximum:
                raise ResponseTooLargeError(f"HTTP response exceeds {maximum} byte limit")
            data.extend(chunk)
        return bytes(data)

    def _request(
        self,
        client: httpx.Client,
        logical_url: str,
        *,
        headers: dict[str, str],
        policy: HttpFetchPolicy,
    ) -> _ResponseData:
        addresses = self._resolve_public_addresses(logical_url, policy=policy)
        last_error: httpx.TransportError | None = None
        for address in addresses:
            connect_url, host_header, sni_host = _connect_url(logical_url, address)
            request_headers = dict(headers)
            request_headers["Host"] = host_header
            extensions: dict[str, object] = {}
            if logical_url.startswith("https://"):
                extensions["sni_hostname"] = sni_host
            client.cookies.clear()
            try:
                with client.stream(
                    "GET",
                    connect_url,
                    headers=request_headers,
                    extensions=extensions,
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES or response.status_code == 304:
                        body = b""
                    elif 200 <= response.status_code < 300:
                        body = self._read_bounded(response, maximum=policy.max_response_bytes)
                    else:
                        body = b""
                    return _ResponseData(response.status_code, response.headers, body)
            except httpx.TransportError as exc:
                last_error = exc
        if last_error is None:
            raise httpx.ConnectError("no resolved address could be attempted")
        raise last_error

    def fetch(
        self,
        url: str,
        *,
        validators: HttpValidators | None = None,
        policy: HttpFetchPolicy | None = None,
    ) -> HttpFetchResult:
        active = policy or HttpFetchPolicy()
        try:
            requested_url = canonical_http_locator(url)
        except ResearchSourceIdentityError as exc:
            public_url = (
                "<redacted-http-url>"
                if exc.code == "credentials_forbidden"
                else "<invalid-http-url>"
            )
            return HttpFetchResult(
                RefreshDisposition.BLOCKED,
                public_url,
                public_url,
                None,
                error_code="network_policy",
                message=str(exc),
                failure_class=_identity_failure_class(exc),
            )
        current_url = requested_url
        headers = {
            "Accept": (
                "text/html,text/plain,text/markdown,text/csv,application/json,application/pdf,"
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "Accept-Encoding": "identity",
            "User-Agent": active.user_agent,
        }
        if validators is not None:
            if validators.etag:
                headers["If-None-Match"] = validators.etag
            if validators.last_modified:
                headers["If-Modified-Since"] = validators.last_modified

        timeout = httpx.Timeout(
            connect=active.connect_timeout_seconds,
            read=active.read_timeout_seconds,
            write=active.write_timeout_seconds,
            pool=active.pool_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=active.max_connections,
            max_keepalive_connections=active.max_keepalive_connections,
        )
        try:
            with httpx.Client(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                for redirect_count in range(active.max_redirects + 1):
                    response = self._request(
                        client,
                        current_url,
                        headers=headers,
                        policy=active,
                    )
                    status = response.status_code
                    etag = response.headers.get("etag")
                    last_modified = response.headers.get("last-modified")
                    if status in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            return HttpFetchResult(
                                RefreshDisposition.FAILED,
                                requested_url,
                                current_url,
                                status,
                                error_code="redirect_missing_location",
                                message="redirect response did not include Location",
                                failure_class=ResearchFetchFailureClass.HTTP,
                            )
                        if redirect_count >= active.max_redirects:
                            return HttpFetchResult(
                                RefreshDisposition.FAILED,
                                requested_url,
                                current_url,
                                status,
                                error_code="redirect_limit",
                                message="redirect limit exceeded",
                                failure_class=ResearchFetchFailureClass.HTTP,
                            )
                        try:
                            current_url = canonical_http_locator(urljoin(current_url, location))
                        except ResearchSourceIdentityError as exc:
                            return HttpFetchResult(
                                RefreshDisposition.BLOCKED,
                                requested_url,
                                current_url,
                                status,
                                error_code="network_policy",
                                message=f"redirect target violates research URL policy: {exc}",
                                failure_class=_identity_failure_class(exc),
                            )
                        headers.pop("If-None-Match", None)
                        headers.pop("If-Modified-Since", None)
                        continue
                    if status == 304:
                        return HttpFetchResult(
                            RefreshDisposition.NOT_MODIFIED,
                            requested_url,
                            current_url,
                            status,
                            etag=etag,
                            last_modified=last_modified,
                        )
                    if status in {401, 403}:
                        return HttpFetchResult(
                            RefreshDisposition.BLOCKED,
                            requested_url,
                            current_url,
                            status,
                            error_code="authentication_required",
                            message="source requires credentials or access not granted to Research",
                            failure_class=ResearchFetchFailureClass.AUTH,
                        )
                    if status in {404, 410}:
                        return HttpFetchResult(
                            RefreshDisposition.REMOVED,
                            requested_url,
                            current_url,
                            status,
                            error_code="source_removed",
                            message="source returned a permanent missing status",
                            failure_class=ResearchFetchFailureClass.HTTP,
                        )
                    if status in _RETRYABLE_STATUSES:
                        return HttpFetchResult(
                            RefreshDisposition.FAILED,
                            requested_url,
                            current_url,
                            status,
                            error_code="retryable_http_status",
                            message=f"source returned retryable HTTP status {status}",
                            retryable=True,
                            retry_after_seconds=_retry_after(
                                response.headers,
                                maximum=active.max_backoff_seconds,
                            ),
                            failure_class=ResearchFetchFailureClass.HTTP,
                        )
                    if status < 200 or status >= 300:
                        return HttpFetchResult(
                            RefreshDisposition.FAILED,
                            requested_url,
                            current_url,
                            status,
                            error_code="http_status",
                            message=f"source returned HTTP status {status}",
                            failure_class=ResearchFetchFailureClass.HTTP,
                        )
                    media_type = _media_type(response.headers)
                    if media_type not in _SUPPORTED_MEDIA_TYPES:
                        return HttpFetchResult(
                            RefreshDisposition.UNSUPPORTED,
                            requested_url,
                            current_url,
                            status,
                            media_type=media_type,
                            etag=etag,
                            last_modified=last_modified,
                            error_code="unsupported_media_type",
                            message=f"unsupported response media type: {media_type or '<missing>'}",
                            failure_class=ResearchFetchFailureClass.UNSUPPORTED,
                        )
                    return HttpFetchResult(
                        RefreshDisposition.CHANGED,
                        requested_url,
                        current_url,
                        status,
                        media_type=media_type,
                        body=response.body,
                        etag=etag,
                        last_modified=last_modified,
                    )
        except PrivateResearchSourceError as exc:
            return HttpFetchResult(
                RefreshDisposition.BLOCKED,
                requested_url,
                current_url,
                None,
                error_code="network_policy",
                message=str(exc),
                failure_class=ResearchFetchFailureClass.PRIVATE,
            )
        except UnsupportedResearchSourceError as exc:
            return HttpFetchResult(
                RefreshDisposition.BLOCKED,
                requested_url,
                current_url,
                None,
                error_code="network_policy",
                message=str(exc),
                failure_class=ResearchFetchFailureClass.UNSUPPORTED,
            )
        except NetworkPolicyError as exc:
            return HttpFetchResult(
                RefreshDisposition.BLOCKED,
                requested_url,
                current_url,
                None,
                error_code="network_policy",
                message=str(exc),
                failure_class=ResearchFetchFailureClass.POLICY,
            )
        except ResponseTooLargeError as exc:
            return HttpFetchResult(
                RefreshDisposition.FAILED,
                requested_url,
                current_url,
                None,
                error_code="response_too_large",
                message=str(exc),
                failure_class=ResearchFetchFailureClass.RESOURCE,
            )
        except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
            return HttpFetchResult(
                RefreshDisposition.FAILED,
                requested_url,
                current_url,
                None,
                error_code=type(exc).__name__,
                message=str(exc)[:1000],
                retryable=True,
                failure_class=ResearchFetchFailureClass.NETWORK,
            )
