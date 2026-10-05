from __future__ import annotations

from typing import Protocol

from nika_core.web_api.contracts import WebCommand, WebCommandResult, WebPrincipal


class WebCommandOutcomeUnknownError(RuntimeError):
    """The handler may have applied an effect; callers must reconcile before retry."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__("Web command outcome is unknown and requires reconciliation")


class WebAuthorizationPort(Protocol):
    """Server-side authority decision for one already-admitted Web command."""

    def allows(self, principal: WebPrincipal, command: WebCommand) -> bool: ...


class WebCommandHandler(Protocol):
    """Presentation-neutral application service behind the Web boundary."""

    def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult: ...


class WebApplicationBoundary:
    """Validate, authorize and dispatch an untrusted Web command.

    Authentication is intentionally outside this class. The caller must establish a
    ``WebPrincipal`` from trusted server-side session/token state and pass it separately
    from client JSON, so a browser cannot self-assert tenant/user/workspace authority.
    """

    def __init__(
        self,
        *,
        authorization: WebAuthorizationPort,
        handler: WebCommandHandler,
    ) -> None:
        self._authorization = authorization
        self._handler = handler

    def dispatch(self, *, principal: WebPrincipal, command: object) -> WebCommandResult:
        if type(principal) is not WebPrincipal:
            raise ValueError("principal must be the exact server authority carrier")
        admitted = WebCommand.from_untrusted(command)

        allowed = self._authorization.allows(principal, admitted)
        if type(allowed) is not bool:
            raise RuntimeError("Web authorization port must return an exact bool")
        if not allowed:
            return WebCommandResult.create(
                request_id=admitted.request_id,
                status="rejected",
                code="forbidden",
                message="Дію заборонено поточними серверними повноваженнями.",
            )

        try:
            result = self._handler.handle(principal, admitted)
            if type(result) is not WebCommandResult:
                raise RuntimeError("Web command handler returned an invalid result carrier")
            if result.request_id != admitted.request_id:
                raise RuntimeError("Web command handler changed the request identity")
            return WebCommandResult.create(
                request_id=result.request_id,
                status=result.status,
                code=result.code,
                message=result.message,
                data=result.data,
            )
        except WebCommandOutcomeUnknownError:
            raise
        except Exception as exc:
            raise WebCommandOutcomeUnknownError(admitted.request_id) from exc
