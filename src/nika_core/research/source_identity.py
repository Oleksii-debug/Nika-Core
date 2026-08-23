from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit, urlunsplit


class ResearchSourceIdentityError(ValueError):
    """Fail-closed source identity violation with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "client_secret",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x_amz_credential",
        "x_goog_credential",
    }
)
_SENSITIVE_QUERY_SUFFIXES = ("_token", "_secret", "_password", "_signature")


def _query_contains_credentials(query: str) -> bool:
    for key, _value in parse_qsl(query, keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_KEYS or normalized.endswith(_SENSITIVE_QUERY_SUFFIXES):
            return True
    return False


def canonical_http_locator(locator: str) -> str:
    """Return the credential-free canonical locator used for HTTP source identity.

    This is deliberately narrower than URL canonicalization for crawling. It only
    normalizes components that cannot change the fetched HTTP resource identity:
    scheme/host case, IDNA host spelling, default ports, an empty path, and the
    fragment (which is never sent in an HTTP request). Query spelling/order is kept.
    """

    raw = locator.strip()
    if not raw:
        raise ResearchSourceIdentityError("invalid_source", "HTTP source URL is required")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ResearchSourceIdentityError(
            "invalid_source",
            "HTTP source URL authority or port is malformed",
        ) from exc

    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ResearchSourceIdentityError(
            "unsupported_source",
            "only HTTP(S) research sources are supported",
        )
    if parts.hostname is None:
        raise ResearchSourceIdentityError("invalid_source", "HTTP source URL has no hostname")
    if parts.username is not None or parts.password is not None or _query_contains_credentials(
        parts.query
    ):
        raise ResearchSourceIdentityError(
            "credentials_forbidden",
            "credentials must not be embedded in a research source URL",
        )

    try:
        host = parts.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise ResearchSourceIdentityError(
            "invalid_source",
            "HTTP source hostname cannot be normalized",
        ) from exc
    if not host:
        raise ResearchSourceIdentityError("invalid_source", "HTTP source URL has no hostname")

    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    netloc = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))
