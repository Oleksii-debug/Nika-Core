from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlparse


class FixtureFamily(StrEnum):
    IMMEDIATE_SUCCESS = "immediate_success"
    DELAYED_SEMANTIC_CONTROL = "delayed_semantic_control"
    TEMPORARY_BUSY = "temporary_busy"
    RATE_LIMIT_TRANSIENT = "rate_limit_transient"
    DETERMINISTIC_FAILURE = "deterministic_failure"
    DELAYED_RESULT = "delayed_result"
    DUPLICATE_ACCESSIBLE_NAME = "duplicate_accessible_name_ambiguity"
    DISABLED_THEN_ENABLED = "disabled_then_enabled"
    NAVIGATION_RESULT = "navigation_result"
    AMBIGUOUS_ACTION_NO_RETRY = "ambiguous_action_no_retry"


@dataclass(frozen=True, slots=True)
class ScenarioBTarget:
    target_id: str
    family: FixtureFamily
    input_order: int
    delay_ms: int = 0
    retry_safe: bool = True

    @property
    def path(self) -> str:
        return f"/targets/{self.target_id}"

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["family"] = self.family.value
        payload["path"] = self.path
        return payload


SCENARIO_B_TARGETS: tuple[ScenarioBTarget, ...] = (
    ScenarioBTarget("scenario-b-01", FixtureFamily.IMMEDIATE_SUCCESS, 1),
    ScenarioBTarget("scenario-b-02", FixtureFamily.IMMEDIATE_SUCCESS, 2),
    ScenarioBTarget("scenario-b-03", FixtureFamily.DELAYED_SEMANTIC_CONTROL, 3, delay_ms=120),
    ScenarioBTarget("scenario-b-04", FixtureFamily.DELAYED_SEMANTIC_CONTROL, 4, delay_ms=180),
    ScenarioBTarget("scenario-b-05", FixtureFamily.TEMPORARY_BUSY, 5),
    ScenarioBTarget("scenario-b-06", FixtureFamily.TEMPORARY_BUSY, 6),
    ScenarioBTarget("scenario-b-07", FixtureFamily.RATE_LIMIT_TRANSIENT, 7),
    ScenarioBTarget("scenario-b-08", FixtureFamily.RATE_LIMIT_TRANSIENT, 8),
    ScenarioBTarget("scenario-b-09", FixtureFamily.DETERMINISTIC_FAILURE, 9),
    ScenarioBTarget("scenario-b-10", FixtureFamily.DETERMINISTIC_FAILURE, 10),
    ScenarioBTarget("scenario-b-11", FixtureFamily.DELAYED_RESULT, 11, delay_ms=140),
    ScenarioBTarget("scenario-b-12", FixtureFamily.DELAYED_RESULT, 12, delay_ms=200),
    ScenarioBTarget("scenario-b-13", FixtureFamily.DUPLICATE_ACCESSIBLE_NAME, 13),
    ScenarioBTarget("scenario-b-14", FixtureFamily.DUPLICATE_ACCESSIBLE_NAME, 14),
    ScenarioBTarget("scenario-b-15", FixtureFamily.DISABLED_THEN_ENABLED, 15, delay_ms=130),
    ScenarioBTarget("scenario-b-16", FixtureFamily.DISABLED_THEN_ENABLED, 16, delay_ms=190),
    ScenarioBTarget("scenario-b-17", FixtureFamily.NAVIGATION_RESULT, 17),
    ScenarioBTarget("scenario-b-18", FixtureFamily.NAVIGATION_RESULT, 18),
    ScenarioBTarget(
        "scenario-b-19",
        FixtureFamily.AMBIGUOUS_ACTION_NO_RETRY,
        19,
        retry_safe=False,
    ),
    ScenarioBTarget(
        "scenario-b-20",
        FixtureFamily.AMBIGUOUS_ACTION_NO_RETRY,
        20,
        retry_safe=False,
    ),
)

_TARGET_BY_ID = {target.target_id: target for target in SCENARIO_B_TARGETS}

if len(SCENARIO_B_TARGETS) != 20 or len(_TARGET_BY_ID) != 20:
    raise RuntimeError("Scenario B fixture must define exactly 20 unique target identities")


@dataclass(slots=True)
class _ScenarioBState:
    attempts: dict[str, int] = field(default_factory=dict)
    effects: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, target_id: str, *, effect: bool) -> tuple[int, int]:
        with self.lock:
            self.attempts[target_id] = self.attempts.get(target_id, 0) + 1
            if effect:
                self.effects[target_id] = self.effects.get(target_id, 0) + 1
            return self.attempts[target_id], self.effects.get(target_id, 0)

    def reset(self, target_id: str | None = None) -> None:
        with self.lock:
            if target_id is None:
                self.attempts.clear()
                self.effects.clear()
                return
            self.attempts.pop(target_id, None)
            self.effects.pop(target_id, None)

    def snapshot(self, target_id: str) -> dict[str, object]:
        target = _require_target(target_id)
        with self.lock:
            return {
                "target_id": target.target_id,
                "family": target.family.value,
                "attempt_count": self.attempts.get(target_id, 0),
                "effect_count": self.effects.get(target_id, 0),
                "retry_safe": target.retry_safe,
            }


def _require_target(target_id: str) -> ScenarioBTarget:
    try:
        return _TARGET_BY_ID[target_id]
    except KeyError as exc:
        raise ValueError(f"unknown Scenario B target: {target_id}") from exc


def scenario_b_manifest() -> tuple[dict[str, object], ...]:
    return tuple(target.to_payload() for target in SCENARIO_B_TARGETS)


def _document(target: ScenarioBTarget, body: str, *, title: str | None = None) -> bytes:
    safe_id = escape(target.target_id)
    safe_title = escape(title or f"Scenario B {target.target_id}")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title></head><body>"
        f"<main role='main' aria-label='Scenario B target {safe_id}'>"
        f"<h1>Scenario B target {safe_id}</h1>"
        f"<p role='status' aria-label='Target status' id='target-status' "
        f"data-target-id='{safe_id}'>Ready</p>"
        f"{body}</main></body></html>"
    ).encode("utf-8")


def _action_form(target: ScenarioBTarget, *, disabled: bool = False) -> str:
    disabled_attr = " disabled" if disabled else ""
    return (
        f"<form action='/actions/{escape(target.target_id)}' method='post'>"
        f"<button type='submit' aria-label='Execute target'{disabled_attr}>"
        "Execute target</button></form>"
    )


def _target_page(target: ScenarioBTarget) -> bytes:
    family = target.family
    if family is FixtureFamily.DELAYED_SEMANTIC_CONTROL:
        body = (
            "<section role='region' aria-label='Action area' id='action-area'></section>"
            "<script>"
            f"setTimeout(() => {{"
            "document.getElementById('target-status').textContent='Control ready';"
            "document.getElementById('action-area').innerHTML="
            f"`{_action_form(target)}`;"
            f"}}, {target.delay_ms});"
            "</script>"
        )
        return _document(target, body)

    if family is FixtureFamily.DUPLICATE_ACCESSIBLE_NAME:
        form = _action_form(target)
        return _document(
            target,
            "<section role='region' aria-label='Duplicate action A'>"
            f"{form}</section>"
            "<section role='region' aria-label='Duplicate action B'>"
            f"{form}</section>",
        )

    if family is FixtureFamily.DISABLED_THEN_ENABLED:
        body = (
            "<section role='region' aria-label='Action area'>"
            f"{_action_form(target, disabled=True)}</section>"
            "<script>"
            f"setTimeout(() => {{"
            "const button=document.querySelector(\"button[aria-label='Execute target']\");"
            "button.disabled=false;"
            "document.getElementById('target-status').textContent='Control enabled';"
            f"}}, {target.delay_ms});"
            "</script>"
        )
        return _document(target, body)

    return _document(
        target,
        "<section role='region' aria-label='Action area'>"
        f"{_action_form(target)}</section>",
    )


def _state_page(
    target: ScenarioBTarget,
    *,
    state: str,
    text: str,
    retry: bool = False,
    delayed_ms: int = 0,
) -> bytes:
    retry_form = _action_form(target) if retry else ""
    retry_safe = "true" if target.retry_safe else "false"
    if delayed_ms:
        body = (
            f"<p role='status' aria-label='Action result' id='action-result' "
            f"data-state='pending' data-retry-safe='{retry_safe}'>Pending result</p>"
            "<script>"
            f"setTimeout(() => {{"
            "const result=document.getElementById('action-result');"
            f"result.textContent={json.dumps(text)};"
            f"result.dataset.state={json.dumps(state)};"
            f"}}, {delayed_ms});"
            "</script>"
        )
    else:
        body = (
            f"<p role='status' aria-label='Action result' data-state='{escape(state)}' "
            f"data-retry-safe='{retry_safe}'>{escape(text)}</p>{retry_form}"
        )
    return _document(target, body, title=f"{target.target_id} {state}")


def _handler(state: _ScenarioBState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "NikaScenarioBFixture/1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _send(
            self,
            status: int,
            payload: bytes,
            content_type: str = "text/html; charset=utf-8",
            *,
            retry_after: str | None = None,
            location: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; base-uri 'none'; object-src 'none'",
            )
            self.send_header("Cache-Control", "no-store")
            if retry_after is not None:
                self.send_header("Retry-After", retry_after)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _json(self, payload: object, status: int = 200) -> None:
            self._send(
                status,
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._json({"fixture": "scenario-b", "status": "ok", "target_count": 20})
                return
            if parsed.path == "/manifest.json":
                self._json({"scenario": "B", "targets": scenario_b_manifest()})
                return
            if parsed.path.startswith("/targets/"):
                target_id = parsed.path.removeprefix("/targets/")
                try:
                    target = _require_target(target_id)
                except ValueError:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                self._send(200, _target_page(target))
                return
            if parsed.path.startswith("/state/"):
                target_id = parsed.path.removeprefix("/state/")
                try:
                    payload = state.snapshot(target_id)
                except ValueError:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                self._json(payload)
                return
            if parsed.path.startswith("/results/"):
                target_id = parsed.path.removeprefix("/results/")
                try:
                    target = _require_target(target_id)
                except ValueError:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                self._send(
                    200,
                    _state_page(target, state="succeeded", text="Navigation result confirmed"),
                )
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/reset":
                state.reset()
                self._json({"reset": "all"})
                return
            if parsed.path.startswith("/reset/"):
                target_id = parsed.path.removeprefix("/reset/")
                try:
                    _require_target(target_id)
                except ValueError:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                state.reset(target_id)
                self._json({"reset": target_id})
                return
            if not parsed.path.startswith("/actions/"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return

            target_id = parsed.path.removeprefix("/actions/")
            try:
                target = _require_target(target_id)
            except ValueError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return

            family = target.family
            if family is FixtureFamily.TEMPORARY_BUSY:
                attempt, _ = state.record(target_id, effect=False)
                if attempt == 1:
                    self._send(
                        503,
                        _state_page(
                            target,
                            state="temporary_busy",
                            text="Temporarily busy",
                            retry=True,
                        ),
                    )
                    return
                with state.lock:
                    state.effects[target_id] = state.effects.get(target_id, 0) + 1
                self._send(200, _state_page(target, state="succeeded", text="Success"))
                return

            if family is FixtureFamily.RATE_LIMIT_TRANSIENT:
                attempt, _ = state.record(target_id, effect=False)
                if attempt == 1:
                    self._send(
                        429,
                        _state_page(
                            target,
                            state="rate_limited",
                            text="Rate limited",
                            retry=True,
                        ),
                        retry_after="1",
                    )
                    return
                with state.lock:
                    state.effects[target_id] = state.effects.get(target_id, 0) + 1
                self._send(200, _state_page(target, state="succeeded", text="Success"))
                return

            if family is FixtureFamily.DETERMINISTIC_FAILURE:
                state.record(target_id, effect=False)
                self._send(
                    422,
                    _state_page(
                        target,
                        state="failed",
                        text="Deterministic validation failure",
                    ),
                )
                return

            if family is FixtureFamily.DELAYED_RESULT:
                state.record(target_id, effect=True)
                self._send(
                    200,
                    _state_page(
                        target,
                        state="succeeded",
                        text="Delayed result confirmed",
                        delayed_ms=target.delay_ms,
                    ),
                )
                return

            if family is FixtureFamily.NAVIGATION_RESULT:
                state.record(target_id, effect=True)
                self._send(
                    303,
                    b"",
                    "text/plain; charset=utf-8",
                    location=f"/results/{target.target_id}",
                )
                return

            if family is FixtureFamily.AMBIGUOUS_ACTION_NO_RETRY:
                attempt, effect_count = state.record(target_id, effect=True)
                self._send(
                    202,
                    _state_page(
                        target,
                        state="ambiguous",
                        text=(
                            "Action was accepted by the fixture but completion is unproven; "
                            f"attempt={attempt}; effect_count={effect_count}; automatic retry forbidden"
                        ),
                    ),
                )
                return

            state.record(target_id, effect=True)
            self._send(200, _state_page(target, state="succeeded", text="Success"))

    return Handler


class ScenarioBFixtureServer:
    """Deterministic loopback-only Scenario B web fixture for acceptance tests."""

    def __init__(self) -> None:
        self.state = _ScenarioBState()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.state))
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="nika-scenario-b-fixture",
            daemon=True,
        )
        self._started = False

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def manifest(self) -> tuple[dict[str, object], ...]:
        return scenario_b_manifest()

    def target_url(self, target_id: str) -> str:
        target = _require_target(target_id)
        return f"{self.base_url}{target.path}"

    def state_url(self, target_id: str) -> str:
        _require_target(target_id)
        return f"{self.base_url}/state/{target_id}"

    def reset(self, target_id: str | None = None) -> None:
        if target_id is not None:
            _require_target(target_id)
        self.state.reset(target_id)

    def start(self) -> Self:
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def close(self) -> None:
        if not self._started:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._started = False

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()
